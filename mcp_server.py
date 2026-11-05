"""Remote MCP server (Streamable HTTP). `mcp` is the open Model Context Protocol SDK, not an AI client:
the app makes no model calls. Claude, running in the customer's own account, calls these tools."""
from sqlalchemy import func, select

from checks import ITEMS
from invoices import invoice_dict
from models import FIELD_NAMES, INVOICE_STATUSES, STATUSES, Invoice, SessionLocal, User, Vendor, now
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from services import (base_url, days_since, log, save_fields, set_invoice_status, set_status, validate_items,
                      vendor_detail, vendor_summary)

INSTRUCTIONS = f"""VendorDesk keeps the customer's vendor records: a checklist and a public link per vendor, the
vendor's submitted details and documents, rule-based checks (GSTIN checksum, PAN, IFSC, account number, state and
name matching) and an audit history. After onboarding is approved, the vendor uses the same link every month to send
invoices and see payment status; invoices get rule checks too (duplicate number, vendor approved, standard GST rate,
date, document attached). The app has no AI: you do the thinking and writing.
- Write every message yourself (request and follow-up messages) and pass it to the tools.
- VendorDesk never sends messages. Never say a message was sent; give the user the link and message to send.
- If vendor details needed for a tool are missing (e.g. the vendor's name), ask the user. Do not guess or invent
  contact details, GSTIN, PAN or bank details.
- Checklist keys for required_items: {", ".join(ITEMS)}.
- Vendor statuses: {", ".join(STATUSES)}. Invoice statuses: {", ".join(INVOICE_STATUSES)}.
- Approving a vendor, approving an invoice for payment and marking an invoice paid are the user's decisions: only do
  them when the user explicitly asks. VendorDesk does not move money; 'paid' only records a payment the user made.
- When reviewing invoices, compare the amount with the vendor's earlier invoices and point out anything unusual."""

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
        invs = db.execute(select(Invoice, Vendor).join(Vendor).where(Vendor.user_id == user.id,
                                                                      Invoice.status.in_(["submitted", "approved"]))).all()
        for inv, v in invs:
            if inv.status == "submitted":
                attention.append({"id": v.id, "vendor_name": v.vendor_name, "invoice_id": inv.id,
                                  "why": f"Invoice {inv.invoice_number} ({inv.total_paise / 100:,.2f}) awaiting review"})
        return {"connected": True, "business_name": user.business_name,
                "counts": {s: counts.get(s, 0) for s in STATUSES}, "needs_attention": attention,
                "invoices": {"to_review": sum(1 for i, _ in invs if i.status == "submitted"),
                             "approved_unpaid": sum(1 for i, _ in invs if i.status == "approved"),
                             "approved_unpaid_total": sum(i.total_paise for i, _ in invs if i.status == "approved") / 100},
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


@mcp.tool()
def list_invoices(ctx: Context, status: str = "", vendor_id: int = 0, limit: int = 50) -> dict:
    """List invoices that vendors sent through their VendorDesk link, newest first. Each has invoice number, date,
    taxable/GST/total amounts, status (submitted = to review, approved = approved for payment, paid, rejected),
    rule-based check results, any review note, payment date/reference, and the vendor's earlier invoice totals so
    you can spot unusual amounts. Filter by status and/or vendor_id. Use this when the user asks about invoices,
    bills, what to pay, or payment status."""
    with SessionLocal() as db:
        user = _user(db, ctx)
        q = (select(Invoice, Vendor).join(Vendor).where(Vendor.user_id == user.id)
             .order_by(Invoice.id.desc()).limit(max(1, min(limit, 200))))
        if status:
            if status not in INVOICE_STATUSES:
                raise ValueError(f"Unknown invoice status '{status}'. Allowed: {INVOICE_STATUSES}")
            q = q.where(Invoice.status == status)
        if vendor_id:
            _vendor(db, user, vendor_id)
            q = q.where(Invoice.vendor_id == vendor_id)
        out = []
        for inv, v in db.execute(q).all():
            d = invoice_dict(inv, v)
            d["vendor_status"] = v.status
            d["vendor_earlier_invoice_totals"] = [o.total_paise / 100 for o in v.invoices
                                                  if o.id != inv.id and o.status != "rejected"][:12]
            out.append(d)
        return {"invoices": out}


@mcp.tool()
def update_invoice(ctx: Context, invoice_id: int, status: str = "", review_note: str = "", paid_on: str = "",
                   payment_reference: str = "") -> dict:
    """Update one invoice. review_note: your plain-language review (what checks out, what looks wrong) shown to
    the user in the app. status: approved (approved for payment), rejected, paid, or submitted (back to review).
    Only set approved, rejected or paid when the user explicitly asks. For paid, pass paid_on (YYYY-MM-DD, default
    today) and payment_reference (e.g. UTR) if the user gave them. VendorDesk does not move money: paid records a
    payment the user made, and the vendor sees it on their link. Changes are logged as made by Claude."""
    with SessionLocal() as db:
        user = _user(db, ctx)
        inv = db.get(Invoice, invoice_id)
        v = db.get(Vendor, inv.vendor_id) if inv else None
        if not inv or v.user_id != user.id:
            raise ValueError(f"No invoice with id {invoice_id}. Use list_invoices to find the right id.")
        if review_note:
            inv.review_note = review_note
            log(db, v, "claude", "reviewed invoice", f"{inv.invoice_number}: {review_note[:200]}")
            inv.updated_at = v.updated_at = now()
        if status:
            set_invoice_status(db, v, inv, status, "claude", paid_on, payment_reference)
        db.commit()
        return invoice_dict(inv, v) | {"vendor_page_in_app": f"{_base(ctx)}/vendors/{v.id}"}
