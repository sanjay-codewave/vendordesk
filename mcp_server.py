"""Remote MCP server (Streamable HTTP). `mcp` is the open Model Context Protocol SDK, not an AI client:
the app makes no model calls. Claude, running in the customer's own account, calls these tools."""
from sqlalchemy import func, select

from checks import ITEMS
from models import FIELD_NAMES, STATUSES, SessionLocal, User, Vendor, now
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from services import (base_url, days_since, log, save_fields, set_status, validate_items, vendor_detail,
                      vendor_summary)

INSTRUCTIONS = f"""VendorDesk keeps the customer's vendor onboarding records: a checklist and a public upload link
per vendor, the vendor's submitted details and documents, rule-based checks (GSTIN checksum, PAN, IFSC, account
number, state and name matching) and an audit history. The app has no AI: you do the thinking and writing.
- Write every message yourself (request and follow-up messages) and pass it to the tools.
- VendorDesk never sends messages. Never say a message was sent; give the user the link and message to send.
- If vendor details needed for a tool are missing (e.g. the vendor's name), ask the user. Do not guess or invent
  contact details, GSTIN, PAN or bank details.
- Checklist keys for required_items: {", ".join(ITEMS)}.
- Statuses: {", ".join(STATUSES)}. Approval is the user's decision: only set 'approved' when the user asks."""

