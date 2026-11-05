"""VendorDesk web app. Plain FastAPI + Jinja2. No AI: Claude connects from outside through the MCP server."""
import csv
import io
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, timedelta
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.middleware.sessions import SessionMiddleware

from checks import FIELD_LABELS, ITEMS
from mcp_server import mcp
from invoices import INVOICE_STATUS_LABELS, inr, invoice_checks, parse_amount, parse_date
from models import (FIELD_NAMES, FILE_KINDS, INVOICE_STATUSES, STATUSES, Event, Invoice, SessionLocal, User, Vendor,
                    VendorFile, hash_password, init_db, now, verify_password)
from services import (STATUS_LABELS, base_url, ensure_fields, log, mask, public_link, save_fields, set_status,
                      set_invoice_status, share_links, vendor_checks, vendor_summary)
from checks import summarize

# Format hints shown under fields: (rule, made-up example that passes every check).
FIELD_HINTS = {
    "legal_name": ("As on the GST certificate", "Sharma Packaging Pvt Ltd"),
    "gstin": ("15 characters: 2-digit state code + PAN + 1Z + check character", "27ABCCS1234K1ZD"),
    "pan": ("5 letters, 4 digits, 1 letter. Must match characters 3-12 of the GSTIN", "ABCCS1234K"),
    "address": (None, "Plot 14, MIDC Bhosari, Pune, Maharashtra 411026"),
    "bank_account_name": ("Name exactly as on the cheque", "Sharma Packaging Pvt Ltd"),
    "bank_account_number": ("9 to 18 digits, no spaces", "50100123456789"),
    "ifsc": ("11 characters: 4 letters, 0, then 6 letters/digits", "HDFC0001234"),
    "bank_name": (None, "HDFC Bank"),
    "msme_number": ("Udyam registration number", "UDYAM-MH-26-0012345"),
}

