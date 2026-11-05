# VendorDesk

Vendor onboarding for small businesses that runs on **your own Claude**.

You ask Claude to onboard a vendor. Claude picks the checklist and writes the request message. VendorDesk creates the
vendor record and a public link. The vendor opens the link (no login), fills in GST, PAN and bank details and uploads
documents. VendorDesk checks every field the same way every time. You review in Claude or in the app, edit anything,
approve and export.

**No AI inside the app.** There are no model calls, no AI keys and no AI, ML or OCR libraries. All the thinking and
writing happens in the customer's own Claude, which connects over MCP. The `mcp` dependency is the open
[Model Context Protocol](https://modelcontextprotocol.io) server SDK. It only defines tools that Claude can call. It
is not an AI client.

## Who does what

| Claude (in the customer's chat) | VendorDesk (plain code) |
|---|---|
| Decides what to ask each vendor | Hosts the public link and form |
| Writes request and follow-up messages | Stores details and uploaded documents |
| Reviews a submission and explains issues | Runs rule-based checks: GSTIN regex + state code + check digit, PAN, PAN-inside-GSTIN, IFSC, account number, state match, account-holder name match |
| Changes status, checklist and notes on request | Keeps status, notes and an audit trail (vendor / you / Claude) |
| | Lets a person edit any field, approve and export CSV |

## Connect to Claude

1. Log in and copy your MCP link from the **Connect to Claude** panel on the dashboard
   (`https://<host>/mcp/<your-token>`). Treat it like a password.
2. In Claude (Pro plan or above): **Customize → Connectors → + → Add custom connector**.
3. Name it **VendorDesk**, paste the link and choose **No sign in**.
4. In a new chat, enable VendorDesk from the **+** menu.

Try:

- "Use VendorDesk to onboard Sharma Packaging (contact Ravi, 9800000000, Maharashtra). I need GST, PAN, bank details and a cancelled cheque."
- "Review Sharma Packaging's submission and draft a follow-up."
- "Mark Sharma Packaging as needs fixes."
- "What needs my attention in VendorDesk?"

### Fake vendor data for testing the public form

| Field | Passes | Fails |
|---|---|---|
| Legal name | Sharma Packaging Pvt Ltd | |
| GSTIN | `27ABCDE1234F1Z0` | `27ABCDE1234F1Z5` (wrong check digit) |
| PAN | `ABCDE1234F` | `ABCDE1234` (too short), `ZZZZZ1234F` (doesn't match GSTIN) |
| IFSC | `HDFC0001234` | `HDFC1001234` (5th character must be 0) |
| Account number | `50100123456789` | `12AB` |
| Account holder | Sharma Packaging | Ravi Sharma (warning: differs from business) |

All values are made up. Uploads must be PDF, JPG or PNG, up to 5 MB.

## MCP tools

`get_summary`, `create_vendor_request`, `list_vendors`, `get_vendor`, `update_vendor`, `add_review_note`.
Transport: Streamable HTTP (stateless, JSON responses). Each user's token is in the URL path.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn main:app --port 8765 --reload
# open http://127.0.0.1:8765, log in as demo / demo1234 (local defaults)
```

Without `DATABASE_URL`, SQLite (`vendordesk.db`) is used.

## Tests

```bash
.venv/bin/python checks.py                 # check-rule self-test
.venv/bin/python tests/e2e.py              # full flow against a running server (default http://127.0.0.1:8765)
.venv/bin/python tests/e2e.py https://<host>  # same against a deployment; uses a throwaway account, not demo
```

The end-to-end test calls every MCP tool over Streamable HTTP, including with a wrong token. It also submits wrong
and correct details, uploads valid, oversized and wrong-type files, and confirms the audit trail shows vendor, you
and Claude. Finally it checks CSV export and masked account numbers.

## Deploy (Render + Neon)

- `render.yaml` defines the web service. The start command is
  `uvicorn main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips '*'`.
- Environment variables:
  - `DATABASE_URL`: Neon Postgres connection string.
  - `SECRET_KEY`: session signing key.
  - `PUBLIC_URL`: for example `https://vendordesk.onrender.com`.
  - `DEMO_PASSWORD` and `DEMO_TOKEN`: the reviewer demo account's password and MCP token.
- Files are stored in the database because Render's free disk is ephemeral.
- The free tier sleeps when idle. Point an uptime pinger at `/healthz` so Claude's first tool call doesn't time out.

## Data handling

PAN, GST and bank details are sensitive. Account numbers are masked in lists. Field values are never logged, and the
MCP token is stripped from access-log paths. Public links only reach their own vendor record through an unguessable
token. Uploaded files are served with `nosniff` and a restrictive CSP.
