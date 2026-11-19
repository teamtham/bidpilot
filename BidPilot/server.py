"""BidPilot Phase 3 demo server. Binds to 127.0.0.1 only — private local preview.

Owner routes require a per-launch owner session token; every state-changing
POST (founder and client token routes) requires a same-origin request.
"""
import json
import secrets
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core
import db

DB_PATH = Path(__file__).resolve().parent / "bidpilot.db"
SAMPLE_BADGE = core.SAMPLE_BADGE
OWNER_TOKEN = None  # set per launch by make_server()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    # -- plumbing ----------------------------------------------------------
    def open_db(self):
        # One connection per request: ThreadingHTTPServer serves requests on
        # multiple threads and SQLite connections are thread-bound.
        self.conn = db.connect(DB_PATH)

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, text, code=200, noindex=True):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
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
        return bool(supplied and OWNER_TOKEN
                    and secrets.compare_digest(supplied, OWNER_TOKEN))

    def same_origin(self):
        """State changes only from same-origin requests (Origin/Referer == Host)."""
        supplied = self.headers.get("Origin") or self.headers.get("Referer")
        if not supplied:
            return False
        host = self.headers.get("Host", "")
        netloc = urllib.parse.urlsplit(supplied).netloc
        return bool(netloc and netloc == host)

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        self.open_db()
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/" or path == "/dashboard":
                if not self.owner_ok():
                    return self.forbidden("owner token required")
                return self.send_html(founder_page(self.conn))
            if path == "/api/state":
                if not self.owner_ok():
                    return self.forbidden("owner token required")
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
        if path.startswith("/p/"):
            return self.token_post(path)
        if not self.owner_ok():
            return self.forbidden("owner token required")
        self.open_db()
        try:
            try:
                data = self.body()
            except json.JSONDecodeError:
                return self.send_json({"ok": False, "error": "bad_json"}, 400)

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

    # -- client view -------------------------------------------------------
    def client_page(self, token):
        version = core.published_by_token(self.conn, token)
        if not version:
            return self.not_found()
        return self.send_html(render_client_html(self.conn, version, preview=False))

    def preview_page(self, version_id):
        """Owner-only: exactly what the client will see, before approval."""
        version = conn_version(self.conn, version_id)
        if not version:
            return self.not_found()
        return self.send_html(render_client_html(self.conn, version, preview=True))


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
            "SELECT step_key, status, private_note, started_at FROM run_steps"
            " WHERE deal_id=? ORDER BY started_at", (deal["id"],)).fetchall()]
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
                      "steps": steps, "messages": messages, "approvals": approvals,
                      "queued_clarifications": queued})
    return {"badge": SAMPLE_BADGE, "capacity": panel, "deals": deals}


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

<h2>Aggregate planning capacity (private)</h2>
<div id="capacity" class="small">Loading capacity…</div>

<h2>Deals</h2>
<div id="root" class="empty-state"><span class="big">…</span><p>Loading deals…</p></div>
</main>
<script>
const OWNER = new URLSearchParams(location.search).get('owner') || '';
let currentRelease = {{version_id: null, url: null}};

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
  let st;
  try {{
    const res = await fetch('/api/state', {{headers: {{'X-Owner-Token': OWNER}}}});
    if (res.status !== 200) {{
      document.getElementById('root').innerHTML =
        '<div class="empty-state"><p>Could not load deals (HTTP ' + res.status +
        '). Check the owner token in the URL.</p></div>';
      return;
    }}
    st = await res.json();
  }} catch (e) {{
    showError('Could not load state: ' + e); return;
  }}
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
  if (!st.deals.length) {{
    root.innerHTML = '<div class="empty-state"><span class="big">·</span>' +
      '<h2>No deals yet</h2><p class="muted">Load a labeled fixture above or fill in the' +
      ' sample intake form to start the demo workflow.</p></div>';
    return;
  }}
  root.classList.remove('empty-state');
  root.innerHTML = st.deals.map(dealHtml).join('');
}}
function money(v) {{ return v == null ? '—' : 'S$' + Number(v).toLocaleString('en-SG',
  {{minimumFractionDigits: 2}}); }}
function esc(s) {{ const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s); return d.innerHTML; }}
function statusChip(s) {{
  return '<span class="status-chip ' + esc(s) + '">' + esc(String(s).replace(/_/g,' ')) + '</span>';
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
    <ol class="timeline">${{d.steps.map(s =>
      `<li><code>${{esc(s.step_key)}}</code> <b>${{esc(s.status)}}</b><br>
       <span class="small">${{esc(s.private_note)}}</span></li>`).join('')}}</ol>
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
      <tr><th>Base hours</th><td>${{est.base_hours}}</td><th>Buffer</th><td>${{est.buffer_pct}}%</td>
          <th>Planning hours</th><td><b>${{est.planning_hours}}</b></td></tr>
      <tr><th>Capacity</th><td>${{est.capacity_hours}}</td><th>List</th><td>${{money(est.list_sgd)}}</td>
          <th>Floor (internal)</th><td><b>${{money(est.floor_sgd)}}</b></td></tr>
      <tr><th>Effective rate</th><td>S$${{est.effective_rate_sgd}}/h</td>
          <th>Verdict</th><td><b>${{esc(v.verdict)}}</b> ${{esc((v.reason_codes||[]).join(', '))}}</td>
          <th></th><td></td></tr>
    </table>` : '<p class="muted small">Not evaluated yet.</p>';
  const comps = v.comparables ? `
    <p class="small">Comparables used: ${{v.comparables.used.map(c => c.project_id + ' (' + c.unit_hours.toFixed(2) + ' h/video)').join(', ') || 'none'}}<br>
    Excluded: ${{v.comparables.excluded.map(c => c.project_id + ':' + c.fields.join('+')).join(', ') || 'none'}}</p>` : '';
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
</script></body></html>"""

def make_server(db_path, port=0):
    """Build a server on loopback with a fresh per-launch owner token.

    Returns (server, owner_token). The owner token is only shown in the
    launching console; it is never logged or written to disk.
    """
    global DB_PATH, OWNER_TOKEN
    DB_PATH = Path(db_path)
    OWNER_TOKEN = secrets.token_urlsafe(24)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(DB_PATH)
    core.load_seed(conn)
    conn.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server, OWNER_TOKEN


def main():
    server, owner_token = make_server(DB_PATH)
    host, port = server.server_address[:2]
    print(f"BidPilot founder dashboard: http://127.0.0.1:{port}/?owner={owner_token} "
          f"(loopback only, per-launch owner session token shown once here)")
    server.serve_forever()


if __name__ == "__main__":
    main()
