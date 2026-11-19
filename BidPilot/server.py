"""BidPilot Phase 3 demo server. Binds to 127.0.0.1 only — private local preview.

Owner routes require a per-launch owner session token; every state-changing
POST (founder and client token routes) requires a same-origin request.
"""
import argparse
import hashlib
import html
import json
import math
import re
import secrets
import sys
import threading
import time
import urllib.parse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core
import dashboard_ops
import db

DB_PATH = Path(__file__).resolve().parent / "bidpilot.db"
SAMPLE_BADGE = core.SAMPLE_BADGE
QUOTE_LIMIT = 16 * 1024
_quote_lock = threading.Lock()
_quote_times = []

QUOTE_TEXT = ("name_business", "reply_email", "project_brief", "video_format",
              "production_mode", "scripting", "editing_level", "graphics",
              "deadline", "travel_kit_talent")
QUOTE_COUNTS = ("shoot_days", "locations", "interviewees", "deliverables",
                "revision_rounds", "caption_versions")


def validate_quote(data, form=False):
    if not isinstance(data, dict):
        raise ValueError("Expected an object")
    allowed = set(QUOTE_TEXT + QUOTE_COUNTS + ("budget_sgd", "caption_every_deliverable", "request_id"))
    if set(data) - allowed:
        raise ValueError("Unknown field")
    request_id = data.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
        raise ValueError("request_id is required")
    fields = {}
    for key in QUOTE_TEXT:
        value = data.get(key, "")
        if not isinstance(value, str):
            raise ValueError(key + " must be text")
        fields[key] = value.strip()
    for key in QUOTE_COUNTS:
        value = data.get(key)
        if form and isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
            value = int(value)
        if type(value) is not int or value < 0:
            raise ValueError(key + " must be a nonnegative integer")
        fields[key] = value
    value = data.get("budget_sgd")
    if form and isinstance(value, str):
        value = float(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("budget_sgd must be a nonnegative number")
    fields["budget_sgd"] = value
    value = data.get("caption_every_deliverable", False)
    if form:
        if value not in (False, "on", "true", "1"):
            raise ValueError("caption_every_deliverable must be a boolean")
        value = bool(value)
    if type(value) is not bool:
        raise ValueError("caption_every_deliverable must be a boolean")
    fields["caption_every_deliverable"] = value
    if fields["deadline"]:
        date.fromisoformat(fields["deadline"])
    return request_id.strip(), fields


def reject_nonfinite(value):
    raise ValueError(f"Invalid JSON number {value}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    # -- plumbing ----------------------------------------------------------
    def open_db(self):
        # One connection per request: ThreadingHTTPServer serves requests on
        # multiple threads and SQLite connections are thread-bound.
        self.conn = db.connect(self.server.db_path)

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, text, code=200, noindex=True):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "same-origin")
        if noindex:
            self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def not_found(self):
        self.send_html("<h1>404 Not Found</h1><p>The page you requested does not exist.</p>",
                       code=404)

    def forbidden(self, msg="Forbidden"):
        self.send_json({"ok": False, "error": "forbidden", "detail": msg}, 403)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    # -- auth --------------------------------------------------------------
    def query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def owner_ok(self):
        """Founder routes need the per-launch owner session token."""
        supplied = self.headers.get("X-Owner-Token") or (self.query().get("owner") or [None])[0]
        return bool(supplied and secrets.compare_digest(
            supplied.encode("utf-8"), self.server.owner_token.encode("ascii")))

    def same_origin(self):
        """Require the HTTP origin of the loopback server on every write."""
        supplied = self.headers.get("Origin") or self.headers.get("Referer")
        if not supplied:
            return False
        try:
            origin = urllib.parse.urlsplit(supplied)
        except ValueError:
            return False
        return (origin.scheme == "http" and bool(origin.netloc)
                and origin.netloc == self.headers.get("Host", ""))

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        self.open_db()
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/quote":
                return self.send_html(quote_page())
            if path == "/" or path == "/dashboard":
                if not self.owner_ok():
                    return self.forbidden("owner token required")
                return self.send_html(founder_page(self.conn))
            if path == "/api/state":
                if not self.owner_ok():
                    return self.forbidden("owner token required")
                self.conn.execute("BEGIN")
                return self.send_json(state(self.conn))
            if path.startswith("/p/"):
                token = path[3:].strip("/")
                return self.client_page(token)
            if path.startswith("/preview/"):
                if not self.owner_ok():
                    return self.forbidden("owner token required")
                return self.preview_page(path[9:].strip("/"))
            return self.not_found()
        finally:
            self.conn.close()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self.same_origin():
            return self.forbidden("same-origin request required")
        if path == "/quote":
            return self.quote_post()
        if path.startswith("/p/"):
            return self.token_post(path)
        if not self.owner_ok():
            return self.forbidden("owner token required")
        self.open_db()
        try:
            try:
                data = self.body()
            except (ValueError, RecursionError):
                return self.send_json({"ok": False, "error": "bad_json"}, 400)

            if path == "/api/import-run" or path == "/api/reset-sample":
                operation = (dashboard_ops.import_run if path == "/api/import-run"
                             else dashboard_ops.reset_sample)
                try:
                    result = operation(self.conn, data)
                except ValueError as exc:
                    status = 409 if str(exc) == "request_id_conflict" else 400
                    return self.send_json({"ok": False, "error": str(exc)}, status)
                return self.send_json({"ok": True, **result})

            if path == "/api/import":
                offer_request = data["offer_request"]
                try:
                    deal_id, offer_id, fresh = core.claim_or_resume(
                        self.conn, data["source"], data["source_id"],
                        offer_request["offer"]["client"], data.get("raw_inquiry", ""),
                        offer_request)
                except ValueError as exc:
                    return self.send_json({"ok": False, "error": str(exc)}, 400)
                return self.send_json({"ok": True, "deal_id": deal_id, "offer_id": offer_id,
                                       "fresh": fresh, "duplicate": not fresh})

            if path == "/api/evaluate":
                eval_id, version_id = core.evaluate(self.conn, data["deal_id"], data["offer_id"])
                return self.send_json({"ok": True, "eval_id": eval_id, "version_id": version_id})

            if path == "/api/check":
                outcome = core.check_draft(self.conn, data["version_id"])
                return self.send_json({"ok": outcome["passed"], "result": outcome})

            if path == "/api/draft":
                new_id = core.save_draft(self.conn, data["version_id"], data["body"])
                return self.send_json({"ok": True, "version_id": new_id})

            if path == "/api/approve":
                res = core.approve(self.conn, data["version_id"], data.get("option_id"))
                return self.send_json(res, 200 if res["ok"] else 409)

            if path == "/api/reject":
                return self.send_json(core.reject(self.conn, data["version_id"],
                                                  data.get("note", "")))

            if path == "/api/rotate-token":
                res = core.rotate_token(self.conn, data["version_id"])
                return self.send_json(res, 200 if res["ok"] else 409)

            if path == "/api/reevaluate":
                try:
                    offer_id = core.reevaluate(self.conn, data["offer_id"])
                except ValueError as exc:
                    return self.send_json({"ok": False, "error": str(exc)}, 400)
                eval_id, version_id = core.evaluate(
                    self.conn, data["deal_id"], offer_id)
                return self.send_json({"ok": True, "offer_id": offer_id,
                                       "eval_id": eval_id, "version_id": version_id})

            if path == "/api/confirm-booking":
                res = core.confirm_booking(self.conn, data["version_id"])
                code = 200 if res["ok"] else 409
                return self.send_json(res, code)

            if path == "/api/close-deal":
                res = core.close_deal(self.conn, data["deal_id"])
                return self.send_json(res, 200 if res["ok"] else 409)

            if path == "/api/client-change":
                try:
                    offer_id = core.client_change(self.conn, data["deal_id"],
                                                  data.get("body", ""),
                                                  data.get("request_id") or secrets.token_hex(8),
                                                  data.get("terms"))
                except ValueError as exc:
                    return self.send_json({"ok": False, "error": str(exc)}, 400)
                if offer_id is None:
                    return self.send_json({"ok": True, "status": "clarify", "offer_id": None,
                                           "message": "Change request queued for clarification"}, 202)
                return self.send_json({"ok": True, "status": "offered", "offer_id": offer_id})
            return self.not_found()
        finally:
            self.conn.close()

    def quote_post(self):
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
        try:
            length = int(self.headers.get("Content-Length", ""))
            if length < 0 or length > QUOTE_LIMIT:
                raise ValueError("Invalid body size")
            raw = self.rfile.read(length).decode("utf-8")
            if content_type == "application/json":
                data = json.loads(raw, parse_constant=reject_nonfinite)
                form = False
            elif content_type == "application/x-www-form-urlencoded":
                pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True, strict_parsing=True, encoding="utf-8", errors="strict")
                data = dict(pairs)
                if len(data) != len(pairs):
                    raise ValueError("Duplicate field")
                form = True
            else:
                raise ValueError("Unsupported content type")
            request_id, fields = validate_quote(data, form)
            request_id = "quote:" + request_id
            request_id.encode("utf-8")
            payload = json.dumps({"tool": "quote", "fields": fields}, sort_keys=True,
                                 separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        except (ValueError, OverflowError, RecursionError):
            return self.send_json({"ok": False, "error": "invalid_quote"}, 400)
        payload_hash = hashlib.sha256(payload).hexdigest()
        self.open_db()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            op = self.conn.execute("SELECT * FROM mcp_operations WHERE request_id=?", (request_id,)).fetchone()
            if op:
                self.conn.rollback()
                if op["tool"] != "quote" or op["payload_hash"] != payload_hash:
                    return self.send_json({"ok": False, "error": "request_id_conflict"}, 409)
                result = db.uj(op["result_json"])
            else:
                with _quote_lock:
                    now = time.monotonic()
                    _quote_times[:] = [t for t in _quote_times if t > now - 600]
                    if len(_quote_times) >= 20:
                        self.conn.rollback()
                        return self.send_json({"ok": False, "error": "rate_limited"}, 429)
                    _quote_times.append(now)
                source_id = "site-" + secrets.token_hex(8)
                result = {"ok": True, "source_id": source_id}
                self.conn.execute("INSERT INTO inbox (id, source, source_id, fields_json, raw_message, received_at, sample_data) VALUES (?, 'site', ?, ?, ?, ?, 1)",
                    (secrets.token_hex(16), source_id, db.j(fields), fields["project_brief"], db.now()))
                self.conn.execute("INSERT INTO mcp_operations (request_id, tool, payload_hash, result_json, created_at) VALUES (?, 'quote', ?, ?, ?)",
                    (request_id, payload_hash, db.j(result), db.now()))
                self.conn.commit()
            if not form:
                return self.send_json(result)
            return self.send_html('<!doctype html><html lang="en"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Request received</title><h1>Request received</h1><p>Reference: ' + html.escape(result["source_id"]) + '</p></html>')
        finally:
            self.conn.close()

    def token_post(self, path):
        """Client token routes: same-origin enforced, no owner token involved."""
        self.open_db()
        try:
            try:
                data = self.body()
            except json.JSONDecodeError:
                return self.send_json({"ok": False, "error": "bad_json"}, 400)

            if path.endswith("/accept"):
                token = path[3:-7]
                res = core.client_accept(self.conn, token, data.get("option_id"))
                code = res.pop("http", 200) if not res["ok"] else 200
                return self.send_json(res, code)

            if path.endswith("/request-change"):
                token = path[3:-15]
                version = core.published_by_token(self.conn, token)
                if not version:
                    return self.send_json({"ok": False, "error": "not_found"}, 404)
                request_id = data.get("request_id") or secrets.token_hex(8)
                try:
                    offer_id = core.client_change(self.conn, version["deal_id"],
                                                  data.get("body", ""), request_id,
                                                  data.get("terms"),
                                                  expected_version_id=version["id"],
                                                  expected_token=token)
                except ValueError as exc:
                    if str(exc) == "stale_version":
                        return self.send_json({"ok": False, "error": "not_found"}, 404)
                    return self.send_json({"ok": False, "error": str(exc)}, 400)
                if offer_id is None:
                    return self.send_json({"ok": True, "status": "clarify", "offer_id": None,
                                           "message": "Change request queued for clarification"}, 202)
                return self.send_json({"ok": True, "status": "offered", "offer_id": offer_id})
            return self.not_found()
        finally:
            self.conn.close()

    # -- client view -------------------------------------------------------
    def client_page(self, token):
        self.conn.execute("BEGIN")
        version = core.published_by_token(self.conn, token)
        if not version:
            return self.not_found()
        try:
            option_rows = core.options_of(self.conn, version)
            valid = (version["outbound_status"] == "passed"
                     and core.digest(version, option_rows) == version["checked_digest"]
                     and not core.option_set_errors(self.conn, version))
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            return self.not_found()
        return self.send_html(render_client_html(self.conn, version, preview=False))

    def preview_page(self, version_id):
        """Owner-only: exactly what the client will see, before approval."""
        version = conn_version(self.conn, version_id)
        if not version:
            return self.not_found()
        return self.send_html(render_client_html(self.conn, version, preview=True))


def quote_page():
    page = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Request a quote</title>
<style>body{font:16px system-ui;max-width:700px;margin:auto;padding:1rem;line-height:1.5} form{display:grid;gap:1rem}label{display:grid;gap:.25rem}input,select,textarea,button{font:inherit;padding:.6rem;box-sizing:border-box;width:100%}input[type=checkbox]{width:auto}button{cursor:pointer}textarea{min-height:6rem}</style></head>
<body><p>Sample data · 23 Nov 2026 · Asia/Singapore</p><h1>Request a quote</h1>
<form method="post" action="/quote"><input type="hidden" name="request_id" value="QUOTE_REQUEST_ID">
<label>Name / business<input name="name_business" required></label><label>Reply email<input type="email" name="reply_email" required></label>
<label>Project brief<textarea name="project_brief" required></textarea></label>
<label>Video format<input name="video_format" required></label>
<label>Production mode<select name="production_mode"><option value="edit_only">Edit-only</option><option value="on_location">On-location</option></select></label>
<label>Shoot days<input type="number" min="0" name="shoot_days" value="0" required></label>
<label>Locations<input type="number" min="0" name="locations" value="0" required></label>
<label>Interviewees<input type="number" min="0" name="interviewees" value="0" required></label>
<label>Scripting<select name="scripting"><option value="none">None</option><option value="light">Light</option><option value="full">Full</option></select></label>
<label>Number of videos<input type="number" min="0" name="deliverables" value="1" required></label>
<label>Editing complexity<input name="editing_level" required></label><label>Graphics type<input name="graphics" required></label>
<label>Revision rounds<input type="number" min="0" name="revision_rounds" value="0" required></label>
<label>Captioned video count<input type="number" min="0" name="caption_versions" value="0" required></label>
<label><span><input type="checkbox" name="caption_every_deliverable"> Caption every deliverable</span></label>
<label>Delivery deadline<input type="date" name="deadline" required></label>
<label>SGD budget<input type="number" name="budget_sgd" min="0" step="0.01" required></label>
<label>Travel, kit or talent needs<textarea name="travel_kit_talent"></textarea></label>
<button type="button" id="preset">Use demo preset</button><button type="submit">Submit request</button></form>
<script>document.getElementById('preset').onclick = () => {const f=document.querySelector('form');
for (const [k,v] of Object.entries({editing_level:'standard',graphics:'basic_titles',scripting:'light',revision_rounds:2,interviewees:0,shoot_days:1,locations:1,production_mode:'on_location'})) f.elements[k].value=v;};</script></body></html>"""
    return page.replace("QUOTE_REQUEST_ID", secrets.token_urlsafe(18))


def conn_version(conn, version_id):
    return conn.execute("SELECT * FROM proposal_versions WHERE id=?",
                         (version_id,)).fetchone()


OPTION_LABELS = {
    "as_offered": "As proposed",
    "hold_date": "Hold the date",
    "hold_budget": "Fit your budget",
    "hold_scope": "Full scope, list price",
    "phased": "Phased delivery",
}


def option_label(kind):
    return OPTION_LABELS.get(kind, kind.replace("_", " ").title())


def render_client_html(conn, version, preview=False):
    """Client-facing page. Only approved public option terms cross the
    boundary: no floor, capacity, internal metrics or other client names."""
    option_rows = core.options_of(conn, version)
    selected = version["selected_option_id"]
    accepted = None
    if version["accepted_at"]:
        accept_row = conn.execute(
            "SELECT option_ids_json FROM messages WHERE version_id=?"
            " AND direction='acceptance'", (version["id"],)).fetchone()
        if accept_row and db.uj(accept_row["option_ids_json"]):
            accepted = db.uj(accept_row["option_ids_json"])[0]
    cards = []
    for row in option_rows:
        t = db.uj(row["public_terms_json"])
        phases = "".join(
            f"<li>Phase {i + 1}: {p['deliverables']} videos by {p['deadline']}</li>"
            for i, p in enumerate(t.get("phases") or []))
        phase_html = f"<ul class='phases'>{phases}</ul>" if phases else ""
        marks = []
        if row["id"] == selected and not accepted:
            marks.append("<span class='chip'>Alex's suggestion</span>")
        if row["id"] == accepted:
            marks.append("<span class='chip ok'>Your choice</span>")
        if preview:
            button = "<button type='button' disabled>Preview only — not yet released</button>"
        elif accepted:
            button = ("<button type='button' disabled>Accept this option</button>"
                      if row["id"] != accepted else
                      "<button type='button' disabled>Accepted — awaiting Alex</button>")
        else:
            button = ("<form method='post' onsubmit=\"return submitAccept(event, '{0}')\">"
                      "<button type='submit'>Accept this option</button></form>"
                      ).format(row["id"])
        cards.append(f"""
          <div class="card option{' selected' if row['id'] == selected else ''}">
            <h3>{option_label(row['option_key'])}</h3>
            <p class="terms">{t['deliverables']} video{'s' if t['deliverables'] != 1 else ''}
               by {t['deadline']}</p>
            <p class="price">S${t['price_sgd']:,.2f}</p>
            <p>{t['caption_versions']} captioned version{'s' if t['caption_versions'] != 1 else ''}
               · {t['revision_rounds']} revision round{'s' if t['revision_rounds'] != 1 else ''}</p>
            {''.join(marks)}
            {phase_html}
            {button}
          </div>""")
    if accepted:
        status_banner = ("<p class='accepted'>Thank you — your acceptance is recorded"
                         " and awaits confirmation from Alex.</p>")
        change_form = ""
    elif preview:
        status_banner = "<p class='chip'>Owner preview — nothing has been sent.</p>"
        change_form = ""
    else:
        status_banner = ""
        change_form = """
        <form class="rc" method="post" onsubmit="return submitChangeForm(event)">
          <label for="rc">Request a change</label>
          <textarea id="rc" name="body" placeholder=
            "State what you want changed: number of videos, captioned versions, deadline and budget."></textarea>
          <p class="small">Your message is kept exactly as written. The explicit
           counters below state the terms — nothing is inferred from the free text.</p>
          <div class="ctl">
            <label class="rc-field">Videos
              <input id="rc-count" type="number" min="1" value="5"></label>
            <label class="rc-field">Captioned versions
              <input id="rc-captions" type="number" min="0" value="5"></label>
            <label class="rc-field">Deadline
              <input id="rc-deadline" value="2026-12-04"></label>
            <label class="rc-field">Budget S$
              <input id="rc-budget" type="number" step="0.01" value="2700"></label>
            <button type="submit">Send change request</button>
          </div>
        </form>"""
    title = "Alex Studio — your proposal" if not preview else "Owner preview — client page"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{_esc(title)}</title>
<style>{CLIENT_STYLE}</style></head><body>
<header><span class="badge">{SAMPLE_BADGE}</span></header>
<main><h1>{_esc(title)}</h1>
<p class="lede">{_esc(version['draft_body'])}</p>
{status_banner}
<div class="cards">{''.join(cards)}</div>
{change_form}
<p id="msg" role="status" aria-live="polite"></p>
<p class="small">Prices and dates come from the option cards above.</p>
</main><script>
// Submit handlers are synchronous: they prevent the native form POST and
// disable the button BEFORE starting the async JSON request, so a Promise
// return value can never trigger a native submission to the raw JSON route.
function stableRequestId(payload) {{
  // Stable id for the exact payload on this client link: retrying the same
  // text and counters reuses one request_id, so an ambiguous transport
  // outcome cannot create a duplicate change round. The page path (unique
  // per client link/deal) is part of the hash so equal texts on different
  // proposals never collide — request_id is globally unique in storage.
  const s = location.pathname + '|' + JSON.stringify(payload);
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
  return 'req-' + h.toString(16);
}}
function setMsg(text) {{
  document.getElementById('msg').textContent = text;
}}
function submitAccept(ev, optionId) {{
  ev.preventDefault();
  const btn = ev.target.querySelector("button[type='submit']");
  if (btn.disabled) return false;
  btn.disabled = true;
  accept(optionId, btn);
  return false;
}}
async function accept(optionId, btn) {{
  try {{
    const res = await fetch(location.pathname + '/accept', {{method: 'POST',
      headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{option_id: optionId}})}});
    const data = await res.json();
    setMsg(data.ok
      ? 'Thank you — your acceptance is recorded and awaits confirmation from Alex.'
      : 'Could not record acceptance: ' + (data.error || 'unknown error') + '. You can try again.');
    if (data.ok) setTimeout(() => location.reload(), 800);
    else btn.disabled = false;
  }} catch (e) {{
    setMsg('Network error — your acceptance was not sent. Retrying is safe; your choice is preserved.');
    btn.disabled = false;
  }}
}}
function submitChangeForm(ev) {{
  ev.preventDefault();
  const btn = ev.target.querySelector("button[type='submit']");
  if (btn.disabled) return false;
  btn.disabled = true;
  btn.textContent = 'Sending…';
  requestChange(btn);
  return false;
}}
async function requestChange(btn) {{
  const body = document.getElementById('rc').value;
  const terms = {{
    count: Number(document.getElementById('rc-count').value),
    captions: Number(document.getElementById('rc-captions').value),
    deadline: document.getElementById('rc-deadline').value,
    budget: Number(document.getElementById('rc-budget').value)}};
  const payload = {{body: body, terms: terms}};
  try {{
    const res = await fetch(location.pathname + '/request-change', {{method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{body: body, terms: terms,
        request_id: stableRequestId(payload)}})}});
    const data = await res.json();
    setMsg(data.ok
      ? 'Your change request was sent. Alex will review and reply.'
      : 'Could not send the request: ' + (data.error || 'unknown error') + '. Your message is still in the box — you can try again.');
    if (data.ok) {{ btn.textContent = 'Change request sent'; }}
    else {{ btn.disabled = false; btn.textContent = 'Send change request'; }}
  }} catch (e) {{
    setMsg('Network error — the request may not have been sent. Retrying with the same text will not create a duplicate request. Your message is still in the box.');
    btn.disabled = false;
    btn.textContent = 'Send change request';
  }}
}}
</script></body></html>"""


def _esc(text):
    import html
    return html.escape(text or "", quote=True)


def capacity_panel(conn):
    """Private founder panel: aggregate weekly planning capacity (sample only)."""
    led = core.cap.ledger(conn)
    weeks = [{"week": w, "budget_hours": s["budget"] / 100,
              "allocated_hours": s["allocated"] / 100,
              "remaining_hours": s["residual"] / 100}
             for w, s in sorted(led.items())]
    return {"label": "Aggregate planning capacity (sample booking calendar —"
                     " not a promise of shoot dates)",
            "revision": core.cap.ledger_revision(conn),
            "baseline_seed_hash": core.seed_hash(core.seed_data())[:12],
            "weeks": weeks,
            "note": "Final booking is subject to Alex's confirmation; quotes and"
                    " pending client acceptances reserve nothing."}


def state(conn):
    panel = capacity_panel(conn)
    current_revision = panel["revision"]
    deals = []
    for deal in conn.execute("SELECT * FROM deals ORDER BY created_at").fetchall():
        versions = []
        for v in conn.execute(
                "SELECT * FROM proposal_versions WHERE deal_id=? ORDER BY version_no",
                (deal["id"],)).fetchall():
            eval_row = conn.execute("SELECT * FROM evaluations WHERE offer_id=?",
                                    (v["offer_id"],)).fetchone()
            result = db.uj(eval_row["result_json"]) if eval_row else None
            capacity_stale = bool(
                eval_row and eval_row["capacity_revision"] is not None
                and eval_row["capacity_revision"] != current_revision)
            accept_row = conn.execute(
                "SELECT option_ids_json, created_at FROM messages WHERE version_id=?"
                " AND direction='acceptance'", (v["id"],)).fetchone()
            accepted_option = None
            if accept_row:
                ids = db.uj(accept_row["option_ids_json"]) or []
                terms = {r["id"]: db.uj(r["public_terms_json"])
                         for r in core.options_of(conn, v)}
                accepted_option = {"option_id": ids[0] if ids else None,
                                   "terms": terms.get(ids[0]) if ids else None,
                                   "accepted_at": accept_row["created_at"]}
            confirm_row = conn.execute("SELECT * FROM confirmations WHERE version_id=?",
                                       (v["id"],)).fetchone()
            versions.append({
                "version_id": v["id"], "version_no": v["version_no"], "status": v["status"],
                "outbound_status": v["outbound_status"], "draft": v["draft_body"],
                "selected_option_id": v["selected_option_id"],
                "published": v["status"] in ("published", "confirmed"),
                "accepted_at": v["accepted_at"],
                "accepted_option": accepted_option,
                "confirmed": bool(confirm_row),
                "verdict": eval_row["verdict"] if eval_row else None,
                "reason_codes": db.uj(eval_row["reason_codes_json"]) if eval_row else [],
                "estimate": (result or {}).get("estimate"),
                "options": [db.uj(r["public_terms_json"])
                            for r in core.options_of(conn, v)],
                "option_meta": [{"option_row_id": r["id"], "option_id": r["option_key"],
                                 "label": option_label(r["option_key"])}
                                for r in core.options_of(conn, v)],
                "comparables": (result or {}).get("comparables"),
                "option_metrics": (result or {}).get("option_metrics"),
                "capacity_stale": capacity_stale,
            })
        offers = []
        for o in conn.execute("SELECT * FROM offers WHERE deal_id=? ORDER BY round",
                              (deal["id"],)).fetchall():
            evaluated = conn.execute("SELECT id FROM evaluations WHERE offer_id=?",
                                     (o["id"],)).fetchone() is not None
            offers.append({"offer_id": o["id"], "round": o["round"],
                           "evaluated": evaluated,
                           "offer": db.uj(o["offer_json"]), "as_of": o["as_of"]})
        steps = [dict(r) for r in conn.execute(
            "SELECT offer_id, step_key, status, private_note, started_at, finished_at"
            " FROM run_steps WHERE deal_id=? ORDER BY started_at, rowid", (deal["id"],))]
        agent_outputs = [{"offer_id": r["offer_id"], "kind": r["kind"],
                          "created_at": r["created_at"], "payload": db.uj(r["payload_json"])}
                         for r in conn.execute(
                             "SELECT offer_id, kind, created_at, payload_json FROM agent_outputs"
                             " WHERE deal_id=? ORDER BY created_at, rowid", (deal["id"],))]
        messages = [dict(r) for r in conn.execute(
            "SELECT direction, body, request_id, created_at FROM messages"
            " WHERE deal_id=? ORDER BY created_at", (deal["id"],)).fetchall()]
        approvals = [dict(r) for r in conn.execute(
            "SELECT action, note, reviewed_digest, decided_at FROM approvals a"
            " JOIN proposal_versions v ON v.id=a.version_id WHERE v.deal_id=?"
            " ORDER BY decided_at", (deal["id"],)).fetchall()]
        queued = [s for s in steps if s["status"] == "queued"]
        deals.append({"deal_id": deal["id"], "source": deal["source"],
                      "source_id": deal["source_id"], "client": deal["client"],
                      "status": deal["status"], "raw_inquiry": deal["raw_inquiry"],
                      "versions": versions, "offers": offers,
                      "steps": steps, "agent_outputs": agent_outputs,
                      "messages": messages, "approvals": approvals,
                      "queued_clarifications": queued})
    inbox = [{"source_id": r["source_id"], "fields": db.uj(r["fields_json"]),
              "received_at": r["received_at"], "claimed": bool(r["deal_id"])}
             for r in conn.execute("SELECT i.*, d.id AS deal_id FROM inbox i LEFT JOIN deals d ON d.source=i.source AND d.source_id=i.source_id ORDER BY i.received_at DESC").fetchall()]
    return {"badge": SAMPLE_BADGE, "capacity": panel, "deals": deals, "inbox": inbox}


BASE_STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { font: 15px/1.55 -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  margin: 0; background: #f6f5f2; color: #1f272e; }
header { background: #16212a; color: #fff; padding: 10px 18px; display: flex; gap: 10px;
  align-items: center; flex-wrap: wrap; }
.badge { background: #d9ed92; color: #16212a; padding: 3px 10px; border-radius: 999px;
  font-size: 12.5px; font-weight: 650; }
.badge.dim { background: #33475a; color: #dbe4ec; }
main { max-width: 980px; margin: 0 auto; padding: 20px 16px 60px; }
h1 { font-size: 26px; margin: 14px 0 6px; letter-spacing: -0.01em; }
h2 { font-size: 19px; margin: 0 0 10px; }
h3 { font-size: 16px; margin: 12px 0 6px; }
p.lede { color: #4a5763; font-size: 16px; }
.small { color: #66727e; font-size: 13px; }
.muted { color: #55616d; }
button { background: #16212a; color: #fff; border: 0; border-radius: 8px; padding: 9px 14px;
  font-size: 14.5px; cursor: pointer; }
button:hover { background: #33475a; }
button:disabled { background: #9aa5af; cursor: not-allowed; }
button.ghost { background: transparent; color: #16212a; border: 1px solid #c4ccd3; }
button.danger { background: #7a2e2e; }
code { background: #e8edf2; padding: 1px 5px; border-radius: 4px; font-size: 13px; }
.cards { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(235px, 1fr)); }
.card { background: #fff; border: 1px solid #dde3e8; border-radius: 12px; padding: 14px; }
.card.option.selected { border-color: #2a7d5a; outline: 2px solid #2a7d5a; }
.price { font-size: 21px; font-weight: 750; margin: 4px 0; }
.deal { background: #fff; border: 1px solid #dde3e8; border-radius: 14px; padding: 18px;
  margin-bottom: 22px; box-shadow: 0 1px 2px rgba(20,30,40,.04); }
.deal > h2 { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.status-chip { padding: 2px 10px; border-radius: 999px; font-size: 12.5px; font-weight: 650; }
.status-chip.new { background: #e3ecf7; color: #1c4e8a; }
.status-chip.published { background: #e0f2e8; color: #17603f; }
.status-chip.confirmed { background: #d9ed92; color: #274d16; }
.status-chip.closed { background: #d7dde3; color: #3c4854; }
.status-chip.change_requested { background: #fdeecb; color: #8a5a12; }
.status-chip.evaluated { background: #e3ecf7; color: #1c4e8a; }
.blocked { background: #fdecec; color: #8f1f1f; border: 1px solid #f1a3a3; padding: 8px 12px;
  border-radius: 8px; margin: 8px 0; font-weight: 600; }
.passed { background: #e9f6ee; color: #135c36; border: 1px solid #a4cdb5; padding: 8px 12px;
  border-radius: 8px; margin: 8px 0; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; margin: 6px 0; }
td, th { border: 1px solid #e2e7ec; padding: 5px 8px; text-align: left; vertical-align: top; }
th { background: #f1f4f7; font-weight: 620; }
.timeline li { margin-bottom: 4px; }
.ctl { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 10px 0; }
textarea { width: 100%; min-height: 70px; margin: 6px 0; border: 1px solid #c9d1d8;
  border-radius: 8px; padding: 8px; font: inherit; }
textarea.draft { min-height: 90px; }
input, select { border: 1px solid #c9d1d8; border-radius: 6px; padding: 6px 8px; font: inherit; }
form.intake { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 8px 10px; align-items: end; }
form.intake label { display: flex; flex-direction: column; gap: 3px; font-size: 12.5px;
  color: #55616d; font-weight: 600; }
form.intake label.wide { grid-column: 1 / -1; }
.vblock { border-top: 1px solid #e7ebef; margin-top: 12px; padding-top: 8px; }
.release-panel { position: sticky; top: 0; z-index: 5; background: #10231b; color: #eafbe7;
  border-radius: 12px; padding: 12px 14px; margin: 14px 0; display: none; }
.release-panel.visible { display: block; }
.release-panel input { flex: 1; min-width: 260px; background: #0c1a14; color: #c9f2cd;
  border: 1px solid #2e5a42; }
.release-panel .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.release-panel button { background: #2a7d5a; }
.release-panel button.ghost { background: transparent; color: #c9f2cd; border-color: #2e5a42; }
.error-box { background: #fdecec; color: #8f1f1f; border: 1px solid #f1a3a3; border-radius: 10px;
  padding: 10px 14px; margin: 10px 0; display: none; }
.error-box.visible { display: block; }
.empty-state { text-align: center; padding: 46px 12px; color: #55616d; }
.empty-state .big { font-size: 34px; }
.quote { border-left: 3px solid #c4ccd3; margin: 6px 0; padding: 4px 10px; color: #4a5763;
  font-style: italic; }
fieldset { border: 1px solid #dde3e8; border-radius: 12px; margin: 14px 0; padding: 12px 14px; }
legend { font-weight: 700; padding: 0 6px; }
.msg-line { min-height: 18px; }
"""

CLIENT_STYLE = BASE_STYLE + """
.chip { display: inline-block; background: #e3ecf7; color: #1c4e8a; border-radius: 999px;
  padding: 2px 10px; font-size: 12.5px; font-weight: 650; margin: 4px 4px 0 0; }
.chip.ok { background: #d9ed92; color: #274d16; }
p.accepted { background: #e9f6ee; color: #135c36; border: 1px solid #a4cdb5; padding: 10px 14px;
  border-radius: 10px; font-weight: 600; }
ul.phases { margin: 6px 0; padding-left: 20px; }
form.rc .ctl { display: flex; gap: 10px; flex-wrap: wrap; align-items: end; margin: 8px 0; }
label.rc-field { display: flex; flex-direction: column; gap: 3px; font-size: 12.5px;
  color: #55616d; font-weight: 600; }
"""


FIXTURE_INQUIRY1 = {
    "as_of": "2026-11-23",
    "comparable_ids": ["P-031", "P-036", "P-041", "P-044"],
    "offer": {
        "client": "Juniper Cafe", "format": "product_promo",
        "production_mode": "one_shoot_day", "shoot_days": 1, "locations": 1,
        "interviewees": 0, "scripting": "light", "deliverables": 3,
        "editing_level": "standard", "graphics": "basic_titles",
        "revision_rounds": 2, "caption_versions": 3, "deadline": "2026-12-11",
        "budget_sgd": 2700, "direct_costs_sgd": 0,
    },
}

FIXTURE_INQUIRY2 = {
    "as_of": "2026-11-23",
    "comparable_ids": ["P-031", "P-036"],
    "offer": {
        "client": "Harbor Fitness", "format": "social_reel",
        "production_mode": "edit_only", "shoot_days": 0, "locations": 0,
        "interviewees": 0, "scripting": "none", "deliverables": 2,
        "editing_level": "simple", "graphics": "none",
        "revision_rounds": 1, "caption_versions": 2, "deadline": "2026-12-18",
        "budget_sgd": 1500, "direct_costs_sgd": 0,
    },
}

FIXTURES = [
    ("LEAD-0001", "Inquiry 1 — Juniper Cafe (3 promo videos, S$2,700)",
     FIXTURE_INQUIRY1,
     "three 30-second Instagram promo videos, one location, captions, by 11 Dec, budget S$2,700"),
    ("LEAD-0002", "Inquiry 2 — Harbor Fitness (2 social reels, S$1,500)",
     FIXTURE_INQUIRY2,
     "two edited social reels from supplied footage, captions, by 18 Dec, budget S$1,500"),
]


_FIXTURES_JSON = json.dumps([
    {"source_id": sid, "label": label, "raw": raw, "offer_request": offer}
    for sid, label, offer, raw in FIXTURES])


def founder_page(conn):
    fixture_buttons = "".join(
        f'<button class="ghost" onclick="importFixture({i})">{_esc(label)}</button>'
        for i, (_sid, label, _offer, _raw) in enumerate(FIXTURES))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BidPilot — founder dashboard (private)</title>
<style>{BASE_STYLE}</style></head><body>
<header><span class="badge">{SAMPLE_BADGE}</span>
 <span class="badge dim">Private founder view — loopback only</span></header>
<main>
<h1>BidPilot founder dashboard</h1>
<p class="lede">Sample-only demo. Evaluate inbound inquiries with the real engine,
 review the outbound-checked options, release a client link, and confirm bookings.</p>

<div class="release-panel" id="release-panel">
  <b>Client link released</b>
  <div class="row">
    <input id="release-url" readonly aria-label="Client URL">
    <button onclick="copyRelease()">Copy URL</button>
    <button onclick="openRelease()">Open client preview</button>
    <button class="ghost" id="rotate-btn" onclick="rotateToken()">Rotate / reissue link</button>
    <button class="ghost" onclick="hideRelease()">Dismiss</button>
  </div>
  <p class="small" style="color:#a9c8b6">Only a hash is stored. If this URL is lost, use
   Rotate / reissue — the old link stops working immediately. The token is never logged.</p>
</div>
<div class="error-box" id="error-box" role="alert"></div>
<div class="ctl" aria-label="Dashboard updates">
  <button type="button" class="ghost" id="live-updates" aria-pressed="true">Live updates on</button>
  <button type="button" class="ghost" id="refresh-state">Refresh</button>
</div>

<fieldset><legend>Sample intake</legend>
  <p class="small">Raw inbound message plus explicit structured inputs — validated by the
   engine. Missing or invalid values are reported, never fabricated. A duplicate
   source ID resumes the existing deal without overwriting history.</p>
  <form class="intake" id="intake" onsubmit="return submitIntake(event)">
    <label class="wide">Raw inbound message
      <textarea id="in-raw" placeholder="Paste the inbound message here…"></textarea></label>
    <label>Source<select id="in-source"><option>hashmeta</option></select></label>
    <label>Source ID<input id="in-source_id" placeholder="LEAD-…" required></label>
    <label>Client<input id="in-client" required></label>
    <label>as_of (YYYY-MM-DD)<input id="in-as_of" value="2026-11-23" required></label>
    <label>Format<select id="in-format">
      <option>product_promo</option><option>social_reel</option><option>testimonial</option>
      <option>event_recap</option><option>explainer</option></select></label>
    <label>Production mode<select id="in-production_mode">
      <option>one_shoot_day</option><option>edit_only</option><option>multi_day</option>
      </select></label>
    <label>Shoot days<input id="in-shoot_days" type="number" min="0" value="1"></label>
    <label>Locations<input id="in-locations" type="number" min="0" value="1"></label>
    <label>Interviewees<input id="in-interviewees" type="number" min="0" value="0"></label>
    <label>Scripting<select id="in-scripting">
      <option>light</option><option>none</option><option>full</option></select></label>
    <label>Deliverables<input id="in-deliverables" type="number" min="1" value="3" required></label>
    <label>Editing level<select id="in-editing_level">
      <option>standard</option><option>simple</option><option>heavy</option></select></label>
    <label>Graphics<select id="in-graphics">
      <option>basic_titles</option><option>none</option><option>light_motion</option>
      <option>custom_animation</option></select></label>
    <label>Revision rounds<input id="in-revision_rounds" type="number" min="0" value="2"></label>
    <label>Caption versions<input id="in-caption_versions" type="number" min="0" value="3"></label>
    <label>Deadline<input id="in-deadline" value="2026-12-11" required></label>
    <label>Budget S$<input id="in-budget_sgd" type="number" step="0.01" value="2700" required></label>
    <label>Direct costs S$<input id="in-direct_costs_sgd" type="number" step="0.01" value="0"></label>
    <label class="wide">Comparable IDs (comma-separated)
      <input id="in-comparables" value="P-031, P-036, P-041, P-044"></label>
    <label class="wide"><button type="submit">Import inquiry</button>
      <button type="button" class="ghost" onclick="loadFixture(0)">Load Inquiry 1 into form</button>
      <button type="button" class="ghost" onclick="loadFixture(1)">Load Inquiry 2 into form</button></label>
  </form>
  <p id="intake-msg" class="msg-line small" role="status" aria-live="polite"></p>
</fieldset>

<fieldset><legend>Fixture loader (labeled sample data, distinct source IDs)</legend>
  <div class="ctl">{fixture_buttons}</div>
  <p class="small">Each fixture imports with its own stable source ID; re-importing the same
   fixture resumes that deal instead of creating a new one.</p>
</fieldset>

<fieldset><legend>Import Run JSON</legend>
  <form id="import-run" onsubmit="return submitRun(event)">
    <label>Run JSON file <input id="run-file" type="file" accept=".json,application/json"></label>
    <label for="run-json">Run JSON</label>
    <textarea id="run-json" required></textarea>
    <button type="submit">Import run</button>
  </form>
  <p id="run-status" class="msg-line small" role="status"></p>
</fieldset>

<fieldset><legend>Reset sample data</legend>
  <form id="reset-sample" onsubmit="return resetSample(event)">
    <label>Type <code>RESET SAMPLE DATA</code> <input name="confirm" required></label>
    <button type="submit" class="danger">Reset sample data</button>
  </form>
  <p id="reset-status" class="msg-line small" role="status"></p>
</fieldset>

<h2>Inbox</h2>
<div id="inbox" class="small">Loading inquiries…</div>

<h2>Aggregate planning capacity (private)</h2>
<div id="capacity" class="small">Loading capacity…</div>

<h2>Deals</h2>
<div id="root" class="empty-state"><span class="big">…</span><p>Loading deals…</p></div>
</main>
<script>
const OWNER = new URLSearchParams(location.search).get('owner') || '';
let currentRelease = {{version_id: null, url: null}};
let liveUpdates = true;
let pollTimer;
let stateRequest = 0;
let renderedDeals;
let resetRequestId;

async function api(path, data) {{
  try {{
    const res = await fetch(path, {{method: 'POST',
      headers: {{'Content-Type': 'application/json', 'X-Owner-Token': OWNER}},
      body: JSON.stringify(data || {{}})}});
    return {{status: res.status, data: await res.json()}};
  }} catch (e) {{
    showError('Request failed: ' + e);
    return {{status: 0, data: {{ok: false, error: String(e)}}}};
  }}
}}
function showError(text) {{
  const el = document.getElementById('error-box');
  el.textContent = text; el.classList.add('visible');
  setTimeout(() => el.classList.remove('visible'), 8000);
}}
document.getElementById('run-file').addEventListener('change', async event => {{
  const file = event.target.files[0];
  if (file) {{
    try {{
      document.getElementById('run-json').value = await file.text();
    }} catch (e) {{
      document.getElementById('run-status').textContent = 'Could not read the selected file.';
    }}
  }}
}});
async function submitRun(event) {{
  event.preventDefault();
  const status = document.getElementById('run-status');
  let bundle;
  try {{ bundle = JSON.parse(document.getElementById('run-json').value); }}
  catch (e) {{ status.textContent = 'Enter valid run JSON.'; return; }}
  const result = await api('/api/import-run', bundle);
  if (!result.data.ok) {{ status.textContent = 'Import failed. ' + result.data.error; return; }}
  const check = result.data.outbound_result.passed ? 'Outbound check passed.' : 'Outbound check blocked.';
  status.textContent = 'Imported run. ' + check + (result.data.mismatch
    ? ' Supplied results differ from the server results.' : '');
  refresh();
}}
async function resetSample(event) {{
  event.preventDefault();
  const status = document.getElementById('reset-status');
  resetRequestId ||= crypto.randomUUID();
  const result = await api('/api/reset-sample', {{confirm: event.target.elements.confirm.value,
    request_id: resetRequestId}});
  if (!result.data.ok) {{ status.textContent = 'Reset refused. ' + result.data.error; return; }}
  resetRequestId = null;
  status.textContent = 'Sample data reset. Deals removed: ' + result.data.counts.deals + '.';
  event.target.reset();
  hideRelease();
  refresh();
}}
const FIXTURES = {_FIXTURES_JSON};
function loadFixture(i) {{
  const f = FIXTURES[i];
  document.getElementById('in-raw').value = f.raw;
  document.getElementById('in-source_id').value = f.source_id;
  const o = f.offer_request.offer;
  for (const key of ['client','format','production_mode','scripting','editing_level',
                     'graphics','deadline']) {{
    const el = document.getElementById('in-' + key); if (el) el.value = o[key];
  }}
  for (const key of ['shoot_days','locations','interviewees','deliverables',
                     'revision_rounds','caption_versions']) {{
    const el = document.getElementById('in-' + key); if (el) el.value = o[key];
  }}
  document.getElementById('in-as_of').value = f.offer_request.as_of;
  document.getElementById('in-budget_sgd').value = o.budget_sgd;
  document.getElementById('in-direct_costs_sgd').value = o.direct_costs_sgd;
  document.getElementById('in-comparables').value = f.offer_request.comparable_ids.join(', ');
  return false;
}}
async function importFixture(i) {{
  const f = FIXTURES[i];
  const r = await api('/api/import', {{source: 'hashmeta', source_id: f.source_id,
    raw_inquiry: f.raw, offer_request: f.offer_request}});
  const el = document.getElementById('intake-msg');
  if (!r.data.ok) {{ el.textContent = 'Import failed: ' + r.data.error; return; }}
  el.textContent = r.data.duplicate
    ? 'Duplicate source ID — resumed existing deal (history preserved).'
    : 'Imported ' + f.source_id + ' as a new deal.';
  refresh();
}}
async function submitIntake(ev) {{
  ev.preventDefault();
  const num = id => Number(document.getElementById(id).value);
  const str = id => document.getElementById(id).value;
  const offer_request = {{
    as_of: str('in-as_of'),
    comparable_ids: str('in-comparables').split(',').map(s => s.trim()).filter(Boolean),
    offer: {{
      client: str('in-client'), format: str('in-format'),
      production_mode: str('in-production_mode'), shoot_days: num('in-shoot_days'),
      locations: num('in-locations'), interviewees: num('in-interviewees'),
      scripting: str('in-scripting'), deliverables: num('in-deliverables'),
      editing_level: str('in-editing_level'), graphics: str('in-graphics'),
      revision_rounds: num('in-revision_rounds'), caption_versions: num('in-caption_versions'),
      deadline: str('in-deadline'), budget_sgd: num('in-budget_sgd'),
      direct_costs_sgd: num('in-direct_costs_sgd'),
    }},
  }};
  const r = await api('/api/import', {{source: str('in-source'),
    source_id: str('in-source_id'), raw_inquiry: str('in-raw'), offer_request}});
  const el = document.getElementById('intake-msg');
  if (!r.data.ok) {{
    el.textContent = 'Engine validation failed — fix the reported fields: ' + r.data.error;
    return;
  }}
  el.textContent = r.data.duplicate
    ? 'Duplicate source ID — resumed existing deal (history preserved).'
    : 'Imported as a new deal.';
  refresh();
  return false;
}}
async function refresh() {{
  await refreshState(false);
}}
async function refreshState(automatic) {{
  if (automatic && (!liveUpdates || document.hidden)) return;
  const request = ++stateRequest;
  let st;
  try {{
    const res = await fetch('/api/state', {{headers: {{'X-Owner-Token': OWNER}}}});
    if (request !== stateRequest) return;
    if (res.status !== 200) {{
      document.getElementById('root').innerHTML =
        '<div class="empty-state"><p>Could not load deals (HTTP ' + res.status +
        '). Check the owner token in the URL.</p></div>';
      return;
    }}
    st = await res.json();
  }} catch (e) {{
    if (request === stateRequest) showError('Could not load state: ' + e);
    return;
  }}
  if (request !== stateRequest || (automatic && (!liveUpdates || document.hidden))) return;
  document.getElementById('inbox').innerHTML = st.inbox.length
    ? '<ul>' + st.inbox.map(i => `<li><b>${{esc(i.claimed ? 'Claimed' : 'Unclaimed')}}</b> <code>${{esc(i.source_id)}}</code> — ${{esc(i.fields.name_business)}}: ${{esc(i.fields.project_brief)}}</li>`).join('') + '</ul>'
    : '<p>No inquiries yet.</p>';
  const root = document.getElementById('root');
  const capEl = document.getElementById('capacity');
  if (st.capacity) {{
    const c = st.capacity;
    capEl.innerHTML = `<p class="muted">${{esc(c.label)}} · ledger revision ${{c.revision}}</p>`
      + `<table><tr><th>Week ending</th><th>Buffered budget</th><th>Confirmed allocations</th>`
      + `<th>Remaining</th></tr>`
      + c.weeks.map(w => `<tr><td>${{esc(w.week)}}</td><td>${{w.budget_hours.toFixed(2)}} h</td>`
        + `<td>${{w.allocated_hours.toFixed(2)}} h</td><td>${{w.remaining_hours.toFixed(2)}} h</td></tr>`).join('')
      + `</table><p class="small">${{esc(c.note)}}</p>`;
  }}
  const dealSnapshot = JSON.stringify(st.deals);
  if (dealSnapshot === renderedDeals) return;
  if (automatic && root.contains(document.activeElement)
      && document.activeElement.matches('input, textarea, select')) return;
  if (automatic && Array.from(root.querySelectorAll('input, textarea'))
      .some(field => field.value !== field.defaultValue)) return;
  renderedDeals = dealSnapshot;
  if (!st.deals.length) {{
    root.innerHTML = '<div class="empty-state"><span class="big">·</span>' +
      '<h2>No deals yet</h2><p class="muted">Load a labeled fixture above or fill in the' +
      ' sample intake form to start the demo workflow.</p></div>';
    return;
  }}
  root.classList.remove('empty-state');
  root.innerHTML = st.deals.map(dealHtml).join('');
}}
function schedulePolling() {{
  clearInterval(pollTimer);
  if (liveUpdates && !document.hidden) pollTimer = setInterval(() => refreshState(true), 4000);
}}
const liveButton = document.getElementById('live-updates');
liveButton.addEventListener('click', () => {{
  liveUpdates = !liveUpdates;
  liveButton.textContent = 'Live updates ' + (liveUpdates ? 'on' : 'off');
  liveButton.setAttribute('aria-pressed', liveUpdates);
  schedulePolling();
  if (liveUpdates) refreshState(true);
}});
document.getElementById('refresh-state').addEventListener('click', () => refresh());
document.addEventListener('visibilitychange', () => {{
  schedulePolling();
  if (!document.hidden && liveUpdates) refreshState(true);
}});
function money(v) {{ return v == null ? '—' : 'S$' + Number(v).toLocaleString('en-SG',
  {{minimumFractionDigits: 2}}); }}
function hours(value) {{ return Number(value).toFixed(2); }}
function esc(s) {{ const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }}
function statusChip(s) {{
  return '<span class="status-chip ' + esc(s) + '">' + esc(String(s).replace(/_/g,' ')) + '</span>';
}}
const STEP_LABELS = {{claimed: 'Claimed', resume: 'Claimed (resume)', intake: 'Intake',
  scoping: 'Scoping', engine: 'Engine', engine_evaluate: 'Engine (evaluation)',
  reevaluate: 'Engine (re-evaluation)', draft: 'Draft', edit_draft: 'Draft (edit)',
  outbound: 'Outbound', outbound_check: 'Outbound (check)', review: 'Review',
  approve: 'Review (approve)', reject: 'Review (reject)', client_change: 'Client change'}};
function timelineHtml(steps) {{
  if (!steps.length) return '';
  return `<ol class="timeline">${{steps.map(s => {{
    const start = new Date(s.started_at * 1000).toISOString();
    const label = STEP_LABELS[s.step_key] || s.step_key.replace(/_/g, ' ');
    return `<li><b>${{esc(label)}}</b> · ${{esc(s.status)}}
      <time class="small" datetime="${{start}}">${{start}}</time>
      ${{s.private_note ? `<div class="small">${{esc(s.private_note)}}</div>` : ''}}</li>`;
  }}).join('')}}</ol>`;
}}
function evidenceText(value) {{
  return esc(typeof value === 'object' ? JSON.stringify(value) : value);
}}
function evidenceTable(rows) {{
  if (!rows.length) return '';
  return '<table><thead><tr><th>Field</th><th>Evidence</th></tr></thead><tbody>'
    + rows.map(([field, value]) => `<tr><th scope="row">${{esc(field)}}</th>
      <td>${{evidenceText(value)}}</td></tr>`).join('') + '</tbody></table>';
}}
function clarificationHtml(value) {{
  if (!value || (Array.isArray(value) && !value.length)) return '';
  const text = Array.isArray(value) ? value.map(evidenceText).join('<br>') : evidenceText(value);
  return `<div class="blocked"><strong>Needs information, not sent</strong><div>${{text}}</div></div>`;
}}
function agentOutputHtml(output) {{
  const p = output.payload;
  switch (output.kind) {{
    case 'intake': {{
      const evidence = p.evidence && typeof p.evidence === 'object' ? p.evidence : p;
      return '<h4>Intake evidence</h4>' + evidenceTable(Object.entries(evidence))
        + clarificationHtml(p.clarification);
    }}
    case 'scoping':
      return '<h4>Scoping</h4><p>Comparables: ' + esc(p.comparable_ids.join(', ') || 'None') + '</p>'
        + evidenceTable(p.scope_profile.map(item =>
          [item.field, {{value: item.value, evidence: item.evidence}}]))
        + (p.differences.length ? '<h5>Comparable differences</h5><ul>' + p.differences.map(item =>
          `<li><b>${{esc(item.project_id)}}</b> · ${{evidenceText(item.fields)}} · ${{esc(item.why)}}
           <div class="small">${{evidenceText(item.evidence)}}</div></li>`).join('') + '</ul>' : '');
    case 'negotiator':
      return '<p><b>Negotiator option IDs</b> ' + esc(p.option_ids.join(', ') || 'None') + '</p>';
    case 'clarification':
      return clarificationHtml(p.questions);
    case 'imported_run':
      return '<p><b>Imported run</b></p>';
    default:
      return '';
  }}
}}
function dealHtml(d) {{
  const offerRows = d.offers.map(o => {{
    const fields = Object.entries(o.offer).map(([k, v]) =>
      '<code>' + esc(k) + '</code> ' + esc(String(v))).join(' · ');
    const action = o.evaluated ? '' :
      `<button onclick="doEvaluate('${{d.deal_id}}','${{o.offer_id}}')">Evaluate</button>`;
    return `<tr><td>Round ${{o.round}}</td><td>${{esc(o.as_of || '')}}</td>
      <td class="small">${{fields}}</td>
      <td>${{o.evaluated ? 'evaluated' : 'queued'}} ${{action}}</td></tr>`;
  }}).join('');
  const queuedHtml = d.queued_clarifications.length
    ? `<div class="blocked">Awaiting founder clarification: ${{d.queued_clarifications.map(q =>
        esc(q.private_note)).join(' | ')}}</div>` : '';
  const confirmBtn = d.versions.filter(v => v.status === 'published' && v.accepted_at)
    .map(v => `<button class="danger" id="confirm-${{v.version_id}}"
      onclick="doConfirm('${{v.version_id}}')">Confirm booking (sample only)</button>`).join('');
  const closeBtn = d.status === 'confirmed'
    ? `<button onclick="doClose('${{d.deal_id}}')">Close deal</button>` : '';
  const firstOffer = d.offers[0]?.offer_id;
  const roundForStep = s => s.offer_id || (['intake', 'resume'].includes(s.step_key) ? firstOffer : null);
  const rounds = d.offers.map(o => `<section aria-label="Run round ${{o.round}}">
    <h4>Round ${{o.round}}</h4>
    ${{timelineHtml(d.steps.filter(s => roundForStep(s) === o.offer_id))}}
    ${{d.agent_outputs.filter(a => a.offer_id === o.offer_id).map(agentOutputHtml).join('')}}
    </section>`).join('');
  const dealActivity = timelineHtml(d.steps.filter(s => !roundForStep(s)))
    + d.agent_outputs.filter(a => !a.offer_id).map(agentOutputHtml).join('');
  return `<section class="deal" id="deal-${{d.deal_id.slice(0,8)}}">
    <h2>${{esc(d.client)}} ${{statusChip(d.status)}}
      <span class="muted small">${{esc(d.source)}}/${{esc(d.source_id)}}</span></h2>
    <p class="quote">“${{esc(d.raw_inquiry || '')}}”</p>
    ${{queuedHtml}}
    <h3>Inbound offers &amp; rounds</h3>
    <table><tr><th>Round</th><th>as_of</th><th>Structured offer (engine-validated)</th>
      <th>State</th></tr>${{offerRows}}</table>
    ${{d.versions.map(v => versionHtml(d, v)).join('')}}
    <div class="ctl">${{confirmBtn}}${{closeBtn}}</div>
    <h3>Run steps</h3>
    ${{dealActivity ? '<h4>Deal activity</h4>' + dealActivity : ''}}
    ${{rounds}}
    <h3>Audit trail (approvals &amp; confirmations)</h3>
    <table><tr><th>action</th><th>note</th><th>digest</th></tr>
    ${{d.approvals.map(a => `<tr><td>${{esc(a.action)}}</td><td>${{esc(a.note)}}</td>
      <td><span class="small">${{esc(String(a.reviewed_digest||'').slice(0,12))}}…</span></td></tr>`).join('')}}</table>
    <h3>Messages</h3>
    <ul>${{d.messages.map(m => `<li><b>${{esc(m.direction)}}</b> ${{esc(m.body)}}
      ${{m.request_id ? '<code>'+esc(m.request_id)+'</code>' : ''}}</li>`).join('')}}</ul>
    <h3>Record a demo client change request</h3>
    <p class="small">Explicit structured terms — nothing is inferred from free text.
     The five captioned versions below are stated, not assumed.</p>
    <form class="intake" onsubmit="return submitChange(event, '${{d.deal_id}}')">
      <label>Videos<input id="chg-count-${{d.deal_id.slice(0,8)}}" value="5" type="number" min="1"></label>
      <label>Caption versions<input id="chg-caps-${{d.deal_id.slice(0,8)}}" value="5" type="number" min="0"></label>
      <label>Deadline<input id="chg-deadline-${{d.deal_id.slice(0,8)}}" value="2026-12-04"></label>
      <label>Budget S$<input id="chg-budget-${{d.deal_id.slice(0,8)}}" value="2700" type="number" step="0.01"></label>
      <label class="wide"><button type="submit">Queue change request</button></label>
    </form>
    <p class="msg-line small" id="msg-${{d.deal_id.slice(0,8)}}" role="status" aria-live="polite"></p>
   </section>`;
}}
function versionHtml(d, v) {{
  const est = v.estimate;
  const estHtml = est ? `
    <table>
      <tr><th>Base hours</th><td>${{hours(est.base_hours)}}</td><th>Buffer</th><td>${{est.buffer_pct}}%</td>
          <th>Planning hours</th><td><b>${{hours(est.planning_hours)}}</b></td></tr>
      <tr><th>Capacity</th><td>${{hours(est.capacity_hours)}}</td><th>List</th><td>${{money(est.list_sgd)}}</td>
          <th>Floor (internal)</th><td><b>${{money(est.floor_sgd)}}</b></td></tr>
      <tr><th>Effective rate</th><td>${{money(est.effective_rate_sgd)}}/h</td>
          <th>Verdict</th><td><b>${{esc(v.verdict)}}</b> ${{esc((v.reason_codes||[]).join(', '))}}</td>
          <th></th><td></td></tr>
    </table>` : '<p class="muted small">Not evaluated yet.</p>';
  const comps = v.comparables ? `
    <p class="small">Comparables used: ${{esc(v.comparables.used.map(c => c.project_id + ' (' + hours(c.unit_hours) + ' h/video)').join(', ') || 'none')}}<br>
    Excluded: ${{esc(v.comparables.excluded.map(c => c.project_id + ':' + c.fields.join('+')).join(', ') || 'none')}}</p>` : '';
  const meta = {{}};
  (v.option_meta || []).forEach(m => {{ meta[m.option_id] = m; }});
  const optCards = (v.options || []).map(o => {{
    const m = meta[o.option_id] || {{}};
    const rowId = m.option_row_id || '';
    const phases = (o.phases || []).map((p, i) =>
      `<li>Phase ${{i+1}}: ${{p.deliverables}} videos by ${{p.deadline}}</li>`).join('');
    const sel = rowId === v.selected_option_id ? ' selected' : '';
    const acc = v.accepted_option && v.accepted_option.option_id === rowId
      ? '<span class="chip ok">Client accepted</span>' : '';
    return `<div class="card option${{sel}}"><h3>${{esc(m.label || o.option_id)}}</h3>
      <p class="muted">${{o.deliverables}} videos by ${{o.deadline}}</p>
      <p class="price">${{money(o.price_sgd)}}</p>
      ${{phases ? `<ul class="phases">${{phases}}</ul>` : ''}}
      <p class="small">${{o.caption_versions}} caption versions · ${{o.revision_rounds}} revisions
        · kind: <code>${{esc(o.option_id)}}</code></p>${{acc}}</div>`;
  }}).join('');
  const banner = v.outbound_status === 'blocked'
    ? `<p class="blocked">Blocked by outbound check</p>`
    : (v.outbound_status === 'passed' ? `<p class="passed">Outbound check passed</p>` : '');
  const staleCap = v.capacity_stale
    ? `<p class="blocked">Capacity changed since this evaluation (another booking was`
      + ` confirmed). Re-evaluate before releasing — options may no longer fit.</p>` : '';
  const acceptInfo = v.accepted_option ? `
    <p class="passed">Client accepted <b>${{esc(v.accepted_option.terms ? v.accepted_option.terms.option_id : '')}}</b>
    — ${{v.accepted_option.terms ? v.accepted_option.terms.deliverables + ' videos by ' +
      v.accepted_option.terms.deadline + ' for ' + money(v.accepted_option.terms.price_sgd) : ''}}
    ${{v.confirmed ? '· booking confirmed' : '· awaiting your confirmation'}}</p>` : '';
  const canAct = ['draft','blocked','checked','evaluated'].includes(v.status) && !v.confirmed;
  const previewBtn = `<button class="ghost"
    onclick="openPreview('${{v.version_id}}')">Preview client page</button>`;
  const rotateBtn = (v.status === 'published' || v.status === 'confirmed')
    ? `<button class="ghost" onclick="doRotate('${{v.version_id}}')">Rotate / reissue client link</button>`
    : '';
  return `<div class="vblock">
    <h3>Version ${{v.version_no}} — ${{statusChip(v.status)}}
      <span class="muted small">outbound: ${{esc(v.outbound_status)}}</span></h3>
    ${{estHtml}}${{comps}}${{banner}}${{staleCap}}
    <div class="cards">${{optCards}}</div>
    ${{acceptInfo}}
    <textarea class="draft" id="draft-${{v.version_id}}">${{esc(v.draft)}}</textarea>
    <div class="ctl">
      ${{canAct ? `<button onclick="doCheck('${{v.version_id}}')">Check</button>
        <button onclick="doEdit('${{v.version_id}}')">Save draft (new version)</button>
        <button onclick="doApprove('${{v.version_id}}')">Approve &amp; release</button>
        <button class="danger" onclick="doReject('${{v.version_id}}')">Reject</button>` : ''}}
      ${{previewBtn}}${{rotateBtn}}
    </div>
    <div id="msg-${{v.version_id}}" role="status" aria-live="polite" class="small msg-line"></div>
  </div>`;
}}
function msg(id, text) {{
  const el = document.getElementById('msg-' + id);
  if (el) el.textContent = text;
  else showError(text);
}}
async function doEvaluate(dealId, offerId) {{
  const r = await api('/api/evaluate', {{deal_id: dealId, offer_id: offerId}});
  msg(offerId, r.data.ok ? 'Engine evaluation complete.' : 'Evaluate failed.');
  refresh();
}}
async function doCheck(id) {{ const r = await api('/api/check', {{version_id: id}});
  msg(id, r.data.ok ? 'Check passed.' : 'BLOCKED: ' + JSON.stringify(r.data.result.violations));
  refresh(); }}
async function doEdit(id) {{ const r = await api('/api/draft', {{version_id: id,
  body: document.getElementById('draft-' + id).value}});
  msg(id, r.data.ok ? 'New version created; a fresh check is required.' : 'failed'); refresh(); }}
function showRelease(versionId, url) {{
  currentRelease = {{version_id: versionId, url: url}};
  const panel = document.getElementById('release-panel');
  document.getElementById('release-url').value = location.origin + url;
  panel.classList.add('visible');
}}
function hideRelease() {{ document.getElementById('release-panel').classList.remove('visible'); }}
function copyRelease() {{
  const input = document.getElementById('release-url');
  input.select(); document.execCommand('copy');
  input.setSelectionRange(0, 0);
}}
function openRelease() {{ window.open(currentRelease.url, '_blank', 'noopener'); }}
function openPreview(versionId) {{
  window.open('/preview/' + versionId + '?owner=' + encodeURIComponent(OWNER),
    '_blank', 'noopener');
}}
async function doApprove(id) {{
  const r = await api('/api/approve', {{version_id: id}});
  if (r.data.ok) {{
    msg(id, 'Released.');
    showRelease(id, r.data.url);
  }} else {{
    msg(id, 'Refused: ' + (r.data.reasons || []).join(', '));
  }}
  refresh();
}}
async function doReject(id) {{ const r = await api('/api/reject', {{version_id: id}});
  msg(id, 'Rejected; nothing released.'); refresh(); }}
async function doRotate(versionId) {{
  const r = await api('/api/rotate-token', {{version_id: versionId}});
  if (r.data.ok) {{
    msg(versionId, 'Client link reissued; the old link no longer works.');
    showRelease(versionId, r.data.url);
  }} else {{
    msg(versionId, 'Rotate refused: ' + (r.data.reasons || []).join(', '));
  }}
}}
async function doConfirm(versionId) {{
  const r = await api('/api/confirm-booking', {{version_id: versionId}});
  if (r.data.ok) {{
    msg(versionId, r.data.already ? 'Already confirmed.' :
      'Sample booking confirmed on option ' + r.data.option_id + '. Nothing was sent.');
    const btn = document.getElementById('confirm-' + versionId);
    if (btn) btn.disabled = true;
  }} else {{
    if ((r.data.reasons || []).includes('capacity_changed')) {{
      msg(versionId, 'Confirm refused: capacity changed since evaluation. '
        + (r.data.next_action || 'Re-evaluate this offer, then check, review and release.'));
    }} else {{
      msg(versionId, 'Confirm refused: ' + (r.data.reasons || []).join(', ')
        + (r.data.next_action ? ' — ' + r.data.next_action : ''));
    }}
  }}
  refresh();
}}
async function doClose(dealId) {{
  const r = await api('/api/close-deal', {{deal_id: dealId}});
  msg(dealId.slice(0,8), r.data.ok ? 'Deal closed (sample only).' :
    'Close refused: ' + (r.data.reasons || []).join(', '));
  refresh();
}}
async function submitChange(ev, dealId) {{
  ev.preventDefault();
  const form = ev.target, k = dealId.slice(0, 8);
  const btn = form.querySelector("button[type='submit']");
  if (btn.disabled) return false;
  btn.disabled = true;
  const count = document.getElementById('chg-count-' + k).value;
  const caps = document.getElementById('chg-caps-' + k).value;
  const deadline = document.getElementById('chg-deadline-' + k).value;
  const budget = document.getElementById('chg-budget-' + k).value;
  const body = count + ' videos, ' + caps + ' captioned versions by ' + deadline +
    ' for S$' + budget;
  let h = 5381; const s = dealId + '|' + body;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
  const requestId = 'founder-' + h.toString(16);
  const r = await api('/api/client-change', {{deal_id: dealId,
    request_id: requestId, body: body}});
  msg(k, r.data.ok ? (r.data.status === 'clarify'
    ? 'Change request queued for clarification.'
    : 'Change request queued as a new offer round.') : JSON.stringify(r.data));
  btn.disabled = false;
  refresh();
  return false;
}}
refresh();
schedulePolling();
</script></body></html>"""

def make_server(db_path, port=0):
    """Build a server on loopback with a fresh per-launch owner token.

    Returns (server, owner_token). The owner token is only shown in the
    launching console; it is never logged or written to disk.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    try:
        core.load_seed(conn)
    finally:
        conn.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.db_path = db_path
    server.owner_token = secrets.token_urlsafe(24)
    return server, server.owner_token


def main():
    parser = argparse.ArgumentParser(description="Run the loopback-only BidPilot dashboard.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()
    server, owner_token = make_server(args.db, args.port)
    port = server.server_address[1]
    with server:
        print(f"BidPilot founder dashboard: http://127.0.0.1:{port}/?owner={owner_token}",
              flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