mcp = FastMCP(
    "VendorDesk",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _request(ctx):
    return ctx.request_context.request


def _user(db, ctx):
    # Token comes from /mcp/<token>, put into the ASGI scope by TokenPathMiddleware (contextvars don't propagate
    # into stateless handlers).
    token = _request(ctx).scope.get("mcp_token")
    user = db.scalar(select(User).where(User.token == token)) if token else None
    if not user:
        raise ValueError("Invalid VendorDesk connection token. Copy the MCP link again from the VendorDesk dashboard "
                         "(Connect to Claude panel) and update the connector URL.")
    return user


def _base(ctx):
    return base_url(_request(ctx).base_url)


def _vendor(db, user, vendor_id):
    v = db.get(Vendor, vendor_id)
    if not v or v.user_id != user.id:
        raise ValueError(f"No vendor with id {vendor_id}. Use list_vendors to find the right id.")
    return v


@mcp.tool()
def get_summary(ctx: Context) -> dict:
    """Check the VendorDesk connection and get an overview: the business name, vendor counts per status, and
    vendors needing attention (submissions awaiting review, and requests or fixes pending for 3+ days).
    Call this first if unsure the connection works, or when the user asks what needs doing."""
    with SessionLocal() as db:
        user = _user(db, ctx)
        counts = dict(db.execute(select(Vendor.status, func.count()).where(Vendor.user_id == user.id)
                                 .group_by(Vendor.status)).all())
        attention = []
        for v in db.scalars(select(Vendor).where(Vendor.user_id == user.id, Vendor.status != "approved")):
            days = days_since(v.updated_at)
            if v.status == "submitted":
                attention.append({"id": v.id, "vendor_name": v.vendor_name, "why": "Submitted, awaiting review"})
            elif v.status in ("requested", "needs_fixes") and days >= 3:
                attention.append({"id": v.id, "vendor_name": v.vendor_name,
                                  "why": f"{v.status.replace('_', ' ')} for {days} days, consider a reminder"})
        return {"connected": True, "business_name": user.business_name,
                "counts": {s: counts.get(s, 0) for s in STATUSES}, "needs_attention": attention,
                "dashboard": _base(ctx) + "/dashboard"}


@mcp.tool()
def create_vendor_request(ctx: Context, vendor_name: str, required_items: list[str], request_message: str,
                          contact_name: str = "", contact_phone: str = "", contact_email: str = "",
                          category: str = "", state: str = "", notes: str = "") -> dict:
    """Start onboarding a new vendor. Creates the vendor record, its document checklist and a unique public link
    where the vendor (no login) fills in details and uploads documents.

    - required_items: choose the checklist from these keys: legal_name, address, gstin, pan, bank_details
      (account name, number, IFSC, bank), msme, gst_certificate, pan_card, cancelled_cheque. Pick what the user
      asked for; 'GST' usually means gstin (+ gst_certificate if they want the document), 'bank details' means
      bank_details. legal_name is always worth including.
    - request_message: the message YOU write for the user to send the vendor (WhatsApp/email tone, short,
      friendly, lists what is needed). It may contain {link}, which is replaced by the public link; otherwise the
      link is appended when the user shares it.
    - state: the vendor's state as the user stated it (e.g. Maharashtra); used to cross-check the GSTIN.
    Returns the public link and the final message. VendorDesk does not send anything: show the user the link and
    message and tell them to send it via the WhatsApp/email buttons in the app or their own channel."""
    with SessionLocal() as db:
        user = _user(db, ctx)
        if not vendor_name.strip():
            raise ValueError("vendor_name is required. Ask the user for the vendor's name.")
        items = validate_items(required_items)
        if not items:
            raise ValueError(f"required_items is empty. Choose from: {list(ITEMS)}")
        v = Vendor(user_id=user.id, vendor_name=vendor_name.strip(), contact_name=contact_name or None,
                   contact_phone=contact_phone or None, contact_email=contact_email or None,
                   category=category or None, state=state or None, required_items=items,
                   notes=notes or None, created_by="claude")
        db.add(v)
        db.flush()
        link = f"{_base(ctx)}/v/{v.public_token}"
        v.request_message = request_message.replace("{link}", link) if request_message else None
        log(db, v, "claude", "created vendor request", "Checklist: " + ", ".join(ITEMS[i]["label"] for i in items))
        db.commit()
        return {"id": v.id, "vendor_name": v.vendor_name, "status": v.status, "public_link": link,
                "request_message": v.request_message, "required_items": items,
                "vendor_page_in_app": f"{_base(ctx)}/vendors/{v.id}",
                "note": "Nothing has been sent. The user sends the link and message themselves."}


@mcp.tool()
def list_vendors(ctx: Context, status: str = "", limit: int = 50) -> dict:
    """List the user's vendors, newest activity first, with status, days in status and a check summary
    (counts of pass/fail/warn/missing). Optional status filter: requested, submitted, needs_fixes, verified,
    approved. Use this to find a vendor's id by name before calling get_vendor or update_vendor."""
    with SessionLocal() as db:
        user = _user(db, ctx)
        q = select(Vendor).where(Vendor.user_id == user.id).order_by(Vendor.updated_at.desc()).limit(max(1, min(limit, 200)))
        if status:
            if status not in STATUSES:
                raise ValueError(f"Unknown status '{status}'. Allowed: {STATUSES}")
            q = q.where(Vendor.status == status)
        base = _base(ctx)
        return {"vendors": [vendor_summary(v, base) for v in db.scalars(q)]}


@mcp.tool()
def get_vendor(ctx: Context, id: int) -> dict:
    """Get one vendor's full record to review it: contact, checklist, the details the vendor submitted (GSTIN,
    PAN, address, bank details...), rule-based check results (each pass/fail/warn/missing with a reason),
    uploaded files (names and sizes; file contents are not available), the user's notes, the last review and
    the audit history. When reviewing, explain problems in plain language, then save your findings and a
    drafted follow-up with add_review_note."""
    with SessionLocal() as db:
        user = _user(db, ctx)
        return vendor_detail(_vendor(db, user, id), _base(ctx))


@mcp.tool()
def update_vendor(ctx: Context, id: int, status: str = "", required_items: list[str] | None = None,
                  request_message: str = "", notes: str = "", fields: dict[str, str] | None = None) -> dict:
    """Change a vendor when the user asks: status (requested, submitted, needs_fixes, verified, approved),
    the checklist (required_items, full replacement list of keys), the request message, the user's notes, or
    submitted fields (keys: legal_name, trade_name, gstin, pan, address, bank_account_name,
    bank_account_number, ifsc, bank_name, msme_number) when the user gives a correction. Only pass what
    should change. Every change is logged in the history as made by Claude. Do not set 'approved' unless
    the user explicitly asks."""
    with SessionLocal() as db:
        user = _user(db, ctx)
        v = _vendor(db, user, id)
        if fields:
            unknown = [k for k in fields if k not in FIELD_NAMES]
            if unknown:
                raise ValueError(f"Unknown fields {unknown}. Allowed: {FIELD_NAMES}")
        if status:
            set_status(db, v, status, "claude")
        if required_items is not None:
            v.required_items = validate_items(required_items)
            log(db, v, "claude", "changed checklist", ", ".join(ITEMS[i]["label"] for i in v.required_items))
        if request_message:
            v.request_message = request_message.replace("{link}", f"{_base(ctx)}/v/{v.public_token}")
            log(db, v, "claude", "updated request message")
        if notes:
            v.notes = notes
            log(db, v, "claude", "updated notes")
        if fields:
            changed = save_fields(v, fields)
            if changed:
                log(db, v, "claude", "edited fields", ", ".join(changed))
        v.updated_at = now()
        db.commit()
        return vendor_detail(v, _base(ctx))


@mcp.tool()
def add_review_note(ctx: Context, id: int, findings: str, suggested_status: str = "",
                    follow_up_message: str = "") -> dict:
    """Save your review of a vendor's submission so the user sees it in the app. findings: your plain-language
    explanation of what is fine and what is wrong or missing (based on get_vendor). follow_up_message: a message
    you draft for the user to send the vendor asking for the fixes (the user sends it; nothing is sent).
    suggested_status: optionally move the vendor to needs_fixes or verified when your review supports it;
    never 'approved' (approval is the user's call)."""
    with SessionLocal() as db:
        user = _user(db, ctx)
        v = _vendor(db, user, id)
        if suggested_status == "approved":
            raise ValueError("Approval is the user's decision. Suggest 'verified' instead, or ask the user.")
        v.review_findings = findings
        v.follow_up_message = follow_up_message or None
        v.reviewed_at = now()
        log(db, v, "claude", "added review note", findings[:280])
        if suggested_status:
            set_status(db, v, suggested_status, "claude")
        v.updated_at = now()
        db.commit()
        return {"saved": True, "id": v.id, "status": v.status, "vendor_page_in_app": f"{_base(ctx)}/vendors/{v.id}",
                "note": "Review saved. The follow-up message has not been sent; the user sends it."}
