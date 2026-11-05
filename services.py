"""Operations shared by the web app and the MCP tools, so both write the same records and audit events."""
import os
import re
from urllib.parse import quote

from checks import ITEMS, normalize, run_checks, summarize
from models import FIELD_NAMES, STATUSES, Event, Vendor, VendorFields, now

STATUS_LABELS = {"requested": "Requested", "submitted": "Submitted", "needs_fixes": "Needs fixes",
                 "verified": "Verified", "approved": "Approved"}


def base_url(fallback):
    return (os.environ.get("PUBLIC_URL") or str(fallback)).rstrip("/")


def public_link(vendor, base):
    return f"{base}/v/{vendor.public_token}"


def log(db, vendor, actor, action, detail=None):
    db.add(Event(vendor_id=vendor.id, actor=actor, action=action, detail=detail))


def ensure_fields(vendor):
    if vendor.fields is None:
        vendor.fields = VendorFields()
    return vendor.fields


def vendor_checks(vendor):
    fields = ensure_fields(vendor).as_dict()
    checks = run_checks(fields, vendor.state, vendor.required_items or [], {f.kind for f in vendor.files})
    order = {"fail": 0, "missing": 1, "warn": 2, "pass": 3}
    return sorted(checks, key=lambda c: order[c["status"]])  # problems first (stable within a status)


def validate_items(items):
    bad = [i for i in items if i not in ITEMS]
    if bad:
        raise ValueError(f"Unknown required_items {bad}. Allowed: {list(ITEMS)}")
    return list(dict.fromkeys(items))


def validate_status(status):
    if status not in STATUSES:
        raise ValueError(f"Unknown status '{status}'. Allowed: {STATUSES}")
    return status


def save_fields(vendor, data):
    """Saves the known fields present in `data`. Returns names of fields that changed (never values)."""
    f = ensure_fields(vendor)
    changed = []
    for name in FIELD_NAMES:
        if name in data:
            value = normalize(name, data[name])
            if value != getattr(f, name):
                setattr(f, name, value)
                changed.append(name)
    if changed:
        vendor.updated_at = now()
    return changed


def set_status(db, vendor, status, actor):
    validate_status(status)
    if status != vendor.status:
        log(db, vendor, actor, "status changed", f"{STATUS_LABELS[vendor.status]} → {STATUS_LABELS[status]}")
        vendor.status = status
        vendor.updated_at = now()


def mask(acct):
    return f"•••• {acct[-4:]}" if acct else ""


def days_since(dt):
    return (now() - dt).days if dt else 0


def share_links(vendor, message, base):
    """WhatsApp and mailto links with the message prefilled. The app sends nothing itself."""
    if not message:
        return None
    link = public_link(vendor, base)
    text = message if link in message else f"{message}\n\n{link}"
    out = {}
    digits = re.sub(r"\D", "", vendor.contact_phone or "")
    if len(digits) == 10:
        digits = "91" + digits
    out["whatsapp"] = f"https://wa.me/{digits}?text={quote(text)}" if digits else f"https://wa.me/?text={quote(text)}"
    out["email"] = (f"mailto:{quote(vendor.contact_email or '')}?subject={quote('Vendor onboarding: ' + vendor.vendor_name)}"
                    f"&body={quote(text)}")
    return out


def vendor_summary(vendor, base):
    checks = vendor_checks(vendor)
    return {
        "id": vendor.id, "vendor_name": vendor.vendor_name, "status": vendor.status,
        "contact_name": vendor.contact_name, "category": vendor.category, "state": vendor.state,
        "check_summary": summarize(checks), "public_link": public_link(vendor, base),
        "days_in_status": days_since(vendor.updated_at), "has_claude_review": bool(vendor.review_findings),
        "bank_account_number": mask(ensure_fields(vendor).bank_account_number),
    }


def vendor_detail(vendor, base):
    checks = vendor_checks(vendor)
    return {
        "id": vendor.id, "vendor_name": vendor.vendor_name, "status": vendor.status,
        "contact": {"name": vendor.contact_name, "phone": vendor.contact_phone, "email": vendor.contact_email},
        "category": vendor.category, "declared_state": vendor.state,
        "required_items": [{"key": k, "label": ITEMS[k]["label"]} for k in vendor.required_items or [] if k in ITEMS],
        "public_link": public_link(vendor, base),
        "request_message": vendor.request_message, "notes": vendor.notes,
        "submitted_fields": ensure_fields(vendor).as_dict(),
        "check_results": checks, "check_summary": summarize(checks),
        "files": [{"kind": f.kind, "filename": f.filename, "content_type": f.content_type, "size_bytes": f.size,
                   "uploaded_at": f.uploaded_at.isoformat(timespec="seconds")} for f in vendor.files],
        "last_review": {"findings": vendor.review_findings, "follow_up_message": vendor.follow_up_message,
                        "reviewed_at": vendor.reviewed_at.isoformat(timespec="seconds") if vendor.reviewed_at else None},
        "submitted_at": vendor.submitted_at.isoformat(timespec="seconds") if vendor.submitted_at else None,
        "history": [{"at": e.created_at.isoformat(timespec="seconds"), "by": e.actor, "action": e.action,
                     "detail": e.detail} for e in vendor.events[:30]],
    }
