"""Invoice rules and helpers. Plain code only: parsing, duplicate lookup, rate comparison, date checks."""
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

# GST slabs: current (0, 5, 18, 40 after the 2025 rationalisation) plus older 12/28 and special 0.25/3 rates.
GST_RATES = [0, 0.25, 3, 5, 12, 18, 28, 40]
INVOICE_STATUS_LABELS = {"submitted": "To review", "approved": "Approved for payment", "paid": "Paid",
                         "rejected": "Rejected"}


def parse_amount(text, field):
    """'12,500.50' / '₹ 12500' -> paise. Raises ValueError with a human message."""
    cleaned = re.sub(r"[₹,\s]|rs\.?|inr", "", (text or "").strip(), flags=re.I)
    if not cleaned:
        raise ValueError(f"{field} is required.")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"{field} must be a number, like 12500 or 12,500.50.")
    if value < 0 or value >= Decimal("10000000000"):
        raise ValueError(f"{field} must be between 0 and 999,99,99,999.")
    if value != value.quantize(Decimal("0.01")):
        raise ValueError(f"{field} can have at most 2 decimal places.")
    return int(value * 100)


def parse_date(text):
    try:
        return date.fromisoformat((text or "").strip()).isoformat()
    except ValueError:
        raise ValueError("Invoice date must be a valid date (YYYY-MM-DD).")


def norm_number(n):
    return re.sub(r"[\s/_-]", "", (n or "")).upper()


def inr(paise):
    """Indian digit grouping: 1234567.5 -> ₹12,34,567.50"""
    if paise is None:
        return ""
    neg, paise = paise < 0, abs(paise)
    rupees, p = divmod(paise, 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
        s = head + "," + tail
    return f"{'-' if neg else ''}₹{s}.{p:02d}"


def invoice_checks(inv, vendor, today=None):
    """Returns [{key, label, status: pass|fail|warn|missing, reason}] for one invoice."""
    today = today or date.today()
    out = []

    def add(key, label, status, reason):
        out.append({"key": key, "label": label, "status": status, "reason": reason})

    dupes = [o for o in vendor.invoices if o.id != inv.id and o.status != "rejected"
             and norm_number(o.invoice_number) == norm_number(inv.invoice_number)]
    if dupes:
        add("duplicate", "Duplicate invoice number", "fail",
            f"Invoice {inv.invoice_number} was already submitted on {dupes[-1].created_at.date().isoformat()}.")
    else:
        add("duplicate", "Duplicate invoice number", "pass", "No other invoice with this number.")

    if vendor.status == "approved":
        add("vendor", "Vendor approved", "pass", "Vendor onboarding is approved.")
    else:
        add("vendor", "Vendor approved", "fail", "Vendor is not approved. Don't pay before onboarding is approved.")

    if inv.taxable_paise <= 0:
        add("gst_rate", "GST rate", "fail", "Taxable amount must be more than zero.")
    else:
        rate = inv.gst_paise * 100 / inv.taxable_paise
        nearest = min(GST_RATES, key=lambda r: abs(r - rate))
        # allow rounding of up to ₹1 on the GST amount
        if abs(inv.taxable_paise * nearest / 100 - inv.gst_paise) <= 100:
            add("gst_rate", "GST rate", "pass", f"GST is {nearest:g}% of the taxable amount.")
        else:
            add("gst_rate", "GST rate", "warn",
                f"GST is {rate:.2f}% of the taxable amount, not a standard rate ({', '.join(f'{r:g}' for r in GST_RATES)}%).")

    d = date.fromisoformat(inv.invoice_date)
    if d > today:
        add("date", "Invoice date", "fail", f"Date {d.isoformat()} is in the future.")
    elif d < today - timedelta(days=180):
        add("date", "Invoice date", "warn", f"Invoice is more than 6 months old ({d.isoformat()}).")
    else:
        add("date", "Invoice date", "pass", f"Dated {d.strftime('%d %b %Y')}.")

    if inv.filename:
        add("file", "Invoice document", "pass", f"Attached: {inv.filename}.")
    else:
        add("file", "Invoice document", "missing", "No invoice PDF or photo attached.")
    order = {"fail": 0, "missing": 1, "warn": 2, "pass": 3}
    return sorted(out, key=lambda c: order[c["status"]])


def invoice_dict(inv, vendor):
    checks = invoice_checks(inv, vendor)
    return {"invoice_id": inv.id, "vendor_id": vendor.id, "vendor_name": vendor.vendor_name,
            "invoice_number": inv.invoice_number, "invoice_date": inv.invoice_date,
            "taxable_amount": inv.taxable_paise / 100, "gst_amount": inv.gst_paise / 100,
            "total": inv.total_paise / 100, "total_display": inr(inv.total_paise), "status": inv.status,
            "review_note": inv.review_note, "paid_on": inv.paid_on, "payment_reference": inv.payment_reference,
            "file": {"filename": inv.filename, "size_bytes": inv.size} if inv.filename else None,
            "check_results": checks, "submitted_at": inv.created_at.isoformat(timespec="seconds")}


if __name__ == "__main__":
    assert inr(123456789) == "₹12,34,567.89", inr(123456789)
    assert inr(50000) == "₹500.00" and inr(100000000) == "₹10,00,000.00"
    assert parse_amount("12,500.50", "x") == 1250050 and parse_amount("₹ 1800", "x") == 180000
    for bad in ("", "abc", "1.234", "-5"):
        try:
            parse_amount(bad, "Amount"); raise SystemExit(f"accepted {bad!r}")
        except ValueError:
            pass
    print("invoices ok")