MAX_FILE = 5 * 1024 * 1024
ALLOWED_TYPES = {"pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}
MAGIC = {"application/pdf": b"%PDF", "image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG"}
FILE_LABELS = {"gst_certificate": "GST certificate", "pan_card": "PAN card", "cancelled_cheque": "Cancelled cheque",
               "msme": "MSME / Udyam certificate", "other": "Other document"}


class TokenPathMiddleware:
    """Rewrites /mcp/<token> to /mcp and keeps the token in scope["mcp_token"] for the MCP tools.
    The scope is changed in place so the server's access log shows /mcp, never the secret token."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp/"):
            token = scope["path"][len("/mcp/"):].strip("/")
            scope["mcp_token"] = token
            scope["path"] = "/mcp"
            scope["raw_path"] = b"/mcp"
        await self.app(scope, receive, send)


mcp_app = mcp.streamable_http_app()  # must be created before session_manager.run()


@asynccontextmanager
async def lifespan(_app):
    init_db()
    async with mcp.session_manager.run():
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TokenPathMiddleware)
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
                   same_site="lax", https_only=os.environ.get("PUBLIC_URL", "").startswith("https"),
                   max_age=14 * 24 * 3600)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
templates.env.globals.update(STATUS_LABELS=STATUS_LABELS, STATUSES=STATUSES, FIELD_LABELS=FIELD_LABELS,
                             FILE_LABELS=FILE_LABELS, ITEMS=ITEMS, mask=mask, INVOICE_STATUS_LABELS=INVOICE_STATUS_LABELS,
                             invoice_checks=invoice_checks, summarize=summarize, FIELD_HINTS=FIELD_HINTS)


def ist(dt):
    return (dt + timedelta(hours=5, minutes=30)).strftime("%d %b, %I:%M %p") if dt else ""


templates.env.filters["ist"] = ist
templates.env.filters["inr"] = inr


def check_upload(filename, data):
    """Returns (content_type, error). Only real PDF, JPG or PNG files up to 5 MB."""
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    ctype = ALLOWED_TYPES.get(ext)
    if not ctype:
        return None, "Only PDF, JPG or PNG files are accepted."
    if len(data) > MAX_FILE:
        return None, "File is larger than 5 MB. Please upload a smaller file."
    if not data.startswith(MAGIC[ctype]):
        return None, "That file does not look like a real PDF, JPG or PNG."
    return ctype, None


def safe_name(name):
    return "".join(c for c in (name or "") if c.isalnum() or c in "._- ") or "file"


@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")  # public links carry tokens
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


def render(request, name, **ctx):
    return templates.TemplateResponse(request, name, ctx)


def current_user(request, db):
    uid = request.session.get("uid")
    return db.get(User, uid) if uid else None


def require_user(request, db):
    user = current_user(request, db)
    if not user:
        raise HTTPException(303, headers={"Location": "/"})
    return user


def own_vendor(db, user, vendor_id):
    v = db.get(Vendor, vendor_id)
    if not v or v.user_id != user.id:
        raise HTTPException(404, "Vendor not found")
    return v


def base(request):
    return base_url(request.base_url)


# ---------- auth ----------

@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    with SessionLocal() as db:
        if current_user(request, db):
            return RedirectResponse("/dashboard", 303)
    return render(request, "landing.html", error=request.query_params.get("error"),
                  tab=request.query_params.get("tab", "login"))


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username.strip().lower()))
        if not user or not verify_password(password, user.password_hash):
            return RedirectResponse("/?error=Wrong+username+or+password", 303)
        request.session["uid"] = user.id
    return RedirectResponse("/dashboard", 303)


@app.post("/register")
def register(request: Request, username: str = Form(...), password: str = Form(...),
             business_name: str = Form(...)):
    username = username.strip().lower()
    if not username.replace("_", "").replace(".", "").isalnum() or not 3 <= len(username) <= 40:
        return RedirectResponse("/?tab=register&error=Username:+3-40+letters,+digits,+dot+or+underscore", 303)
    if len(password) < 8:
        return RedirectResponse("/?tab=register&error=Password+must+be+at+least+8+characters", 303)
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.username == username)):
            return RedirectResponse("/?tab=register&error=That+username+is+taken", 303)
        user = User(username=username, password_hash=hash_password(password), token=secrets.token_urlsafe(24),
                    business_name=business_name.strip()[:200] or username)
        db.add(user)
        db.commit()
        request.session["uid"] = user.id
    return RedirectResponse("/dashboard", 303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", 303)


# ---------- dashboard ----------

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    with SessionLocal() as db:
        user = require_user(request, db)
        vendors = list(db.scalars(select(Vendor).where(Vendor.user_id == user.id).order_by(Vendor.updated_at.desc())))
        b = base(request)
        rows = [vendor_summary(v, b) | {"created_by": v.created_by, "contact_phone": v.contact_phone}
                for v in vendors]
        counts = {s: sum(1 for r in rows if r["status"] == s) for s in STATUSES}
        invs = [i for v in vendors for i in v.invoices]
        month = date.today().isoformat()[:7]
        inv_stats = {"to_review": sum(1 for i in invs if i.status == "submitted"),
                     "to_pay": sum(i.total_paise for i in invs if i.status == "approved"),
                     "to_pay_n": sum(1 for i in invs if i.status == "approved"),
                     "paid_month": sum(i.total_paise for i in invs if i.status == "paid" and (i.paid_on or "")[:7] == month)}
        for r, v in zip(rows, vendors):
            r["invoices_to_review"] = sum(1 for i in v.invoices if i.status == "submitted")
        return render(request, "dashboard.html", user=user, rows=rows, counts=counts, inv_stats=inv_stats,
                      mcp_url=f"{b}/mcp/{user.token}", stamp=_user_stamp(db, user),
                      local=any(h in b for h in ("127.0.0.1", "localhost", "0.0.0.0")))


def _user_stamp(db, user):
    n, latest = db.execute(select(func.count(Vendor.id), func.max(Vendor.updated_at))
                           .where(Vendor.user_id == user.id)).one()
    events = db.scalar(select(func.count(Event.id)).join(Vendor).where(Vendor.user_id == user.id))
    return f"{n}-{latest.isoformat() if latest else ''}-{events}"


@app.get("/api/stamp")
def stamp(request: Request, vendor: int | None = None):
    """Cheap change marker for polling: pages reload when it changes."""
    with SessionLocal() as db:
        user = current_user(request, db)
        if not user:
            return JSONResponse({"error": "login"}, 401)
        if vendor:
            v = own_vendor(db, user, vendor)
            return {"stamp": f"{v.updated_at.isoformat()}-{len(v.events)}-{len(v.files)}-{len(v.invoices)}"}
        return {"stamp": _user_stamp(db, user)}


@app.get("/api/export.csv")
def export_csv(request: Request):
    with SessionLocal() as db:
        user = require_user(request, db)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["vendor_name", "contact_name", "contact_phone", "contact_email", "category", "state",
                    *FIELD_NAMES, "approved_on"])
        for v in db.scalars(select(Vendor).where(Vendor.user_id == user.id, Vendor.status == "approved")
                            .order_by(Vendor.vendor_name)):
            f = ensure_fields(v).as_dict()
            row = [v.vendor_name, v.contact_name, v.contact_phone, v.contact_email, v.category, v.state,
                   *[f[n] for n in FIELD_NAMES], v.updated_at.date().isoformat()]
            # Prevent spreadsheet formula injection from vendor-typed values.
            w.writerow(["'" + c if isinstance(c, str) and c[:1] in "=+-@" else (c or "") for c in row])
        return Response(out.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="approved-vendors.csv"'})


# ---------- vendor detail (owner) ----------

@app.get("/vendors/{vendor_id}", response_class=HTMLResponse)
def vendor_page(request: Request, vendor_id: int):
    with SessionLocal() as db:
        user = require_user(request, db)
        v = own_vendor(db, user, vendor_id)
        b = base(request)
        checks = vendor_checks(v)
        return render(request, "vendor.html", user=user, v=v, f=ensure_fields(v).as_dict(), checks=checks,
                      summary=summarize(checks), link=public_link(v, b),
                      request_share=share_links(v, v.request_message, b),
                      followup_share=share_links(v, v.follow_up_message, b),
                      stamp=f"{v.updated_at.isoformat()}-{len(v.events)}-{len(v.files)}-{len(v.invoices)}",
                      today=date.today().isoformat(),
                      saved=request.query_params.get("saved"))


@app.post("/vendors/{vendor_id}/fields")
async def edit_fields(request: Request, vendor_id: int):
    form = await request.form()
    with SessionLocal() as db:
        user = require_user(request, db)
        v = own_vendor(db, user, vendor_id)
        changed = save_fields(v, {k: str(form.get(k, "")) for k in FIELD_NAMES})
        if changed:
            log(db, v, "you", "edited fields", ", ".join(FIELD_LABELS[c] for c in changed))
        db.commit()
    return RedirectResponse(f"/vendors/{vendor_id}?saved=fields", 303)


@app.post("/vendors/{vendor_id}/status")
def change_status(request: Request, vendor_id: int, status: str = Form(...)):
    with SessionLocal() as db:
        user = require_user(request, db)
        v = own_vendor(db, user, vendor_id)
        if status not in STATUSES:
            raise HTTPException(400, "Unknown status")
        set_status(db, v, status, "you")
        db.commit()
    return RedirectResponse(f"/vendors/{vendor_id}", 303)


@app.post("/vendors/{vendor_id}/texts")
async def edit_texts(request: Request, vendor_id: int):
    """Saves the editable texts: request message, Claude's findings and follow-up, and notes."""
    form = await request.form()
    names = {"request_message": "request message", "review_findings": "review findings",
             "follow_up_message": "follow-up message", "notes": "notes"}
    with SessionLocal() as db:
        user = require_user(request, db)
        v = own_vendor(db, user, vendor_id)
        for key, label in names.items():
            if key in form:
                value = str(form[key]).strip() or None
                if value != getattr(v, key):
                    setattr(v, key, value)
                    log(db, v, "you", f"edited {label}")
                    v.updated_at = now()
        db.commit()
    return RedirectResponse(f"/vendors/{vendor_id}?saved=texts", 303)


@app.get("/vendors/{vendor_id}/files/{file_id}")
def download_file(request: Request, vendor_id: int, file_id: int):
    with SessionLocal() as db:
        user = require_user(request, db)
        v = own_vendor(db, user, vendor_id)
        f = db.get(VendorFile, file_id)
        if not f or f.vendor_id != v.id:
            raise HTTPException(404)
        return Response(f.data, media_type=f.content_type,
                        headers={"Content-Disposition": f'inline; filename="{safe_name(f.filename)}"',
                                 "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"})


# ---------- public vendor form ----------

def public_vendor(db, token):
    v = db.scalar(select(Vendor).where(Vendor.public_token == token))
    if not v:
        raise HTTPException(404, "This link is not valid. Please ask the business that sent it for a new one.")
    return v


def form_fields(v):
    fields = ["legal_name", "trade_name"]
    for key in v.required_items or []:
        for name in ITEMS[key]["fields"]:
            if name not in fields:
                fields.append(name)
    return fields


def form_uploads(v):
    kinds = [ITEMS[k]["file"] for k in v.required_items or [] if ITEMS.get(k, {}).get("file")]
    return list(dict.fromkeys(kinds)) + ["other"]


def editable(v):
    return v.status in ("requested", "needs_fixes")


def public_state(v):
    checks = vendor_checks(v)
    return {"checks": checks, "summary": summarize(checks), "status": v.status, "editable": editable(v),
            "files": [{"id": f.id, "kind": f.kind, "filename": f.filename, "size": f.size} for f in v.files]}


@app.get("/v/{token}", response_class=HTMLResponse)
def public_form(request: Request, token: str):
    with SessionLocal() as db:
        v = public_vendor(db, token)
        owner = db.get(User, v.user_id)
        return render(request, "public_form.html", v=v, owner=owner, f=ensure_fields(v).as_dict(),
                      fields=form_fields(v), uploads=form_uploads(v), state=public_state(v),
                      today=date.today().isoformat(), inv_sent=request.query_params.get("inv") == "sent",
                      inv_error=request.query_params.get("inv_error"))


@app.post("/v/{token}/save")
async def public_save(request: Request, token: str):
    data = await request.json()
    with SessionLocal() as db:
        v = public_vendor(db, token)
        if not editable(v):
            return JSONResponse({"error": "This form has been submitted and is locked."}, 409)
        allowed = form_fields(v)
        changed = save_fields(v, {k: str(data.get(k) or "") for k in allowed if k in data})
        if changed:
            labels = [FIELD_LABELS[c] for c in changed]
            last = v.events[0] if v.events else None
            # Autosave runs as the vendor types: fold repeated saves into one history row.
            if last and last.actor == "vendor" and last.action == "saved details" and \
                    now() - last.created_at < timedelta(minutes=15):
                prev = (last.detail or "").split(", ")
                last.detail = ", ".join(dict.fromkeys([p for p in prev if p] + labels))
                last.created_at = now()
            else:
                log(db, v, "vendor", "saved details", ", ".join(labels))
        db.commit()
        return public_state(v)


@app.post("/v/{token}/upload")
async def public_upload(token: str, kind: str = Form(...), file: UploadFile = File(...)):
    data = await file.read(MAX_FILE + 1)
    with SessionLocal() as db:
        v = public_vendor(db, token)
        if not editable(v):
            return JSONResponse({"error": "This form has been submitted and is locked."}, 409)
        if kind not in FILE_KINDS:
            return JSONResponse({"error": "Unknown document type."}, 400)
        ctype, error = check_upload(file.filename, data)
        if error:
            return JSONResponse({"error": error}, 400)
        ext = file.filename.rsplit(".", 1)[-1].lower()
        if kind != "other":  # one file per document type; a new upload replaces the old one
            for old in [f for f in v.files if f.kind == kind]:
                v.files.remove(old)
        name = os.path.basename(file.filename or f"{kind}.{ext}")[:200]
        v.files.append(VendorFile(kind=kind, filename=name, content_type=ctype, size=len(data), data=data))
        log(db, v, "vendor", "uploaded file", f"{FILE_LABELS[kind]}: {name}")
        v.updated_at = now()
        db.commit()
        return public_state(v)


@app.post("/v/{token}/files/{file_id}/delete")
def public_delete_file(token: str, file_id: int):
    with SessionLocal() as db:
        v = public_vendor(db, token)
        if not editable(v):
            return JSONResponse({"error": "This form has been submitted and is locked."}, 409)
        f = next((f for f in v.files if f.id == file_id), None)
        if not f:
            return JSONResponse({"error": "File not found."}, 404)
        v.files.remove(f)
        log(db, v, "vendor", "removed file", f"{FILE_LABELS[f.kind]}: {f.filename}")
        v.updated_at = now()
        db.commit()
        return public_state(v)


@app.post("/v/{token}/submit")
def public_submit(token: str):
    with SessionLocal() as db:
        v = public_vendor(db, token)
        if not editable(v):
            return JSONResponse({"error": "Already submitted."}, 409)
        s = summarize(vendor_checks(v))
        set_status(db, v, "submitted", "vendor")
        v.submitted_at = now()
        log(db, v, "vendor", "submitted form",
            f"{s['pass']} passed, {s['fail']} failed, {s['warn']} warnings, {s['missing']} missing")
        db.commit()
        return public_state(v)


# ---------- invoices ----------

@app.post("/v/{token}/invoices")
async def public_invoice(token: str, invoice_number: str = Form(""), invoice_date: str = Form(""),
                         taxable_amount: str = Form(""), gst_amount: str = Form("0"),
                         file: UploadFile | None = File(None)):
    back = f"/v/{token}"
    data = await file.read(MAX_FILE + 1) if file and file.filename else b""
    with SessionLocal() as db:
        v = public_vendor(db, token)
        try:
            if v.status != "approved":
                raise ValueError("Invoices can be sent once your onboarding is approved.")
            number = invoice_number.strip()[:60]
            if not number:
                raise ValueError("Invoice number is required.")
            inv = Invoice(invoice_number=number, invoice_date=parse_date(invoice_date),
                          taxable_paise=parse_amount(taxable_amount, "Taxable amount"),
                          gst_paise=parse_amount(gst_amount or "0", "GST amount"))
            if data:
                ctype, error = check_upload(file.filename, data)
                if error:
                    raise ValueError(error)
                inv.filename, inv.content_type, inv.size, inv.data = (
                    os.path.basename(file.filename)[:200], ctype, len(data), data)
        except ValueError as e:
            return RedirectResponse(f"{back}?inv_error={quote(str(e))}#invoices", 303)
        v.invoices.append(inv)
        log(db, v, "vendor", "submitted invoice", f"{inv.invoice_number} · {inr(inv.total_paise)}")
        v.updated_at = now()
        db.commit()
    return RedirectResponse(f"{back}?inv=sent#invoices", 303)


def own_invoice(db, user, vendor_id, invoice_id):
    v = own_vendor(db, user, vendor_id)
    inv = db.get(Invoice, invoice_id)
    if not inv or inv.vendor_id != v.id:
        raise HTTPException(404, "Invoice not found")
    return v, inv


@app.post("/vendors/{vendor_id}/invoices/{invoice_id}/status")
def invoice_status(request: Request, vendor_id: int, invoice_id: int, status: str = Form(...),
                   paid_on: str = Form(""), payment_reference: str = Form("")):
    with SessionLocal() as db:
        user = require_user(request, db)
        v, inv = own_invoice(db, user, vendor_id, invoice_id)
        try:
            set_invoice_status(db, v, inv, status, "you", paid_on, payment_reference)
        except ValueError as e:
            raise HTTPException(400, str(e))
        db.commit()
    return RedirectResponse(f"/vendors/{vendor_id}#inv-{invoice_id}", 303)


@app.post("/vendors/{vendor_id}/invoices/{invoice_id}/note")
def invoice_note(request: Request, vendor_id: int, invoice_id: int, review_note: str = Form("")):
    with SessionLocal() as db:
        user = require_user(request, db)
        v, inv = own_invoice(db, user, vendor_id, invoice_id)
        value = review_note.strip() or None
        if value != inv.review_note:
            inv.review_note = value
            log(db, v, "you", "edited invoice note", inv.invoice_number)
            inv.updated_at = v.updated_at = now()
        db.commit()
    return RedirectResponse(f"/vendors/{vendor_id}#inv-{invoice_id}", 303)


@app.get("/vendors/{vendor_id}/invoices/{invoice_id}/file")
def invoice_file(request: Request, vendor_id: int, invoice_id: int):
    with SessionLocal() as db:
        user = require_user(request, db)
        _, inv = own_invoice(db, user, vendor_id, invoice_id)
        if not inv.filename:
            raise HTTPException(404)
        return Response(inv.data, media_type=inv.content_type,
                        headers={"Content-Disposition": f'inline; filename="{safe_name(inv.filename)}"',
                                 "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"})


@app.get("/api/payments.csv")
def payments_csv(request: Request):
    """Payment file: invoices approved for payment, with the vendor's checked bank details."""
    with SessionLocal() as db:
        user = require_user(request, db)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["vendor_name", "invoice_number", "invoice_date", "taxable_amount", "gst_amount", "total",
                    "account_name", "account_number", "ifsc", "bank_name", "gstin", "vendor_status"])
        rows = db.execute(select(Invoice, Vendor).join(Vendor).where(Vendor.user_id == user.id,
                                                                     Invoice.status == "approved")
                          .order_by(Vendor.vendor_name, Invoice.invoice_date)).all()
        for inv, v in rows:
            f = ensure_fields(v)
            row = [v.vendor_name, inv.invoice_number, inv.invoice_date, f"{inv.taxable_paise / 100:.2f}",
                   f"{inv.gst_paise / 100:.2f}", f"{inv.total_paise / 100:.2f}", f.bank_account_name,
                   f.bank_account_number, f.ifsc, f.bank_name, f.gstin, v.status]
            w.writerow(["'" + c if isinstance(c, str) and c[:1] in "=+-@" else (c or "") for c in row])
        return Response(out.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="payment-file.csv"'})


@app.get("/healthz")
def healthz():
    with SessionLocal() as db:
        db.execute(select(1))
    return {"ok": True}


# MCP last: /mcp (after the token rewrite) falls through to it; any other unknown path gets its 404.
app.mount("/", mcp_app)
