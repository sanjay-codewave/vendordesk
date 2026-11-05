"""End-to-end test against a running server. Registers a throwaway account, so the demo account stays empty.
Usage: python tests/e2e.py [base_url]   (default http://127.0.0.1:8765)"""
import asyncio
import json
import re
import secrets
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
USER = "e2e_" + secrets.token_hex(4)
PASSWORD = secrets.token_urlsafe(12)
TOKEN = None
ok = 0


def check(cond, msg):
    global ok
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    ok += 1
    print("  ✓", msg)


async def call(session, name, **args):
    r = await session.call_tool(name, args)
    text = r.content[0].text if r.content else ""
    return r.isError, (json.loads(text) if not r.isError else text)


async def main():
    global TOKEN
    owner = httpx.AsyncClient(base_url=BASE)
    r = await owner.post("/register", data={"username": USER, "password": PASSWORD, "business_name": "E2E Test Co"})
    dash = await owner.get("/dashboard")
    TOKEN = re.search(r'/mcp/([A-Za-z0-9_-]+)"', dash.text).group(1)
    print(f"Registered {USER}")
    print("MCP")
    async with streamablehttp_client(f"{BASE}/mcp/wrong-token") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            err, msg = await call(s, "get_summary")
            check(err and "Invalid VendorDesk connection token" in msg, "wrong token gives a clear error")

    async with streamablehttp_client(f"{BASE}/mcp/{TOKEN}") as (r, w, _):
        async with ClientSession(r, w) as s:
            info = await s.initialize()
            check("never" in (info.instructions or "").lower(), "server instructions present")
            tools = {t.name for t in (await s.list_tools()).tools}
            check(tools == {"get_summary", "create_vendor_request", "list_vendors", "get_vendor", "update_vendor",
                            "add_review_note"}, "six tools listed")
            err, summ = await call(s, "get_summary")
            check(not err and summ["connected"], "get_summary works")
            err, v = await call(s, "create_vendor_request", vendor_name="Sharma Packaging (test)",
                                contact_name="Ravi", contact_phone="9800000000", state="Maharashtra",
                                category="Corrugated boxes",
                                required_items=["legal_name", "gstin", "pan", "bank_details", "cancelled_cheque"],
                                request_message="Hi Ravi, please share your details here: {link}")
            check(not err and "/v/" in v["public_link"] and v["public_link"] in v["request_message"],
                  "create_vendor_request returns link and fills {link}")
            err, msg = await call(s, "create_vendor_request", vendor_name="X", required_items=["bogus"],
                                  request_message="hi")
            check(err and "Unknown required_items" in msg, "bad checklist key rejected")
            vid, link = v["id"], v["public_link"]
            err, lst = await call(s, "list_vendors", status="requested")
            check(any(x["id"] == vid for x in lst["vendors"]), "list_vendors finds it")
            err, msg = await call(s, "get_vendor", id=999999)
            check(err and "No vendor with id" in msg, "unknown id gives a clear error")

    print("Vendor (public link)")
    token = link.rsplit("/", 1)[1]
    async with httpx.AsyncClient(base_url=BASE) as c:
        page = await c.get(f"/v/{token}")
        check(page.status_code == 200 and "Sharma Packaging (test)" in page.text, "public form opens without login")
        bad = (await c.post(f"/v/{token}/save", json={"gstin": "27ABCDE1234F1Z5", "pan": "abcde1234f",
                                                       "ifsc": "HDFC1001234", "bank_account_number": "12AB"})).json()
        st = {x["key"]: x["status"] for x in bad["checks"]}
        check(st["gstin"] == "fail" and st["ifsc"] == "fail" and st["account"] == "fail",
              "wrong GSTIN check digit / IFSC / account fail")
        good = (await c.post(f"/v/{token}/save", json={
            "legal_name": "Sharma Packaging Pvt Ltd", "gstin": "27ABCDE1234F1Z0", "pan": "ABCDE1234F",
            "ifsc": "HDFC0001234", "bank_account_number": "50100123456789", "bank_account_name": "Sharma Packaging",
            "bank_name": "HDFC Bank"})).json()
        st = {x["key"]: x["status"] for x in good["checks"]}
        check(all(st[k] == "pass" for k in ("gstin", "pan", "pan_in_gstin", "ifsc", "account", "state", "holder")),
              "correct details pass every check")
        check(st["item:cancelled_cheque"] == "missing", "cheque still missing")
        r = await c.post(f"/v/{token}/upload", data={"kind": "cancelled_cheque"},
                         files={"file": ("cheque.exe", b"MZ...", "application/octet-stream")})
        check(r.status_code == 400, "wrong file type rejected")
        r = await c.post(f"/v/{token}/upload", data={"kind": "cancelled_cheque"},
                         files={"file": ("cheque.png", b"\x89PNG" + b"0" * (5 * 1024 * 1024), "image/png")})
        check(r.status_code == 400 and "5 MB" in r.json()["error"], "oversized file rejected")
        r = await c.post(f"/v/{token}/upload", data={"kind": "cancelled_cheque"},
                         files={"file": ("cheque.pdf", b"%PDF-1.4 test", "application/pdf")})
        check(r.status_code == 200 and len(r.json()["files"]) == 1, "valid PDF uploaded")
        r = (await c.post(f"/v/{token}/submit")).json()
        check(r["status"] == "submitted" and not r["editable"], "submit locks the form")
        r = await c.post(f"/v/{token}/save", json={"pan": "ZZZZZ9999Z"})
        check(r.status_code == 409, "locked form rejects edits")

    print("Claude review")
    async with streamablehttp_client(f"{BASE}/mcp/{TOKEN}") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            err, d = await call(s, "get_vendor", id=vid)
            check(not err and d["submitted_fields"]["gstin"] == "27ABCDE1234F1Z0" and d["files"][0]["size_bytes"] > 0,
                  "get_vendor returns fields and files")
            err, msg = await call(s, "add_review_note", id=vid, findings="x", suggested_status="approved")
            check(err, "Claude cannot approve")
            err, d = await call(s, "add_review_note", id=vid, findings="All checks pass. Cheque uploaded.",
                                follow_up_message="Thanks Ravi, all good.", suggested_status="verified")
            check(not err and d["status"] == "verified", "add_review_note saves and sets verified")
            err, d = await call(s, "update_vendor", id=vid, notes="Pays net 30", fields={"bank_name": "HDFC Bank Ltd"})
            check(not err and d["notes"] == "Pays net 30", "update_vendor works")

    print("Owner (web app)")
    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.post("/login", data={"username": USER, "password": "wrong"})
        check("error" in r.headers["location"], "wrong password rejected")
        await c.post("/login", data={"username": USER, "password": PASSWORD})
        dash = await c.get("/dashboard")
        check(dash.status_code == 200 and "Sharma Packaging (test)" in dash.text and "Created by Claude" in dash.text,
              "dashboard shows Claude-created vendor")
        check("50100123456789" not in dash.text and "•••• 6789" in dash.text, "account number masked on board")
        det = await c.get(f"/vendors/{vid}")
        check("50100123456789" in det.text and "All checks pass" in det.text, "detail shows full value and review")
        r = await c.post(f"/vendors/{vid}/fields", data={"legal_name": "Sharma Packaging Private Limited",
                                                          "gstin": "27ABCDE1234F1Z0", "pan": "ABCDE1234F",
                                                          "ifsc": "HDFC0001234", "bank_account_number": "50100123456789",
                                                          "bank_account_name": "Sharma Packaging",
                                                          "bank_name": "HDFC Bank Ltd"})
        await c.post(f"/vendors/{vid}/status", data={"status": "approved"})
        det = await c.get(f"/vendors/{vid}")
        for actor in ("Vendor", "Claude", "You"):
            check(f'class="actor {actor.lower()}"' in det.text, f"history has actor {actor}")
        csv = await c.get("/api/export.csv")
        check(csv.status_code == 200 and "Sharma Packaging Private Limited" in csv.text, "CSV export has approved vendor")
        fid = re.search(rf'/vendors/{vid}/files/(\d+)', det.text).group(1)
        f = await c.get(f"/vendors/{vid}/files/{fid}")
        check(f.status_code == 200 and f.content.startswith(b"%PDF"), "uploaded file downloads")
        anon = await httpx.AsyncClient(base_url=BASE).get(f"/vendors/{vid}")
        check(anon.status_code == 303, "detail page needs login")
    print(f"\nAll {ok} checks passed.")


asyncio.run(main())
