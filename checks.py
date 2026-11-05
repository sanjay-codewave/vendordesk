"""Rule-based checks. Plain code only: regex, checksums and field comparisons."""
import re

# Checklist items Claude can request. Each is satisfied by all its fields being filled, or by an upload of file_kind.
ITEMS = {
    "legal_name": {"label": "Legal business name", "fields": ["legal_name"], "file": None},
    "address": {"label": "Registered address", "fields": ["address"], "file": None},
    "gstin": {"label": "GSTIN", "fields": ["gstin"], "file": None},
    "pan": {"label": "PAN", "fields": ["pan"], "file": None},
    "bank_details": {"label": "Bank details",
                     "fields": ["bank_account_name", "bank_account_number", "ifsc", "bank_name"], "file": None},
    "msme": {"label": "MSME / Udyam number or certificate", "fields": ["msme_number"], "file": "msme"},
    "gst_certificate": {"label": "GST certificate (upload)", "fields": [], "file": "gst_certificate"},
    "pan_card": {"label": "PAN card (upload)", "fields": [], "file": "pan_card"},
    "cancelled_cheque": {"label": "Cancelled cheque (upload)", "fields": [], "file": "cancelled_cheque"},
}

FIELD_LABELS = {
    "legal_name": "Legal business name", "trade_name": "Trade name (if different)", "gstin": "GSTIN",
    "pan": "PAN", "address": "Registered address", "bank_account_name": "Account holder name",
    "bank_account_number": "Account number", "ifsc": "IFSC", "bank_name": "Bank name",
    "msme_number": "MSME / Udyam number",
}

GST_STATES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "25": "Daman and Diu", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
    "28": "Andhra Pradesh", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman and Nicobar Islands", "36": "Telangana",
    "37": "Andhra Pradesh", "38": "Ladakh", "97": "Other Territory", "99": "Centre Jurisdiction",
}

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
ACCOUNT_RE = re.compile(r"^[0-9]{9,18}$")
CH = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# 4th PAN character = holder status (Income Tax Department).
PAN_HOLDER_TYPES = {"P": "individual", "C": "company", "H": "HUF", "F": "firm / LLP", "A": "association of persons",
                    "B": "body of individuals", "T": "trust", "G": "government", "J": "artificial juridical person",
                    "L": "local authority"}
UPPER_FIELDS = {"gstin", "pan", "ifsc", "bank_account_number", "msme_number"}


def normalize(name, value):
    value = (value or "").strip()
    if name in UPPER_FIELDS:
        value = re.sub(r"\s+", "", value).upper()
    return value or None


def gstin_check_digit(g14):
    total = 0
    for i, c in enumerate(g14):
        v = CH.index(c) * (1 if i % 2 == 0 else 2)
        total += v // 36 + v % 36
    return CH[(36 - total % 36) % 36]


def _simple(s):
    s = (s or "").lower().replace("&", "and")
    return re.sub(r"[^a-z]", "", s)


def _company(s):
    s = re.sub(r"\b(m/s|ms|pvt|private|ltd|limited|llp|the|and|co|company)\b\.?", " ", (s or "").lower())
    return _simple(s)


def item_satisfied(key, fields, file_kinds):
    item = ITEMS.get(key)
    if not item:
        return False
    if item["fields"] and all(fields.get(f) for f in item["fields"]):
        return True
    return bool(item["file"] and item["file"] in file_kinds)


def run_checks(fields, declared_state, required_items, file_kinds):
    """Returns a list of {key, label, status: pass|fail|warn|missing, reason}."""
    out = []

    def add(key, label, status, reason):
        out.append({"key": key, "label": label, "status": status, "reason": reason})

    for key in required_items:
        label = ITEMS.get(key, {}).get("label", key)
        if item_satisfied(key, fields, file_kinds):
            add(f"item:{key}", label, "pass", "Provided.")
        else:
            add(f"item:{key}", label, "missing", "Not provided yet.")

    g, pan, ifsc, acct = fields.get("gstin"), fields.get("pan"), fields.get("ifsc"), fields.get("bank_account_number")
    gst_ok = False
    if g:
        if len(g) != 15:
            add("gstin", "GSTIN format", "fail", f"GSTIN must be 15 characters (got {len(g)}).")
        elif not GSTIN_RE.match(g):
            add("gstin", "GSTIN format", "fail", "GSTIN pattern is wrong (expected 2 digits, PAN, entity code, Z, check).")
        elif g[:2] not in GST_STATES:
            add("gstin", "GSTIN format", "fail", f"State code {g[:2]} is not a valid GST state code.")
        elif gstin_check_digit(g[:14]) != g[14]:
            add("gstin", "GSTIN format", "fail", "Check digit does not match. A character is likely mistyped.")
        else:
            gst_ok = True
            add("gstin", "GSTIN format", "pass", f"Valid format and check digit ({GST_STATES[g[:2]]}).")

    pan_ok = False
    if pan:
        if not PAN_RE.match(pan):
            add("pan", "PAN format", "fail", "PAN must be 5 letters, 4 digits, 1 letter (e.g. ABCCS1234K).")
        elif pan[3] not in PAN_HOLDER_TYPES:
            add("pan", "PAN format", "fail", f"4th letter '{pan[3]}' is not a valid PAN holder type "
                "(P person, C company, F firm, H HUF, A/B/T/G/J/L other). A letter is likely mistyped.")
        else:
            pan_ok = True
            add("pan", "PAN format", "pass", f"Valid format ({PAN_HOLDER_TYPES[pan[3]]}).")

    if gst_ok and pan_ok:
        same = g[2:12] == pan
        add("pan_in_gstin", "PAN matches GSTIN", "pass" if same else "fail",
            "PAN is embedded in the GSTIN." if same else f"GSTIN contains PAN {g[2:12]}, but PAN entered is {pan}.")

    if ifsc:
        ok = bool(IFSC_RE.match(ifsc))
        add("ifsc", "IFSC format", "pass" if ok else "fail",
            f"Valid format (bank code {ifsc[:4]})." if ok else "IFSC must be 4 letters, a zero, then 6 letters/digits.")

    if acct:
        ok = bool(ACCOUNT_RE.match(acct))
        add("account", "Account number", "pass" if ok else "fail",
            "9 to 18 digits." if ok else "Account number must be 9 to 18 digits only.")

    if gst_ok and declared_state:
        code = g[:2]
        if code in ("97", "99"):
            add("state", "State matches GSTIN", "warn", f"GSTIN uses special code {code}; check state manually.")
        elif _simple(GST_STATES[code]) == _simple(declared_state):
            add("state", "State matches GSTIN", "pass", f"Both say {GST_STATES[code]}.")
        else:
            add("state", "State matches GSTIN", "warn",
                f"Vendor declared {declared_state}, but GSTIN is registered in {GST_STATES[code]}.")

    holder = fields.get("bank_account_name")
    names = [n for n in (fields.get("legal_name"), fields.get("trade_name")) if n]
    if holder and names:
        if any(_company(holder) == _company(n) for n in names):
            add("holder", "Account name matches business", "pass", "Account holder matches the business name.")
        else:
            add("holder", "Account name matches business", "warn",
                f"Account holder \"{holder}\" differs from the business name \"{names[0]}\".")
    return out


def summarize(checks):
    counts = {s: 0 for s in ("pass", "fail", "warn", "missing")}
    for c in checks:
        counts[c["status"]] += 1
    return counts


if __name__ == "__main__":  # self-check: python checks.py
    good = "27ABCCS1234K1Z" + gstin_check_digit("27ABCCS1234K1Z")
    f = {"gstin": good, "pan": "ABCCS1234K", "ifsc": "HDFC0001234", "bank_account_number": "123456789012",
         "bank_account_name": "Sharma Packaging Pvt Ltd", "legal_name": "M/s Sharma Packaging Private Limited"}
    r = {c["key"]: c["status"] for c in run_checks(f, "Maharashtra", ["gstin", "pan", "cancelled_cheque"], set())}
    assert r == {"item:gstin": "pass", "item:pan": "pass", "item:cancelled_cheque": "missing", "gstin": "pass",
                 "pan": "pass", "pan_in_gstin": "pass", "ifsc": "pass", "account": "pass", "state": "pass",
                 "holder": "pass"}, r
    bad = {"gstin": good[:14] + ("A" if good[14] != "A" else "B"), "pan": "ABCCS1234X", "ifsc": "HDFC1001234",
           "bank_account_number": "12AB"}
    r = {c["key"]: c["status"] for c in run_checks(bad, "Karnataka", [], set())}
    assert r == {"gstin": "fail", "pan": "pass", "ifsc": "fail", "account": "fail"}, r
    r = {c["key"]: c["status"] for c in run_checks({"gstin": good, "pan": "ZZZCZ1234F"}, "Karnataka", [], set())}
    assert r == {"gstin": "pass", "pan": "pass", "pan_in_gstin": "fail", "state": "warn"}, r
    r = {c["key"]: c["status"] for c in run_checks({"pan": "ABCDS1234K"}, None, [], set())}
    assert r == {"pan": "fail"}, r  # 4th letter D is not a holder type
    for real in ("27AAACR5055K1Z7", "29AAACI4798L1ZU", "33AAACI4798L3Z3", "35AAACR5055K1ZA"):  # public GSTINs
        r = {c["key"]: c["status"] for c in run_checks({"gstin": real, "pan": real[2:12]}, None, [], set())}
        assert r == {"gstin": "pass", "pan": "pass", "pan_in_gstin": "pass"}, (real, r)
    assert gstin_check_digit("27AAPFU0939F1Z") == "V"  # widely published sample GSTIN 27AAPFU0939F1ZV
    print("checks ok; sample valid GSTIN:", good)
