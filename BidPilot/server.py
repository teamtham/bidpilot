"""BidPilot Phase 3 demo server. Binds to 127.0.0.1 only — private local preview.

Owner routes require a per-launch owner session token; every state-changing
POST (founder and client token routes) requires a same-origin request.
"""
import argparse
import os
import hashlib
import html
import json
import math
import re
import secrets
import sqlite3
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
import commerce
import conversation
import estimating
import portfolio
import qwenwork_callback
from studio_modules import modules
import db

DB_PATH = Path(__file__).resolve().parent / "bidpilot.db"
SAMPLE_BADGE = core.SAMPLE_BADGE
QUOTE_LIMIT = 16 * 1024
_quote_lock = threading.Lock()
_quote_times = []


def https_origin(value):
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?", value):
        raise ValueError("Expected an HTTPS origin with a hostname and optional port")
    origin = urllib.parse.urlsplit(value)
    if origin.port == 0:
        raise ValueError("Invalid origin port")
    return value.lower()

QUOTE_TEXT = ("name_business", "reply_email", "project_brief", "video_format",
              "production_mode", "scripting", "editing_level", "graphics",
              "deadline", "travel_kit_talent")
QUOTE_COUNTS = ("shoot_days", "locations", "interviewees", "deliverables",
                "revision_rounds", "caption_versions")


def validate_quote(data, form=False):
    if not isinstance(data, dict):
        raise ValueError("Expected an object")
    allowed = set(QUOTE_TEXT + QUOTE_COUNTS + ("budget_sgd", "caption_every_deliverable", "request_id", "resume_proof"))
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
        self._owner_cookie_header()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, text, code=200, noindex=True):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "same-origin")
        if self.path.split("?", 1)[0] == "/inquire" or self.path.startswith("/i/"):
            embed = getattr(self.server, "embed_origins", None) or []
            self.send_header("Content-Security-Policy", "frame-ancestors 'self'" + "".join(" " + e for e in embed))
        else:
            self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        if noindex:
            self.send_header("X-Robots-Tag", "noindex, nofollow")
        self._owner_cookie_header()
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
        data = json.loads(raw.decode("utf-8") or "{}", parse_constant=reject_nonfinite)
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
        return data

    # -- auth --------------------------------------------------------------
    def query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    OWNER_COOKIE = "bidpilot_owner"

    def _owner_cookie(self):
        jar = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key:
                jar[key] = value
        return jar.get(self.OWNER_COOKIE)

    def _owner_cookie_header(self):
        # Set only after a valid token arrived in the URL or header on the
        # loopback host. HttpOnly + SameSite=Strict, so other sites can't use
        # it and page scripts can't read it; the public tunnel host never sees
        # it (different host) and owner_ok refuses that host regardless.
        if getattr(self, "_issue_owner_cookie", False):
            self.send_header("Set-Cookie", f"{self.OWNER_COOKIE}={self.server.owner_token};"
                             " Path=/; HttpOnly; SameSite=Strict")
            self._issue_owner_cookie = False

    def owner_ok(self):
        """Founder routes need the owner token (URL, header or session cookie)."""
        public = getattr(self.server, "public_origin", None)
        if public and self.headers.get("Host", "").lower() == urllib.parse.urlsplit(public).netloc:
            return False
        expected = self.server.owner_token.encode("ascii")
        supplied = self.headers.get("X-Owner-Token") or (self.query().get("owner") or [None])[0]
        if supplied and secrets.compare_digest(supplied.encode("utf-8"), expected):
            if self._owner_cookie() != self.server.owner_token:
                self._issue_owner_cookie = True
            return True
        cookie = self._owner_cookie()
        return bool(cookie and secrets.compare_digest(cookie.encode("utf-8"), expected))

    def same_origin(self):
        """Require the HTTP origin of the loopback server on every write."""
        supplied = self.headers.get("Origin") or self.headers.get("Referer")
        if not supplied:
            return False
        try:
            origin = urllib.parse.urlsplit(supplied)
        except ValueError:
            return False
        host = self.headers.get("Host", "").lower()
        if not origin.netloc or origin.netloc.lower() != host:
            return False
        public = getattr(self.server, "public_origin", None)
        if public and host == urllib.parse.urlsplit(public).netloc:
            return origin.scheme == "https"
        return origin.scheme == "http"

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        self.open_db()
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if (path == "/estimating" or path.startswith("/api/estimating/")) and not self.owner_ok():
                return self.forbidden("owner token required")
            if any(module.dispatch_get(self, path) for module in modules()):
                return
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
        if path == qwenwork_callback.PATH:
            # Server-to-server: authenticated by the shared secret, not origin.
            return qwenwork_callback.handle(self)
        if not self.same_origin():
            return self.forbidden("same-origin request required")
        if path == "/inquire" or path.startswith("/i/"):
            self.open_db()
            try:
                if not conversation.dispatch_post(self, path):
                    self.not_found()
            finally:
                self.conn.close()
            return
        if path == "/quote":
            return self.quote_post()
        if path.startswith("/p/"):
            return self.token_post(path)
        if not self.owner_ok():
            return self.forbidden("owner token required")
        self.open_db()
        try:
            if commerce.dispatch_post(self, path) or portfolio.dispatch_post(self, path):
                return
            try:
                data = self.body()
            except (ValueError, RecursionError):
                return self.send_json({"ok": False, "error": "bad_json"}, 400)
            try:
                return self.owner_route(path, data)
            except (KeyError, TypeError):
                # Malformed owner payload (missing/None ids or fields) is a
                # clean 400, never an unhandled traceback / dropped connection.
                return self.send_json({"ok": False, "error": "bad_request"}, 400)
        finally:
            self.conn.close()

    def owner_route(self, path, data):
        if estimating.dispatch_post(self, path, data):
            return
        if path == "/api/capacity-preview":
            self.conn.execute("BEGIN")
            try:
                result = core.cap.booking_preview(self.conn, data["version_id"], data["option_id"])
            except ValueError as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
            return self.send_json({"ok": True, **result})
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
            if data.get("source") not in ("site", "hashmeta", "manual"):
                # Same allowlist as the MCP bridge. Internal namespaces
                # (e.g. change:<request_id>) must never be importable as a
                # deal source: they would alias another deal's offer rows.
                return self.send_json({"ok": False, "error": "invalid_source"}, 400)
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
            try:
                eval_id, version_id = core.evaluate(self.conn, data["deal_id"], data["offer_id"])
            except ValueError as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
            return self.send_json({"ok": True, "eval_id": eval_id, "version_id": version_id})

        if path == "/api/check":
            outcome = core.check_draft(self.conn, data["version_id"])
            return self.send_json({"ok": outcome["passed"], "result": outcome})

        if path == "/api/draft":
            new_id = core.save_draft(self.conn, data["version_id"], data["body"])
            return self.send_json({"ok": True, "version_id": new_id})

        if path == "/api/approve":
            res = core.approve(self.conn, data["version_id"], data.get("option_id"),
                               offered_option_ids=data.get("option_ids"))
            return self.send_json(res, 200 if res["ok"] else 409)

        if path == "/api/reject":
            res = core.reject(self.conn, data["version_id"], data.get("note", ""))
            return self.send_json(res, 200 if res["ok"] else 409)

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
                status = 409 if str(exc) == "request_id_conflict" else 400
                return self.send_json({"ok": False, "error": str(exc)}, status)
            if offer_id is None:
                return self.send_json({"ok": True, "status": "clarify", "offer_id": None,
                                       "message": "Change request queued for clarification"}, 202)
            return self.send_json({"ok": True, "status": "offered", "offer_id": offer_id})
        return self.not_found()

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
            proof = data.get('resume_proof')
            if proof is not None and (not isinstance(proof, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', proof)):
                raise ValueError('invalid_resume_proof')
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
                receipt = self.conn.execute('SELECT i.token_hash FROM inquiry_receipts r JOIN inquiries i ON i.id=r.inquiry_id WHERE r.source_id=? AND i.revoked=0', (result['source_id'],)).fetchone()
                if receipt and proof and secrets.compare_digest(receipt['token_hash'], hashlib.sha256(proof.encode()).hexdigest()):
                    result['inquiry_url'] = '/i/' + proof
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
                if proof:
                    conversation.create_receipt_inquiry(self.conn, source_id, proof, fields)
                self.conn.execute("INSERT INTO mcp_operations (request_id, tool, payload_hash, result_json, created_at) VALUES (?, 'quote', ?, ?, ?)",
                    (request_id, payload_hash, db.j(result), db.now()))
                self.conn.commit()
                if proof:
                    result['inquiry_url'] = '/i/' + proof
            if not form:
                return self.send_json(result)
            link = '<p><a href="'+html.escape(result['inquiry_url'], quote=True)+'">Continue your private inquiry</a></p><p>Save this private link to return.</p>' if result.get('inquiry_url') else ''
            return self.send_html('<!doctype html><html lang="en"><meta name="referrer" content="no-referrer"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Request received</title><h1>Request received</h1><p>Reference: ' + html.escape(result["source_id"]) + '</p>'+link+'</html>')
        finally:
            self.conn.close()

    def token_post(self, path):
        """Client token routes: same-origin enforced, no owner token involved."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.send_json({"ok": False, "error": "bad_request"}, 400)
        if length < 0 or length > QUOTE_LIMIT:
            # Anonymous routes get the same bounded body as the quote form:
            # a leaked link must not allow unbounded payloads.
            return self.send_json({"ok": False, "error": "body_too_large"}, 413)
        self.open_db()
        try:
            try:
                data = self.body()
            except (ValueError, RecursionError):
                return self.send_json({"ok": False, "error": "bad_json"}, 400)

            creative = re.fullmatch(r"/p/([^/]+)/creative-change", path)
            portfolio_search = re.fullmatch(r"/p/([^/]+)/portfolio-search", path)
            if portfolio_search:
                try:
                    if data.get('poll') is True:
                        result = portfolio.search_status(self.conn, portfolio_search[1], data.get('request_id'))
                    else:
                        result = portfolio.client_search(self.conn, portfolio_search[1], data)
                    return self.send_json({'ok': True, **result})
                except (KeyError, TypeError, ValueError):
                    return self.send_json({'ok': False, 'error': 'request_unavailable'}, 409)
            if creative:
                try:
                    return self.send_json(commerce.creative_change(self.conn, creative[1], data), 202)
                except (KeyError, TypeError, ValueError) as exc:
                    return self.send_json({"ok":False, "error":str(exc)}, 409)
            amendment = re.fullmatch(r"/p/([^/]+)/change-order/([^/]+)/accept", path)
            additional = re.fullmatch(r"/p/([^/]+)/additional-work", path)
            if amendment or additional:
                try:
                    if amendment:
                        result = commerce.accept_order(self.conn, *amendment.groups())
                    else:
                        result = commerce.request_change(self.conn, additional[1], data.get("body"), data.get("request_id"), data.get("reference"))
                    return self.send_json(result)
                except (ValueError, KeyError, TypeError):
                    return self.send_json({"ok": False, "error": "request_unavailable"}, 409)
            if path.endswith("/accept"):
                token = path[3:-7]
                res = core.client_accept(self.conn, token, data.get("option_id"))
                code = res.pop("http", 200) if not res["ok"] else 200
                return self.send_json(res, code)

            if path.endswith("/request-change"):
                token = path[3:-15]
                version = core.published_by_token(self.conn, token)
                if not version or version["status"] != "published":
                    # Confirmed bookings no longer take changes on the client
                    # link; generic 404, same as any invalid token.
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
                    status = 409 if str(exc) == "request_id_conflict" else 400
                    return self.send_json({"ok": False, "error": str(exc)}, status)
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
            valid = not core.release_integrity_errors(self.conn, version)
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
<style>__QUOTE_STYLE__</style></head>
<body><header><span class="badge">__SAMPLE_BADGE__</span></header>
<main><h1>Request a quote</h1>
<p><a href="/inquire">Discuss your brief and upload a reference</a></p>
<p class="lede">Tell us about your video project. We reply with a clear, fixed-price proposal.</p>
<form class="quote-form" method="post" action="/quote"><input type="hidden" name="request_id" value="QUOTE_REQUEST_ID"><input type="hidden" name="resume_proof" value="QUOTE_RESUME_PROOF">
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
<label><span class="check"><input type="checkbox" name="caption_every_deliverable"> Caption every deliverable</span></label>
<label>Delivery deadline<input type="date" name="deadline" required></label>
<label>SGD budget<input type="number" name="budget_sgd" min="0" step="0.01" required></label>
<label>Travel, kit or talent needs<textarea name="travel_kit_talent"></textarea></label>
<div class="actions"><button type="button" class="ghost" id="preset">Use demo preset</button><button type="submit">Submit request</button></div></form>
</main>
<script>document.getElementById('preset').onclick = () => {const f=document.querySelector('form');
for (const [k,v] of Object.entries({editing_level:'standard',graphics:'basic_titles',scripting:'light',revision_rounds:2,interviewees:0,shoot_days:1,locations:1,production_mode:'on_location'})) f.elements[k].value=v;};</script></body></html>"""
    return (page.replace("QUOTE_REQUEST_ID", secrets.token_urlsafe(18))
                .replace("QUOTE_RESUME_PROOF", secrets.token_urlsafe(32))
                .replace("__SAMPLE_BADGE__", SAMPLE_BADGE)
                .replace("__QUOTE_STYLE__", QUOTE_STYLE))


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
    option_rows = core.offered_options_of(conn, version)
    selected = version["selected_option_id"]
    selected_terms = next((db.uj(r["public_terms_json"]) for r in option_rows
                           if r["id"] == selected), None)
    if selected_terms is None and option_rows:
        selected_terms = db.uj(option_rows[0]["public_terms_json"])
    selected_terms = selected_terms or {}
    try:
        composed = commerce._page(db.uj(version["page_json"]) if version["page_json"] else {})
    except (KeyError, TypeError, ValueError):
        composed = {"headline": "", "introduction": "", "sections": [], "portfolio": []}
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
                      ("<button type='button' disabled>Booking confirmed</button>"
                       if version["status"] == "confirmed" else
                       "<button type='button' disabled>Accepted — awaiting Alex</button>"))
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
        status_banner = (
            "<p class='accepted'>Thank you — your booking is confirmed. Alex will be in touch.</p>"
            if version["status"] == "confirmed" else
            "<p class='accepted'>Thank you — your acceptance is recorded"
            " and awaits confirmation from Alex.</p>")
        change_form = ""
    elif preview:
        status_banner = "<p class='chip'>Owner preview — nothing has been sent.</p>"
        change_form = ""
    else:
        status_banner = ""
        change_form = f"""
        <form class="rc" method="post" onsubmit="return submitChangeForm(event)">
          <label for="rc">Request a change</label>
          <textarea id="rc" name="body" placeholder=
            "State what you want changed: number of videos, captioned versions, deadline and budget."></textarea>
          <p class="small">Your message is kept exactly as written. The explicit
           counters below state the terms — nothing is inferred from the free text.</p>
          <div class="ctl">
            <label class="rc-field">Videos
              <input id="rc-count" type="number" min="1" value="{int(selected_terms.get('deliverables', 1))}"></label>
            <label class="rc-field">Captioned versions
              <input id="rc-captions" type="number" min="0" value="{int(selected_terms.get('caption_versions', 0))}"></label>
            <label class="rc-field">Deadline
              <input id="rc-deadline" type="date" value="{_esc(selected_terms.get('deadline', ''))}"></label>
            <label class="rc-field">Budget S$
              <input id="rc-budget" type="number" step="0.01" value="{selected_terms.get('price_sgd', 0)}"></label>
            <button type="submit">Send change request</button>
          </div>
        </form>"""
    title = "Alex Studio — your proposal" if not preview else "Owner preview — client page"
    story_html = commerce.proposal_html(conn, version, preview)
    if composed["headline"]:
        story_html = story_html.replace('<section class="proposal-story"><h2>' + _esc(composed["headline"]) + '</h2>', '<section class="proposal-story">', 1)
    if composed["introduction"]:
        story_html = story_html.replace('<section class="proposal-story"><p>' + _esc(composed["introduction"]) + '</p>', '<section class="proposal-story">', 1)
    featured = (portfolio.public_asset(conn, composed["portfolio"][0].get("asset_id", ""))
                if composed["portfolio"] else None)
    if featured:
        src = _esc(featured["media_url"])
        media = (f'<video controls preload="metadata" src="{src}" aria-label="Featured creative reference"></video>'
                 if featured["mime"].startswith("video/") else
                 f'<img src="{src}" alt="{_esc(featured["title"])}">')
    else:
        media = '<div class="preview-placeholder" aria-hidden="true">Alex Studio</div>'
    has_composed = any((composed["headline"], composed["introduction"], composed["sections"], composed["portfolio"]))
    nav = ("<nav class='proposal-nav' aria-label='Proposal sections'><a href='#concept'>Concept</a>"
           "<a href='#portfolio'>References</a><a href='#packages'>Packages</a></nav>" if has_composed else "")
    hero_title = composed["headline"] or title
    hero_intro = composed["introduction"] or version["draft_body"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{_esc(title)}</title>
<style>{CLIENT_STYLE}</style></head><body>
<header class="client-header"><strong>Alex Studio</strong><span class="badge">{SAMPLE_BADGE}</span>{nav}</header>
<main class="client-main"><section class="proposal-hero" id="concept"><div><span class="eyebrow">Proposal</span><h1>{_esc(hero_title)}</h1>
<p class="lede">{_esc(hero_intro)}</p></div><div class="featured-preview" id="portfolio">{media}</div></section>
<section class="proposal-content">{story_html}</section>
{status_banner}
<section id="packages"><h2>Choose your package</h2><div class="cards">{''.join(cards)}</div></section>
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
  // Two independent hashes (djb2 + sdbm) make a collision between DIFFERENT
  // payloads astronomically unlikely; the server additionally rejects any
  // request_id reuse whose payload differs.
  const s = location.pathname + '|' + JSON.stringify(payload);
  let h = 5381, g = 0;
  for (let i = 0; i < s.length; i++) {{
    const c = s.charCodeAt(i);
    h = ((h << 5) + h + c) >>> 0;
    g = (c + (g << 6) + (g << 16) - g) >>> 0;
  }}
  return 'req-' + h.toString(16) + '-' + g.toString(16);
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


# Founder-only "What changed": diff a revised offer against its prior round.
# Scope drivers the comparison surfaces explicitly (other offer keys are still
# diffed generically). Values are never fabricated; absent legacy evidence is
# reported as None so the UI can say "not recorded".
SCOPE_DRIVER_FIELDS = ("deliverables", "caption_versions", "deadline", "budget_sgd",
                       "shoot_days", "locations", "interviewees", "scripting",
                       "editing_level", "graphics", "revision_rounds", "format",
                       "production_mode", "direct_costs_sgd")


def _offer_comparison(offer_map, version_offer, v):
    """Return the founder-only comparison for one proposal version.

    Links the version's offer to the prior round (via from_version_id, else the
    highest lower round) and lists the actual changed offer fields old -> new.
    Handles absent legacy evidence (no prior offer, missing/null fields) without
    fabricating values.
    """
    cur = offer_map.get(v["offer_id"])
    if not cur:
        return {"is_revision": False, "prior_round": None, "prior_offer_id": None,
                "changed_fields": [], "offer": {}}
    cur_offer = cur.get("offer") or {}
    prior_id = None
    from_version = cur.get("from_version_id")
    if from_version and from_version in version_offer:
        prior_id = version_offer[from_version]
    if prior_id is None and cur.get("round") and cur["round"] > 1:
        lower = [o for o in offer_map.values()
                 if o.get("round") and o["round"] < cur["round"]]
        if lower:
            prior_id = max(lower, key=lambda o: o["round"])["offer_id"]
    prior = offer_map.get(prior_id) if prior_id else None
    if not prior:
        return {"is_revision": False, "prior_round": None, "prior_offer_id": None,
                "changed_fields": [], "offer": cur_offer}
    prior_offer = prior.get("offer") or {}
    # Known scope drivers first (stable order), then any other changed keys.
    keys = [k for k in SCOPE_DRIVER_FIELDS if k in prior_offer or k in cur_offer]
    keys += sorted(k for k in (set(prior_offer) | set(cur_offer)) if k not in keys)
    changed = [{"field": k, "old": prior_offer.get(k), "new": cur_offer.get(k)}
               for k in keys if prior_offer.get(k) != cur_offer.get(k)]
    return {"is_revision": True, "prior_round": prior.get("round"),
            "prior_offer_id": prior_id, "changed_fields": changed, "offer": cur_offer}


_SIMILAR_STOP = {"a", "an", "and", "the", "of", "for", "by", "to", "in", "on", "with", "our",
                 "we", "s", "sgd", "budget", "video", "videos", "one", "two", "three", "dec", "nov"}


def similar_work(conn, deal, versions):
    """Founder-only: rank shared portfolio pieces against this deal.

    Pieces from the engine's own comparable past jobs rank first, then pieces
    whose title/tags share words with the inquiry and format. Read-only.
    """
    try:
        rows = conn.execute("SELECT id, project_id, title, description, tags FROM portfolio_assets"
                            " WHERE shared=1 ORDER BY title").fetchall()
    except sqlite3.Error:
        return []
    latest = versions[-1] if versions else {}
    used = {c.get("project_id") for c in ((latest.get("comparables") or {}).get("used") or [])}
    offer_row = conn.execute("SELECT offer_json FROM offers WHERE deal_id=? ORDER BY round DESC LIMIT 1",
                             (deal["id"],)).fetchone()
    offer = db.uj(offer_row["offer_json"]) if offer_row else {}
    def words_of(text):
        return set(re.findall(r"[a-z]+", text.casefold().replace("é", "e"))) - _SIMILAR_STOP
    brief = words_of((deal["raw_inquiry"] or "") + " " + (deal["client"] or ""))
    fmt = words_of(str((offer or {}).get("format") or "").replace("_", " "))
    words = brief | fmt
    ranked = []
    for r in rows:
        tags = set(re.findall(r"[a-z]+", (r["tags"] + " " + r["title"]).casefold().replace("é", "e")))
        hits = sorted(words & tags)
        past = r["project_id"] in used
        score = (3 if past else 0) + 2 * len(brief & tags) + len(fmt & tags)
        if past and hits:
            reason = "Picked from your portfolio: a similar past job, also " + ", ".join(hits[:3])
        elif past:
            reason = "Picked from your portfolio: one of the similar past jobs behind this estimate"
        elif hits:
            reason = "Picked from your portfolio: matches " + ", ".join(hits[:3])
        else:
            reason = "From your portfolio library (no close match yet)"
        ranked.append((score, {"asset_id": r["id"], "project_id": r["project_id"],
                               "title": r["title"], "reason": reason}))
    ranked.sort(key=lambda pair: -pair[0])
    return [item for _, item in ranked]


def state(conn):
    panel = capacity_panel(conn)
    current_revision = panel["revision"]
    deals = []
    for deal in conn.execute("SELECT * FROM deals ORDER BY created_at").fetchall():
        offer_rows = conn.execute("SELECT * FROM offers WHERE deal_id=? ORDER BY round",
                                  (deal["id"],)).fetchall()
        offer_map = {o["id"]: {"offer_id": o["id"], "round": o["round"],
                               "from_version_id": o["from_version_id"],
                               "offer": db.uj(o["offer_json"]) or {}} for o in offer_rows}
        version_rows = conn.execute(
            "SELECT * FROM proposal_versions WHERE deal_id=? ORDER BY version_no",
            (deal["id"],)).fetchall()
        version_offer = {v["id"]: v["offer_id"] for v in version_rows}
        versions = []
        for v in version_rows:
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
                "offered_option_ids": db.uj(v["offered_option_ids_json"]) if "offered_option_ids_json" in v.keys() and v["offered_option_ids_json"] else None,
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
                "impacts": [db.uj(row[0]) for row in conn.execute(
                    "SELECT details FROM capacity_impacts WHERE version_id=? ORDER BY rowid DESC", (v["id"],))],
                "pricing_evidence": (result or {}).get("historical_pricing"),
                "comparison": _offer_comparison(offer_map, version_offer, v),
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
            "SELECT offer_id, step_key, status, private_note, started_at, finished_at,"
            ' "trigger" FROM run_steps WHERE deal_id=? ORDER BY started_at, rowid',
            (deal["id"],))]
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
        queued = [s for s in steps if s["status"] == "queued"
                  and s["step_key"] != "needs_information"]
        needs_info = None
        if deal["status"] == "needs_information":
            ni = [s for s in steps if s["step_key"] == "needs_information"]
            if ni:
                try:
                    needs_info = db.uj(ni[-1]["private_note"])
                except (ValueError, TypeError):
                    needs_info = None
        deals.append({"deal_id": deal["id"], "source": deal["source"],
                      "source_id": deal["source_id"], "client": deal["client"],
                      "status": deal["status"], "raw_inquiry": deal["raw_inquiry"],
                      "versions": versions, "offers": offers,
                      "steps": steps, "agent_outputs": agent_outputs,
                      "messages": messages, "approvals": approvals,
                      "queued_clarifications": queued, "needs_information": needs_info,
                      "similar_work": similar_work(conn, deal, versions)})
    inbox = [{"source_id": r["source_id"], "fields": db.uj(r["fields_json"]),
              "received_at": r["received_at"], "claimed": bool(r["deal_id"])}
             for r in conn.execute("SELECT i.*, d.id AS deal_id FROM inbox i LEFT JOIN deals d ON d.source=i.source AND d.source_id=i.source_id ORDER BY i.received_at DESC").fetchall()]
    return {"badge": SAMPLE_BADGE, "capacity": panel, "deals": deals, "inbox": inbox}


# One shared design system for the dashboard, quote form and client page.
# Calm neutral surfaces, a single teal accent, a clear type scale, generous
# spacing, pill statuses and a prominent red blocked-outbound banner.
_DESIGN_BASE = """
:root {
  color-scheme: light;
  --bg: #f4f6f8; --surface: #ffffff; --border: #e2e8f0; --border-strong: #cbd5e1;
  --text: #1e293b; --muted: #5b6779;
  --accent: #0f766e; --accent-strong: #0b5d56; --accent-soft: #e6f2f0;
  --danger: #b91c1c; --danger-bg: #fef2f2; --danger-border: #fca5a5;
  --ok: #15803d; --ok-bg: #ecfdf3; --ok-border: #a7f3d0;
  --warn: #92600a; --warn-bg: #fff7e6; --warn-border: #fcd34d;
  --neutral-bg: #eef2f6; --neutral-text: #475569;
  --radius: 14px; --radius-sm: 9px;
  --shadow: 0 1px 2px rgba(15,23,42,.05), 0 4px 14px rgba(15,23,42,.04);
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
* { box-sizing: border-box; }
body { font: 16px/1.6 var(--font); margin: 0; background: var(--bg); color: var(--text);
  -webkit-font-smoothing: antialiased; }
header { background: var(--surface); border-bottom: 1px solid var(--border);
  padding: 14px 24px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.badge { background: var(--accent-soft); color: var(--accent-strong); border: 1px solid var(--accent);
  padding: 4px 12px; border-radius: 999px; font-size: 12.5px; font-weight: 650; letter-spacing: .01em; }
.badge.dim { background: var(--neutral-bg); color: var(--neutral-text); border-color: var(--border-strong); }
main { max-width: 1080px; margin: 0 auto; padding: 32px 24px 72px; }
h1 { font-size: 28px; line-height: 1.25; margin: 8px 0 10px; letter-spacing: -0.02em; font-weight: 700; }
h2 { font-size: 21px; line-height: 1.3; margin: 32px 0 12px; letter-spacing: -0.01em; font-weight: 680; }
h3 { font-size: 17px; line-height: 1.35; margin: 18px 0 8px; font-weight: 650; }
h4 { font-size: 15px; margin: 14px 0 6px; font-weight: 650; }
h5 { font-size: 14px; margin: 10px 0 4px; font-weight: 650; }
p.lede { color: var(--muted); font-size: 17px; margin: 0 0 8px; }
.small { color: var(--muted); font-size: 13.5px; }
.muted { color: var(--muted); }
a { color: var(--accent-strong); }
button { background: var(--accent); color: #fff; border: 1px solid var(--accent);
  border-radius: var(--radius-sm); padding: 10px 16px; font: 600 15px/1.2 var(--font);
  cursor: pointer; transition: background .15s ease, border-color .15s ease; }
button:hover { background: var(--accent-strong); border-color: var(--accent-strong); }
button:disabled { background: #94a3b8; border-color: #94a3b8; cursor: not-allowed; }
button.ghost { background: transparent; color: var(--accent-strong); border: 1px solid var(--border-strong); }
button.ghost:hover { background: var(--accent-soft); border-color: var(--accent); }
button.danger { background: var(--danger); border-color: var(--danger); }
button.danger:hover { background: #991b1b; border-color: #991b1b; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
code { background: var(--neutral-bg); color: var(--neutral-text); padding: 1px 6px;
  border-radius: 5px; font-size: 13px; overflow-wrap: anywhere; }
.cards { display: grid; gap: 16px; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 20px; box-shadow: var(--shadow); }
.card.option { display: flex; flex-direction: column; gap: 6px; }
.card.option h3 { margin: 0; }
.card.option.selected { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft), var(--shadow); }
.price { font-size: 24px; font-weight: 750; margin: 2px 0; letter-spacing: -0.01em; color: var(--text); }
.terms { color: var(--muted); margin: 0; }
.status-chip { display: inline-flex; align-items: center; padding: 3px 11px; border-radius: 999px;
  font-size: 12.5px; font-weight: 650; letter-spacing: .01em; border: 1px solid transparent;
  text-transform: capitalize; }
.pill-review { background: var(--warn-bg); color: var(--warn); border-color: var(--warn-border); }
.pill-approved { background: var(--accent-soft); color: var(--accent-strong); border-color: var(--accent); }
.pill-blocked { background: var(--danger-bg); color: var(--danger); border-color: var(--danger-border); }
.pill-accepted { background: var(--ok-bg); color: var(--ok); border-color: var(--ok-border); }
.pill-booked { background: var(--accent); color: #fff; border-color: var(--accent); }
.pill-neutral { background: var(--neutral-bg); color: var(--neutral-text); border-color: var(--border-strong); }
.chip { display: inline-block; background: var(--accent-soft); color: var(--accent-strong);
  border: 1px solid var(--accent); border-radius: 999px; padding: 3px 11px; font-size: 12.5px;
  font-weight: 650; margin: 4px 4px 0 0; }
.chip.ok { background: var(--ok-bg); color: var(--ok); border-color: var(--ok-border); }
.blocked { background: var(--danger-bg); color: var(--danger); border: 1px solid var(--danger-border);
  border-left: 4px solid var(--danger); padding: 12px 16px; border-radius: var(--radius-sm);
  margin: 12px 0; font-weight: 600; }
.passed { background: var(--ok-bg); color: var(--ok); border: 1px solid var(--ok-border);
  border-left: 4px solid var(--ok); padding: 12px 16px; border-radius: var(--radius-sm); margin: 12px 0; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; margin: 8px 0; }
td, th { border: 1px solid var(--border); padding: 8px 10px; text-align: left; vertical-align: top; }
th { background: var(--neutral-bg); font-weight: 650; color: var(--neutral-text); }
.ctl { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin: 12px 0; }
textarea { width: 100%; min-height: 76px; margin: 6px 0; border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm); padding: 10px 12px; font: inherit; color: var(--text);
  background: var(--surface); }
textarea.draft { min-height: 96px; }
input, select { border: 1px solid var(--border-strong); border-radius: var(--radius-sm);
  padding: 9px 12px; font: inherit; color: var(--text); background: var(--surface); max-width: 100%; }
input[type=file] { width: 100%; min-width: 0; padding: 7px 10px; }
input:hover, select:hover, textarea:hover { border-color: var(--accent); }
fieldset { border: 1px solid var(--border); border-radius: var(--radius); margin: 20px 0;
  padding: 18px 20px; background: var(--surface); box-shadow: var(--shadow); min-width: 0; }
legend { font-weight: 700; padding: 0 8px; font-size: 15px; color: var(--text); }
ul.phases { margin: 6px 0; padding-left: 20px; }
.msg-line { min-height: 20px; overflow-wrap: anywhere; }
@media (max-width: 640px) {
  main { padding: 20px 16px 56px; }
  header { padding: 12px 16px; }
  h1 { font-size: 24px; }
  h2 { font-size: 19px; }
  .cards { grid-template-columns: 1fr; }
  table { display: block; overflow-x: auto; white-space: nowrap; }
  .ctl { gap: 8px; }
  button { width: auto; }
}
"""

BASE_STYLE = _DESIGN_BASE + """
.deal { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 24px; margin-bottom: 24px; box-shadow: var(--shadow); overflow-wrap: anywhere; }
.deal > h2 { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-top: 0; }
.timeline { padding-left: 20px; }
.timeline li { margin-bottom: 6px; }
form.intake { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px 14px; align-items: end; }
form.intake label { display: flex; flex-direction: column; gap: 4px; font-size: 12.5px;
  color: var(--muted); font-weight: 650; min-width: 0; }
form.intake label.wide { grid-column: 1 / -1; }
.vblock { border-top: 1px solid var(--border); margin-top: 18px; padding-top: 14px; }
.release-panel { position: sticky; top: 0; z-index: 5; background: var(--accent-strong); color: #eafbf7;
  border-radius: var(--radius); padding: 16px 18px; margin: 16px 0; display: none; box-shadow: var(--shadow); }
.release-panel.visible { display: block; }
.release-panel input { flex: 1; min-width: 260px; background: #083f39; color: #d7f5ee;
  border: 1px solid #2e6b62; }
.release-panel .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.release-panel button { background: var(--accent); border-color: var(--accent); }
.release-panel button.ghost { background: transparent; color: #d7f5ee; border-color: #2e6b62; }
.release-panel button.ghost:hover { background: #0b5d56; }
.error-box { background: var(--danger-bg); color: var(--danger); border: 1px solid var(--danger-border);
  border-left: 4px solid var(--danger); border-radius: var(--radius-sm); padding: 12px 16px;
  margin: 12px 0; display: none; font-weight: 600; }
.error-box.visible { display: block; }
.empty-state { text-align: center; padding: 56px 16px; color: var(--muted); }
.empty-state .big { font-size: 38px; }
.quote { border-left: 3px solid var(--accent); margin: 8px 0; padding: 6px 14px; color: var(--muted);
  font-style: italic; background: var(--accent-soft); border-radius: 0 var(--radius-sm) var(--radius-sm) 0; }
"""

FOUNDER_STYLE = BASE_STYLE + """
.stage-timeline { list-style: none; margin: 12px 0 24px; padding: 0; display: grid; gap: 10px; }
.stage { border: 1px solid var(--border); border-left: 4px solid var(--border-strong);
  border-radius: var(--radius-sm); background: var(--surface); padding: 12px 16px; }
.stage-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.stage-label { font-weight: 650; }
.stage-skill { color: var(--muted); }
.stage-trigger { background: var(--neutral-bg); color: var(--neutral-text);
  border: 1px solid var(--border-strong); border-radius: 999px; padding: 1px 9px;
  font-size: 12px; white-space: nowrap; }
.stage-ok { border-left-color: var(--ok); }
.stage-blocked { border-left-color: var(--danger); background: var(--danger-bg);
  border-color: var(--danger-border); }
.stage-blocked .stage-label { color: var(--danger); }
.stage-running, .stage-queued { border-left-color: var(--warn); }
.stage-pending { border-left-color: var(--border-strong); background: var(--neutral-bg); }
.stage-steps { list-style: none; margin: 8px 0 0; padding: 0; display: grid; gap: 6px; }
.stage-steps li { font-size: 13.5px; overflow-wrap: anywhere; }
.stage-steps .status-chip { font-size: 11.5px; padding: 0 8px; }
.ev-blocked { color: var(--danger); }
.ev-blocked b { color: var(--danger); }
.stage-note { overflow-wrap: anywhere; }
.what-changed { border: 1px solid var(--border); border-left: 4px solid var(--accent);
  border-radius: var(--radius-sm); background: var(--accent-soft); padding: 12px 16px; margin: 12px 0; }
.what-changed h4 { margin: 0 0 6px; }
table.diff { width: 100%; border-collapse: collapse; margin: 6px 0; }
table.diff th, table.diff td { text-align: left; padding: 4px 8px;
  border-bottom: 1px solid var(--border); font-size: 13.5px; vertical-align: top; }
table.diff .diff-old { color: var(--danger); }
table.diff .diff-new { color: var(--ok); font-weight: 650; }
.reason-links { margin: 8px 0 0; padding-left: 20px; }
.reason-links li { font-size: 13.5px; margin-bottom: 4px; overflow-wrap: anywhere; }
.needs-info { background: var(--warn-bg); color: var(--warn); border: 1px solid var(--warn-border);
  border-left: 4px solid var(--warn); padding: 12px 16px; border-radius: var(--radius-sm); margin: 12px 0; }
.needs-info code { background: rgba(255,255,255,.65); color: var(--warn); }
.app-header { justify-content: space-between; padding: 14px 26px; }
.brand-lockup { display: flex; align-items: center; gap: 9px; font-weight: 750; letter-spacing: -.02em; }
.brand-mark { width: 23px; height: 23px; background: var(--accent); border-radius: 6px; display: inline-block; position: relative; }
.brand-mark:before, .brand-mark:after { content: ""; position: absolute; bottom: 5px; width: 4px; background: #fff; border-radius: 2px; }
.brand-mark:before { height: 12px; left: 6px; }.brand-mark:after { height: 8px; right: 5px; }
.brand-subtitle { color: var(--muted); font-size: 13px; font-weight: 600; padding-left: 2px; }
.header-meta { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.founder-shell { max-width: 1280px; min-height: calc(100vh - 58px); margin: 0 auto; padding: 0; display: grid; grid-template-columns: 248px minmax(0, 1fr); }
.deal-sidebar { border-right: 1px solid var(--border); padding: 25px 12px 18px; background: var(--surface); display: flex; flex-direction: column; gap: 20px; }
.sidebar-heading { color: var(--muted); font-size: 11px; font-weight: 750; letter-spacing: .08em; text-transform: uppercase; padding: 0 9px; }
.sidebar-nav { display: grid; gap: 3px; margin-top: -12px; }.sidebar-filter { color: var(--text); background: transparent; border: 0; text-align: left; padding: 9px; border-radius: 7px; font-size: 13px; font-weight: 650; display: flex; justify-content: space-between; }
.sidebar-filter:hover, .sidebar-filter.is-active { background: var(--neutral-bg); color: var(--text); border: 0; }.sidebar-filter span { color: var(--muted); font-variant-numeric: tabular-nums; }
.queue-heading { margin-top: -4px; }.deal-queue { display: grid; gap: 4px; margin-top: -12px; }.queue-deal { width: 100%; background: transparent; color: var(--text); border: 0; border-left: 3px solid transparent; border-radius: 0 7px 7px 0; text-align: left; padding: 9px 8px; }
.queue-deal:hover, .queue-deal.is-selected { background: var(--neutral-bg); border-color: var(--accent); color: var(--text); }.queue-line { display: flex; gap: 8px; justify-content: space-between; align-items: start; font-size: 13px; }.queue-meta { display: block; color: var(--muted); font-size: 11.5px; margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.queue-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--warn); margin-top: 5px; flex: 0 0 auto; }.queue-dot.blocked { background: var(--danger); }.queue-dot.approved, .queue-dot.booked { background: var(--ok); }
.sidebar-sync { color: var(--muted); font-size: 11.5px; line-height: 1.5; border-top: 1px solid var(--border); margin: auto 8px 0; padding-top: 14px; }
.workspace-main { min-width: 0; padding: 24px 30px 60px; }.workspace-toolbar { display: flex; justify-content: space-between; gap: 16px; align-items: center; margin: 0 auto 16px; max-width: 900px; }.workspace-toolbar .ctl { margin: 0; }
.operator-tools { max-width: 900px; margin: 32px auto 0; border-top: 1px solid var(--border); padding-top: 12px; }.operator-tools > summary { color: var(--muted); cursor: pointer; font-size: 13px; font-weight: 650; }.operator-tools fieldset { box-shadow: none; }
.workbench { max-width: 900px; margin: 0 auto; }.workbench-header { display: flex; justify-content: space-between; align-items: start; gap: 20px; margin: 12px 0 18px; }.workbench-header h1 { margin: 0; }.workbench-subtitle { color: var(--muted); margin: 3px 0 0; font-size: 14px; }
.next-step { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; margin: 18px 0 12px; box-shadow: var(--shadow); }.next-step-label { color: var(--muted); font-size: 11px; font-weight: 750; letter-spacing: .07em; text-transform: uppercase; }.next-step h2 { font-size: 18px; margin: 4px 0; }.next-step p { color: var(--muted); margin: 0; font-size: 14px; }.next-step .ctl { margin: 16px 0 0; }
.workbench details { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); margin-top: 10px; overflow: hidden; }.workbench details > summary { cursor: pointer; font-size: 14px; font-weight: 700; padding: 13px 16px; }.workbench details[open] > summary { border-bottom: 1px solid var(--border); }.detail-inner { padding: 2px 16px 16px; }.detail-inner > :last-child { margin-bottom: 0; }
.workbench .stage-timeline { margin-bottom: 0; }.workbench .stage { box-shadow: none; }.workbench .vblock { border-top: 0; padding-top: 0; margin-top: 0; }.workbench .vblock > h3 { display: none; }.workbench .draft { min-height: 116px; }.workbench .cards { margin-top: 10px; }
@media (max-width: 780px) { .founder-shell { display: block; }.deal-sidebar { border-right: 0; border-bottom: 1px solid var(--border); padding: 16px; gap: 12px; }.deal-queue { grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }.sidebar-sync { display: none; }.workspace-main { padding: 20px 16px 48px; }.workbench-header, .workspace-toolbar { align-items: start; flex-direction: column; }.app-header { padding: 13px 16px; }.header-meta { justify-content: start; } }
"""

CLIENT_STYLE = BASE_STYLE + """
.client-header { position: sticky; top: 0; z-index: 10; justify-content: space-between; }
.client-header strong { font-size: 18px; letter-spacing: -.02em; }
.proposal-nav { display: flex; gap: 18px; margin-left: auto; }
.proposal-nav a { color: var(--muted); text-decoration: none; font-size: 14px; font-weight: 650; }
.client-main { max-width: 1160px; }
.proposal-hero { display: grid; grid-template-columns: minmax(0,.9fr) minmax(320px,1.1fr);
  gap: 42px; align-items: center; padding: 36px 0 46px; }
.proposal-hero h1 { font-size: clamp(34px,5vw,58px); line-height: 1.05; margin: 8px 0 18px; }
.proposal-hero .lede { font-size: 19px; max-width: 58ch; }
.eyebrow { color: var(--accent-strong); font-size: 12px; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; }
.featured-preview { aspect-ratio: 16/10; overflow: hidden; border-radius: 20px; background: #dce8e5;
  box-shadow: 0 20px 50px rgba(15,23,42,.14); }
.featured-preview video, .featured-preview img { display: block; width: 100%; height: 100%; object-fit: cover; }
.preview-placeholder { width: 100%; height: 100%; display: grid; place-items: center; color: var(--accent-strong);
  font-size: clamp(24px,5vw,52px); font-weight: 750; background: linear-gradient(145deg,#d9efea,#f5eadc); }
.proposal-content > .proposal-story > section { background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 22px; }
#concept, #portfolio, #packages { scroll-margin-top: 86px; }
p.accepted { background: var(--ok-bg); color: var(--ok); border: 1px solid var(--ok-border);
  border-left: 4px solid var(--ok); padding: 12px 16px; border-radius: var(--radius-sm); font-weight: 600; }
form.rc { margin-top: 24px; }
form.rc textarea { min-height: 128px; }
form.rc .ctl { display: flex; gap: 12px; flex-wrap: wrap; align-items: end; margin: 10px 0; }
label.rc-field { display: flex; flex-direction: column; gap: 4px; font-size: 12.5px;
  color: var(--muted); font-weight: 650; }
@media (max-width: 760px) {
  .client-header { align-items: flex-start; }
  .proposal-nav { width: 100%; order: 3; margin: 2px 0 0; justify-content: space-between; gap: 8px; }
  .proposal-hero { grid-template-columns: 1fr; gap: 24px; padding: 26px 0 34px; }
  .proposal-hero h1 { font-size: 36px; }
  .featured-preview { min-height: 220px; }
  form.rc .ctl { display: grid; grid-template-columns: 1fr 1fr; }
  form.rc .ctl button { grid-column: 1/-1; width: 100%; }
}
"""

QUOTE_STYLE = _DESIGN_BASE + """
form.quote-form { display: grid; gap: 18px; }
form.quote-form label { display: grid; gap: 6px; font-size: 14px; font-weight: 600; color: var(--text); }
form.quote-form input, form.quote-form select, form.quote-form textarea { width: 100%; }
form.quote-form input[type=checkbox] { width: auto; }
form.quote-form .check { display: flex; align-items: center; gap: 8px; font-weight: 500; }
form.quote-form textarea { min-height: 7rem; }
form.quote-form .actions { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 8px; }
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


DASH_STYLE = r"""
/* Founder dashboard: values copied from the Alex Studio wireframe (Scenes 3 and 5). */
:root { --b-bg: #f3f5f7; --b-panel: #ffffff; --b-ink: #1d2430; --b-mute: #64707e; --b-line: #dde2e8;
  --b-teal: #146c62; --b-teal-tint: #e2f1ee;
  --ok: #2c7a3f; --ok-tint: #e4f3e7; --warn: #a2620a; --warn-tint: #fbf0dc; --bad: #b3261e; --bad-tint: #fbe4e2;
  --ui: "Instrument Sans", "Helvetica Neue", Arial, sans-serif;
  /* shared design-system tokens re-pointed at the wireframe palette */
  --bg: var(--b-bg); --surface: #fff; --border: var(--b-line); --border-strong: #c9d0d7; --text: var(--b-ink);
  --muted: var(--b-mute); --accent: var(--b-teal); --accent-strong: #0f574f; --accent-soft: var(--b-teal-tint);
  --danger: var(--bad); --danger-bg: var(--bad-tint); --danger-border: #f1c0bb; --ok-bg: var(--ok-tint);
  --ok-border: #bfdcc6; --warn-bg: var(--warn-tint); --warn-border: #ecd5a6; --neutral-bg: #eef1f4;
  --neutral-text: #4b5663; --radius: 10px; --radius-sm: 8px; --shadow: none; --font: var(--ui); }
body { background: var(--b-bg); color: var(--b-ink); font: 15px/1.5 var(--ui); -webkit-font-smoothing: antialiased; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
button { font: 600 13.5px var(--ui); border-radius: 7px; padding: 10px 16px; }
button.ghost { background: #fff; color: var(--b-ink); border: 1px solid var(--b-line); }
button.ghost:hover { background: #f6f2ee; border-color: var(--b-line); color: var(--b-ink); }
.founder-shell.dash { max-width: none; min-height: 100vh; margin: 0; padding: 0; display: grid;
  grid-template-columns: 230px minmax(0, 1fr); background: var(--b-bg); color: var(--b-ink); }
.side { background: var(--b-panel); border-right: 1px solid var(--b-line); padding: 14px; display: grid;
  align-content: start; gap: 10px; }
.side .logo { font: 700 15px var(--ui); display: flex; align-items: center; gap: 8px; }
.side .logo i { width: 18px; height: 18px; border-radius: 5px; background: var(--b-teal); display: inline-block; }
.side h6 { margin: 8px 0 0; font: 600 12px var(--ui); color: var(--b-mute); }
.deal-queue { display: grid; gap: 10px; margin: 0; }
button.rail-deal { border: 1px solid var(--b-line); border-radius: 8px; padding: 9px 10px; display: grid; gap: 3px;
  font: 400 12.5px/1.45 var(--ui); background: #fff; color: var(--b-ink); text-align: left; width: 100%; }
button.rail-deal:hover { background: #fff; border-color: var(--b-teal); color: var(--b-ink); }
button.rail-deal.sel { border-color: var(--b-teal); box-shadow: inset 3px 0 0 var(--b-teal); }
button.rail-deal b { font-size: 13px; }
.dmeta { color: var(--b-mute); }
.st { display: inline-block; font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 999px; justify-self: start; white-space: nowrap; }
.st.rev { background: var(--warn-tint); color: var(--warn); }
.st.ok { background: var(--ok-tint); color: var(--ok); }
.st.bad { background: var(--bad-tint); color: var(--bad); }
.st.new { background: var(--b-teal-tint); color: var(--b-teal); }
.st.mute { background: var(--neutral-bg); color: var(--b-mute); }
.said { margin: 0; border: 1px solid var(--b-line); border-radius: 8px; padding: 10px 12px; font-size: 12.5px;
  line-height: 1.55; background: #fbfcfd; overflow-wrap: anywhere; }
.said small { display: block; margin-top: 6px; font-size: 11.5px; color: var(--b-mute); }
.week { display: grid; gap: 6px; font-size: 12px; color: var(--b-mute); }
.side-foot { margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--b-line); display: grid; gap: 8px;
  font-size: 11.5px; color: var(--b-mute); }
.side-foot summary { cursor: pointer; font-weight: 600; }
.side-foot nav { display: grid; gap: 4px; margin-top: 6px; }
.side-foot a { color: var(--b-teal); text-decoration: none; }
.side-foot a:hover { text-decoration: underline; }
.main { padding: 18px 20px; min-width: 0; }
.workbench { max-width: none; margin: 0; display: grid; gap: 16px; }
.main-head { display: flex; flex-wrap: wrap; gap: 10px; align-items: baseline; justify-content: space-between; }
.main-head h4 { margin: 0; font: 600 19px var(--ui); }
.main-head .st { font-size: 12px; }
.panel { background: var(--b-panel); border: 1px solid var(--b-line); border-radius: 10px; padding: 14px;
  display: grid; gap: 10px; min-width: 0; align-content: start; }
.panel h5 { margin: 0; font: 600 13px var(--ui); color: var(--b-mute); }
.figs { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.fig { display: grid; gap: 2px; }
.fig small { font-size: 11.5px; color: var(--b-mute); }
.fig b { font: 600 18px var(--ui); font-variant-numeric: tabular-nums; }
.fig.priv b::after { content: " private"; font: 600 10px var(--ui); color: var(--b-mute); vertical-align: middle; }
.meterwrap { display: grid; gap: 6px; font-size: 12px; color: var(--b-mute); }
.meter { height: 10px; border-radius: 5px; background: var(--b-line); position: relative; overflow: hidden; }
.meter i { position: absolute; left: 0; top: 0; bottom: 0; background: var(--b-teal); }
.meter.over i { background: var(--bad); }
.warns, .warns.reason-links { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 0; padding: 0; }
.warn { display: grid; grid-template-columns: 22px 1fr; gap: 10px; background: var(--bad-tint); color: var(--bad);
  border-radius: 8px; padding: 10px 12px; font-size: 12.5px; line-height: 1.45; }
.warn b { display: block; font-size: 13.5px; }
.wi { width: 22px; height: 22px; border-radius: 50%; background: var(--bad); color: #fff; font: 700 13px/22px var(--ui); text-align: center; }
.two { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px; align-items: stretch; }
.olist { display: grid; gap: 8px; }
.orow { display: grid; grid-template-columns: 150px 1fr auto; gap: 10px; align-items: center; font-size: 13px;
  border: 1px solid var(--b-line); border-radius: 8px; padding: 8px 10px; background: #fff; }
.orow.one { grid-template-columns: 1fr auto; }
.orow b { font-variant-numeric: tabular-nums; }
.orow .m { color: var(--b-mute); }
.orow.ck { cursor: pointer; }
.orow.ck input { accent-color: var(--b-teal); margin: 0 4px 0 0; vertical-align: -2px; }
.orow.ck:has(input:not(:checked)) { opacity: .5; }
.orow.picked { border-color: var(--b-teal); }
.hint { font-size: 12px; color: var(--b-mute); margin: 0; }
textarea.draft, div.draft, .workbench .draft { display: block; width: 100%; margin: 0; min-height: 0; border: 1px solid var(--b-line);
  border-radius: 8px; padding: 10px 12px; font: 13px/1.55 var(--ui); color: var(--b-ink); background: #fbfcfd;
  resize: none; field-sizing: content; white-space: pre-wrap; }
textarea.draft[readonly] { cursor: default; }
textarea.draft:not([readonly]) { outline: 2px solid var(--b-teal); background: #fff; }
.check { display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px; border-radius: 8px; font-size: 13px; }
.check b { display: block; }
.check.ok { background: var(--ok-tint); color: var(--ok); }
.check.bad { background: var(--bad-tint); color: var(--bad); }
.check.wait { background: #eef1f4; color: var(--b-mute); }
.checks-stack { display: grid; gap: 10px; }
.row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.panel .row { margin-top: auto; }
.bbtn { display: inline-flex; align-items: center; justify-content: center; border-radius: 7px; padding: 10px 16px;
  font: 600 13.5px var(--ui); border: 1px solid var(--b-line); background: #fff; color: var(--b-ink); white-space: nowrap; cursor: pointer; }
.bbtn:hover { background: #f6f2ee; border-color: var(--b-line); color: var(--b-ink); }
.bbtn.p, .bbtn.p:hover { background: var(--b-teal); border-color: var(--b-teal); color: #fff; }
.bbtn:disabled, .bbtn:disabled:hover { opacity: .45; cursor: not-allowed; }
.bbtn.p:disabled, .bbtn.p:disabled:hover { background: var(--b-teal); border-color: var(--b-teal); }
.bbtn.danger { color: var(--bad); }
.bbtn.sm { padding: 6px 10px; }
.helper { font-size: 12px; color: var(--b-mute); margin: 0; }
.msg-line:empty { display: none; }
.needs-info { margin: 0; }
.workbench details { background: #fff; border: 1px solid var(--b-line); border-radius: 10px; margin: 0; }
.workbench details > summary { font: 600 13px var(--ui); color: var(--b-mute); padding: 11px 14px; }
.workbench details[open] > summary { border-bottom: 1px solid var(--b-line); }
.extras { display: grid; gap: 8px; margin-top: 8px; }
.operator-tools { max-width: none; margin: 28px 0 0; }
.release-panel { margin: 0 0 16px; border-radius: 10px; background: #0f574f; }
th { background: #eef1f4; }
.quote { border-left-color: var(--b-teal); background: var(--b-teal-tint); font-style: normal; color: var(--b-ink); }
@media (max-width: 900px) {
  .founder-shell.dash { grid-template-columns: 1fr; }
  .side { border-right: 0; border-bottom: 1px solid var(--b-line); }
  .figs { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .two, .warns, .warns.reason-links { grid-template-columns: 1fr; }
  .orow { grid-template-columns: 1fr auto; }
  .orow .m { grid-column: 1 / -1; order: 3; }
}
"""

DASH_JS = r"""
let latestCapacity = null;
const SOURCE_LABELS = {hashmeta: 'Hashmeta form', site: 'Hashmeta chat', manual: 'Added by you'};
const FORMAT_LABELS = {product_promo: 'promo video', social_reel: 'social reel',
  testimonial: 'testimonial', event_recap: 'event recap', explainer: 'explainer'};
const FIELD_LABELS = {deliverables: 'Videos', caption_versions: 'Captioned versions',
  deadline: 'Deadline', budget_sgd: 'Budget', shoot_days: 'Shoot days', locations: 'Locations',
  interviewees: 'Interviewees', scripting: 'Scripting', editing_level: 'Editing',
  graphics: 'Graphics', revision_rounds: 'Rounds of changes', format: 'Format',
  production_mode: 'Production', direct_costs_sgd: 'Direct costs', client: 'Client'};
function money0(v) { return v == null ? '—' : 'S$' + Math.round(Number(v)).toLocaleString('en-SG'); }
function hrs(v) { return v == null ? '—' : hours(v) + ' h'; }
function shortDate(iso) {
  if (!iso) return '';
  const d = new Date(String(iso) + 'T00:00:00+08:00');
  if (isNaN(d)) return String(iso);
  return d.toLocaleDateString('en-GB', {day: 'numeric', month: 'short', timeZone: 'Asia/Singapore'});
}
function clock(ts) {
  if (!ts) return '';
  return new Date(ts * 1000).toLocaleTimeString('en-GB',
    {hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Singapore'});
}
function dealOffer(d) { const o = d.offers[d.offers.length - 1]; return (o && o.offer) || {}; }
function lastActivity(d) {
  let t = 0;
  d.steps.forEach(s => { t = Math.max(t, s.finished_at || s.started_at || 0); });
  d.messages.forEach(m => { t = Math.max(t, m.created_at || 0); });
  return t;
}
function dealStage(d) {
  const v = latestVersion(d);
  if (d.status === 'needs_information') return ['Needs info', 'rev'];
  if (d.status === 'closed') return ['Closed', 'mute'];
  if (!v) return ['New inquiry', 'new'];
  if (v.status === 'confirmed' || v.confirmed) return ['Confirmed', 'ok'];
  if (v.status === 'published') return v.accepted_at ? ['Client picked an option', 'rev'] : ['Waiting on client', 'new'];
  if (v.status === 'blocked') return ['Draft blocked', 'bad'];
  if (v.status === 'rejected') return ['Rejected', 'mute'];
  if (v.status === 'superseded') return ['Replaced', 'mute'];
  if (v.verdict === 'counter') return ['Counter ready', 'rev'];
  if (v.verdict === 'clarify') return ['Needs your estimate', 'rev'];
  return ['Ready for review', 'rev'];
}
function plainReasons(v, offer) {
  const est = v.estimate || {};
  const due = shortDate(offer.deadline) || 'the deadline';
  const budget = offer.budget_sgd;
  const out = [];
  (v.reason_codes || []).forEach(c => {
    if (c === 'CAPACITY_SHORTFALL') out.push(['Not enough time before ' + due, est.planning_hours == null ? 'There are not enough free hours before the deadline.'
      : `This needs about ${Math.round(est.planning_hours)} hours. You have ${Math.round(est.capacity_hours)} hours free before then.`]);
    else if (c === 'BELOW_FLOOR') out.push(['Price is under your minimum', (budget != null && est.floor_sgd != null)
      ? `${money0(budget)} is ${money0(est.floor_sgd - budget)} less than the lowest you’d accept.`
      : 'The client’s budget is under the lowest you’d accept.']);
    else if (c === 'BELOW_LIST_NEEDS_APPROVAL') out.push(['Under your list price', (budget != null && est.list_sgd != null)
      ? `${money0(budget)} is ${money0(est.list_sgd - budget)} under your list price but above your minimum. It needs your OK.`
      : 'The budget is under your list price but above your minimum. It needs your OK.']);
    else if (c === 'OVER_DISCOUNT_LIMIT') out.push(['Discount is bigger than you allow',
      `${money0(budget)} is above your minimum, but further under your list price of ${money0(est.list_sgd)} than your discount limit allows.`]);
    else if (c === 'MISSING_FIELD') out.push(['Some project details are missing', 'Nothing was guessed. Ask the client for the missing details.']);
    else if (c === 'INVALID_FIELD') out.push(['Some project details don’t add up', 'A date, number or choice in the brief needs checking.']);
    else if (c === 'MANUAL_ESTIMATE_REQUIRED') out.push(['This needs your own estimate', 'No pricing rule covers this kind of job yet, so no price was worked out.']);
    else if (c === 'COMPARABLE_MISMATCH') out.push(['A past job used for comparison doesn’t match', 'Check the similar past jobs before relying on this estimate.']);
    else if (c === 'NO_FEASIBLE_OPTIONS') out.push(['No option fits', 'The engine could not find a safe counter offer.']);
    else out.push(['Check this estimate', 'The engine flagged something it could not explain in plain words.']);
  });
  if (v.capacity_stale) out.push(['Your calendar changed since this estimate',
    'Another booking was confirmed. Re-run the estimate before sending; some options may no longer fit.']);
  (v.impacts || []).forEach(i => out.push(['A booking changed what you can offer',
    'No longer fits: ' + (i.lost || []).map(o => o.deliverables + ' videos by ' + shortDate(o.deadline)).join(', ') + '.']));
  return out;
}
// Plain-language engine reasons (no internal codes are shown).
function reasonLinkHtml(items) {
  if (!items.length) return '';
  return '<div class="warns reason-links">' + items.map(([t, x]) =>
    `<div class="warn"><span class="wi" aria-hidden="true">!</span><span><b>${esc(t)}</b>${esc(x)}</span></div>`).join('') + '</div>';
}
function whatChangedHtml(v) {
  const c = v.comparison;
  if (!c || !c.is_revision) return '';
  const fmt = (f, val) => {
    if (val === null || val === undefined || val === '') return '<span class="muted">not recorded</span>';
    if (/_sgd$/.test(f)) return esc(money0(val));
    if (f === 'deadline') return esc(shortDate(val));
    return esc(String(val).replace(/_/g, ' '));
  };
  const fields = c.changed_fields || [];
  const rows = fields.map(f => `<tr><th scope="row">${esc(FIELD_LABELS[f.field] || f.field.replace(/_/g, ' '))}</th>
     <td class="diff-old">${fmt(f.field, f.old)}</td><td class="diff-new">${fmt(f.field, f.new)}</td></tr>`).join('');
  return `<section class="panel what-changed"><h5>What changed since version ${esc(String(c.prior_round))}</h5>
    ${fields.length ? `<table class="diff"><thead><tr><th>Detail</th><th>Before</th><th>Now</th></tr></thead><tbody>${rows}</tbody></table>`
      : '<p class="muted small">Nothing in the brief changed. This is a fresh estimate against your current calendar.</p>'}</section>`;
}
function draftLeak(v) {
  const est = v.estimate || {};
  const text = String(v.draft || '');
  const forms = n => (n == null || isNaN(Number(n))) ? [] : [
    Number(n).toLocaleString('en-SG', {minimumFractionDigits: 2, maximumFractionDigits: 2}),
    Number(n).toFixed(2), String(Number(n))];
  const floorHit = forms(est.floor_sgd).find(f => text.includes(f));
  const shown = f => text.includes('S$' + f) ? 'S$' + f : f;
  if (floorHit) return ['Blocked: this draft shows your minimum price', `Take out "${shown(floorHit)}" before it can go to the client.`];
  const hourHit = forms(est.planning_hours).concat(forms(est.capacity_hours)).find(f => f.includes('.') && text.includes(f));
  if (hourHit) return ['Blocked: this draft shows your hours', `Take out "${hourHit}" before it can go to the client.`];
  return ['Blocked: this draft has something private in it', 'Take out internal numbers or other client names, then run the check again.'];
}
function checkRow(kind, title, text) {
  const icon = kind === 'ok' ? '✓' : (kind === 'bad' ? '✕' : '○');
  return `<div class="check ${kind}"><span aria-hidden="true">${icon}</span><span><b>${esc(title)}</b>${esc(text)}</span></div>`;
}
function checkRows(v) {
  if (v.outbound_status === 'passed') return checkRow('ok', 'Safe to send', 'No hours, minimum price or other client names in the message.')
    + checkRow('ok', 'Price matches the estimate', 'Price and dates come from this estimate.');
  if (v.outbound_status === 'blocked') { const [t, x] = draftLeak(v);
    return checkRow('bad', t, x) + checkRow('wait', 'Price matches the estimate', 'Checked again once the draft is fixed.'); }
  return checkRow('wait', 'Safe to send', 'Not checked yet. Run the check before this can go out.')
    + checkRow('wait', 'Price matches the estimate', 'Not checked yet.');
}

const NUM_WORDS = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'];
const offeredSel = {};
const cleanDrafts = {};
const similarPick = {};
function oneDecimal(v) { return String(Number(Number(v).toFixed(1))); }
function verdictChip(d, v) {
  if (v && v.verdict === 'accept') return '<span class="st ok">Fits as asked</span>';
  if (v && v.verdict === 'counter') return '<span class="st bad">Doesn’t fit as asked</span>';
  if (v && v.verdict) return '<span class="st rev">Needs your estimate</span>';
  const [label, tone] = dealStage(d);
  return '<span class="st ' + tone + '">' + esc(label) + '</span>';
}
function railMeta(d) {
  const v = latestVersion(d);
  const when = clock(lastActivity(d));
  if (v && (v.status === 'confirmed' || v.confirmed)) return 'Booked' + (when ? ', ' + when : '');
  if (v && v.status === 'published') return 'Proposal sent';
  const changed = v && v.version_no > 1 && d.messages.some(m => m.direction === 'inbound');
  return (changed ? 'Change requested' : (SOURCE_LABELS[d.source] || d.source)) + (when ? ', ' + when : '');
}
function railDeal(d) {
  const [label, tone] = dealStage(d);
  const sel = d.deal_id === selectedDealId ? ' sel' : '';
  return `<button type="button" class="rail-deal${sel}" onclick="selectDeal('${d.deal_id}')"><b>${esc(d.client)}</b><span class="dmeta">${esc(railMeta(d))}</span><span class="st ${tone}">${esc(label)}</span></button>`;
}
function weekHtml() {
  const weeks = (latestCapacity && latestCapacity.weeks) || [];
  if (!weeks.length) return '<span>No capacity calendar yet.</span>';
  const today = new Date().toLocaleDateString('en-CA', {timeZone: 'Asia/Singapore'});
  const w = weeks.find(x => x.week >= today) || weeks[0];
  const pct = w.budget_hours > 0 ? Math.max(0, Math.min(100, w.remaining_hours / w.budget_hours * 100)) : 0;
  return `<span>${oneDecimal(w.remaining_hours)} h free of ${oneDecimal(w.budget_hours)} h</span><div class="meter"><i style="width:${pct.toFixed(0)}%"></i></div>`;
}
function clientSaidHtml(d) {
  const inbound = d ? d.messages.filter(m => m.direction === 'inbound') : [];
  if (!inbound.length) return '';
  const m = inbound[inbound.length - 1];
  return `<h6>Client said</h6><div class="said">"${esc(m.body)}"<small>Client text. Treated as a request, not an approval.</small></div>`;
}
function renderWorkspace(deals) {
  const queue = document.getElementById('deal-queue');
  const progress = document.getElementById('deal-progress');
  const root = document.getElementById('root');
  const attention = deals.filter(needsAttention);
  const others = deals.filter(d => !needsAttention(d));
  document.getElementById('week-capacity').innerHTML = weekHtml();
  if (!deals.length) {
    queue.innerHTML = '<p class="hint">Nothing needs you right now.</p>';
    progress.innerHTML = '<p class="hint">Nothing in progress.</p>';
    document.getElementById('client-said').innerHTML = '';
    root.className = 'empty-state';
    root.innerHTML = '<span class="big">·</span><h2>No inquiries yet</h2><p class="muted">New Hashmeta inquiries will appear here after QwenWork claims them.</p>';
    return;
  }
  const byRecent = (a, b) => lastActivity(b) - lastActivity(a);
  attention.sort(byRecent); others.sort(byRecent);
  if (!selectedDealId || !deals.some(d => d.deal_id === selectedDealId))
    selectedDealId = (attention[0] || others[0]).deal_id;
  queue.innerHTML = attention.map(railDeal).join('') || '<p class="hint">Nothing needs you right now.</p>';
  progress.innerHTML = others.map(railDeal).join('') || '<p class="hint">Nothing in progress.</p>';
  const d = deals.find(x => x.deal_id === selectedDealId);
  document.getElementById('client-said').innerHTML = clientSaidHtml(d);
  root.className = '';
  root.innerHTML = dealHtml(d);
}
function estimatePanel(d, v) {
  const offer = (v.comparison && v.comparison.offer && Object.keys(v.comparison.offer).length) ? v.comparison.offer : dealOffer(d);
  const reasons = plainReasons(v, offer);
  const est = v.estimate;
  if (!est) return reasons.length ? `<div class="panel"><h5>Why this needs you</h5>${reasonLinkHtml(reasons)}</div>` : '';
  const due = shortDate(offer.deadline);
  const over = est.planning_hours > est.capacity_hours;
  const pct = est.capacity_hours > 0 ? Math.min(100, est.planning_hours / est.capacity_hours * 100) : 100;
  const used = (v.comparables && v.comparables.used) || [];
  const basis = used.length ? `Based on ${used.length} similar past job${used.length === 1 ? '' : 's'}` : 'No similar past jobs on file';
  const counter = v.verdict === 'counter';
  const figs = `<div class="figs">
      <div class="fig"><small>Hours needed</small><b>${hrs(est.planning_hours)}</b></div>
      <div class="fig"><small>Free hours before ${esc(due || 'the deadline')}</small><b>${hrs(est.capacity_hours)}</b></div>
      <div class="fig"><small>List price</small><b>${money(est.list_sgd)}</b></div>
      <div class="fig priv"><small>Lowest you'd accept</small><b>${money(est.floor_sgd)}</b></div></div>`;
  if (counter) return `<div class="panel"><h5>Why this needs a counter offer</h5>${figs}
    <div class="meter${over ? ' over' : ''}"><i style="width:${pct.toFixed(0)}%"></i></div>${reasonLinkHtml(reasons)}</div>`;
  return `<div class="panel"><h5>Engine estimate, private to you</h5>${figs}
    <div class="meterwrap"><span>Hours needed vs free hours</span><div class="meter${over ? ' over' : ''}"><i style="width:${pct.toFixed(0)}%"></i></div>
    <span>Works out to ${money(est.effective_rate_sgd)} an hour. ${basis}, plus ${esc(est.buffer_pct)}% extra time for surprises.</span></div>
    ${reasonLinkHtml(reasons)}</div>`;
}
function optionName(key, o, fallback) {
  if (key === 'as_offered') return 'As offered';
  if (key === 'hold_date') return 'Keep the date';
  if (key === 'hold_budget') return 'Keep the budget';
  if (key === 'hold_scope') return 'Keep all ' + (NUM_WORDS[o.deliverables] || o.deliverables);
  if (key === 'phased') return 'Phased';
  return fallback || String(key).replace(/_/g, ' ');
}
function optionDesc(o) {
  if ((o.phases || []).length > 1) return o.phases.map((p, i) => p.deliverables + (i ? ' more' : '') + ' by ' + shortDate(p.deadline)).join(', ');
  return o.deliverables + ' video' + (o.deliverables === 1 ? '' : 's') + ' by ' + shortDate(o.deadline);
}
function optionIds(v) { return (v.option_meta || []).map(m => m.option_row_id); }
function offeredSet(v) {
  if (!offeredSel[v.version_id]) offeredSel[v.version_id] = new Set(v.offered_option_ids || optionIds(v));
  return offeredSel[v.version_id];
}
function toggleOffer(versionId, rowId, box) {
  const set = offeredSel[versionId];
  if (!box.checked && set.size <= 1) { box.checked = true; msg(versionId, 'Keep at least one option ticked.'); return; }
  if (box.checked) set.add(rowId); else set.delete(rowId);
  box.closest('.orow').classList.toggle('off', !box.checked);
}
function approvePayload(id) {
  const v = latestDeals.flatMap(d => d.versions).find(x => x.version_id === id);
  const set = offeredSel[id];
  if (!v || !set || set.size === optionIds(v).length) return {version_id: id};
  return {version_id: id, option_ids: optionIds(v).filter(x => set.has(x))};
}
function cleanDraftFor(d, v) {
  const sig = JSON.stringify(v.options);
  const prior = d.versions.filter(x => x.version_no < v.version_no && x.outbound_status === 'passed' && JSON.stringify(x.options) === sig);
  return prior.length ? prior[prior.length - 1].draft : null;
}
function useClean(id, btn) {
  const ta = document.getElementById('draft-' + id);
  ta.value = cleanDrafts[id]; ta.readOnly = false; ta.focus();
  const edit = document.getElementById('edit-' + id);
  if (edit) edit.textContent = 'Save draft';
  msg(id, 'Clean draft loaded. Save it, then run the check.');
}
function toggleEdit(id, btn) {
  const ta = document.getElementById('draft-' + id);
  if (ta.readOnly) { ta.readOnly = false; ta.focus(); btn.textContent = 'Save draft'; return; }
  doEdit(id);
}
function similarRow(d) {
  const items = d.similar_work || [];
  if (!items.length) return '';
  const i = (similarPick[d.deal_id] || 0) % items.length;
  const it = items[i];
  return `<h5>Similar work to show</h5><div class="orow one"><span><b>${esc(it.title)}</b><br><span class="m">${esc(it.reason)}</span></span>${items.length > 1 ? `<button type="button" class="bbtn sm" onclick="swapSimilar('${d.deal_id}')">Change</button>` : ''}</div>`;
}
function swapSimilar(dealId) { similarPick[dealId] = (similarPick[dealId] || 0) + 1; renderWorkspace(latestDeals); }
function extraActions(d, v) {
  const out = [];
  if (!v) {
    const o = d.offers[d.offers.length - 1];
    if (o && d.status !== 'needs_information') out.push(`<button class="bbtn p" onclick="doEvaluate('${d.deal_id}','${o.offer_id}')">Run the estimate</button>`);
    return out.join('');
  }
  if (v.status === 'published' && v.accepted_at && !v.confirmed) out.push(`<button class="bbtn p" id="confirm-${v.version_id}" onclick="doConfirm('${v.version_id}')">Confirm booking</button>`);
  if (v.status === 'published' || v.status === 'confirmed') {
    out.push(`<button class="bbtn" onclick="openPreview('${v.version_id}')">Preview sent proposal</button>`);
    out.push(`<button class="bbtn" onclick="doRotate('${v.version_id}')">Reissue client link</button>`);
  }
  if (d.status === 'confirmed') out.push(`<button class="bbtn" onclick="doClose('${d.deal_id}')">Close deal</button>`);
  return out.join('');
}
function dealHtml(d) {
  const v = latestVersion(d);
  const latestOffer = d.offers[d.offers.length - 1];
  const offer = latestOffer && latestOffer.offer;
  const o = offer || {};
  const missing = (d.needs_information && d.needs_information.missing_fields) || [];
  const firstOffer = d.offers[0]?.offer_id;
  const roundForStep = s => s.offer_id || (['intake', 'resume'].includes(s.step_key) ? firstOffer : null);
  const rounds = d.offers.map(r => `<section aria-label="Run round ${r.round}">
    <h4>Round ${r.round}</h4>
    ${timelineHtml(d.steps.filter(s => roundForStep(s) === r.offer_id))}
    ${d.agent_outputs.filter(a => a.offer_id === r.offer_id).map(agentOutputHtml).join('')}
    </section>`).join('');
  const dealActivity = timelineHtml(d.steps.filter(s => !roundForStep(s)))
    + d.agent_outputs.filter(a => !a.offer_id).map(agentOutputHtml).join('');
  const kind = FORMAT_LABELS[o.format] || (o.format ? String(o.format).replace(/_/g, ' ') : 'video');
  const what = o.deliverables ? `${o.deliverables} ${kind}${o.deliverables === 1 ? '' : 's'}` : '';
  const title = esc(d.client) + (v && v.version_no > 1 ? ', version ' + v.version_no : (what ? ', ' + esc(what) : ''));
  const needsInfoHtml = d.status === 'needs_information' ? `<div class="panel"><h5>Needs info, no estimate made</h5><div class="warn" style="grid-template-columns:22px 1fr"><span class="wi">!</span><span><b>Some project details are missing</b>Missing: ${missing.map(f => esc(FIELD_LABELS[f] || String(f).replace(/_/g, ' '))).join(', ') || 'details not recorded'}. Nothing was guessed. QwenWork picks this deal back up when the client replies.</span></div></div>` : '';
  const extras = extraActions(d, v);
  const extraPanel = (!v && extras) ? `<div class="panel"><h5>Next step</h5><p class="hint">The brief is saved. Work out hours, free time and price against your calendar.</p><div class="row">${extras}</div><div id="msg-${latestOffer ? latestOffer.offer_id : d.deal_id}" class="small msg-line" role="status"></div></div>` : '';
  const whatIf = v && ['draft','checked','published'].includes(v.status) && (v.options || []).length
    ? `<details><summary>If they book: effect on your calendar</summary><div class="detail-inner"><div class="extras">${(v.options || []).map(op => {
        const m = (v.option_meta || []).find(x => x.option_id === op.option_id) || {};
        return `<div class="row"><b>${esc(optionName(op.option_id, op, m.label))}</b><button type="button" class="bbtn sm" onclick="previewCapacity('${v.version_id}','${m.option_row_id || ''}',this)">What if they book?</button><p role="status" class="small"></p></div>`;
      }).join('')}</div></div></details>` : '';
  return `<section class="workbench" id="deal-${d.deal_id.slice(0,8)}">
    <div class="main-head"><h4>${title}</h4>${verdictChip(d, v)}</div>
    ${needsInfoHtml}${extraPanel}
    ${v ? estimatePanel(d, v) + versionHtml(d, v, extras) : (extraPanel ? '' : '<p class="hint">No proposal yet.</p>')}
    ${v ? whatChangedHtml(v) : ''}
    ${whatIf}
    <details><summary>Brief and where it came from</summary><div class="detail-inner"><p class="quote">“${esc(d.raw_inquiry || '')}”</p>
      ${offer ? `<table><tr><th>Format</th><td>${esc(kind)}</td><th>Delivery</th><td>${esc(offer.deliverables)} videos by ${esc(shortDate(offer.deadline))}</td></tr><tr><th>Budget</th><td>${money(offer.budget_sgd)}</td><th>Production</th><td>${esc(String(offer.production_mode || '').replace(/_/g, ' '))}</td></tr></table>` : '<p class="small muted">Structured intake is not available yet.</p>'}
      <p class="small">${esc((SOURCE_LABELS[d.source] || d.source) + ' · ' + d.source_id)}</p>
      ${d.agent_outputs.filter(a => a.kind === 'intake' || a.kind === 'clarification').map(agentOutputHtml).join('')}</div></details>
    <details><summary>Deal timeline and run audit</summary><div class="detail-inner"><p class="small">Private run history from intake through booking. Blocked stages remain visible for review.</p>${stageTimelineHtml(d)}
      ${dealActivity ? '<h4>Deal activity</h4>' + dealActivity : ''}${rounds}
      <h4>Audit trail</h4><table><tr><th>Action</th><th>Note</th><th>Digest</th></tr>${d.approvals.map(a => `<tr><td>${esc(a.action)}</td><td>${esc(a.note)}</td><td><span class="small">${esc(String(a.reviewed_digest||'').slice(0,12))}…</span></td></tr>`).join('')}</table></div></details>
    <details><summary>Client messages and demo change request</summary><div class="detail-inner"><ul>${d.messages.map(m => `<li><b>${esc(m.direction)}</b> ${esc(m.body)} ${m.request_id ? '<code>'+esc(m.request_id)+'</code>' : ''}</li>`).join('') || '<li class="muted">No client messages yet.</li>'}</ul>
      <form class="intake" onsubmit="return submitChange(event, '${d.deal_id}')"><label>Videos<input id="chg-count-${d.deal_id.slice(0,8)}" value="5" type="number" min="1"></label><label>Caption versions<input id="chg-caps-${d.deal_id.slice(0,8)}" value="5" type="number" min="0"></label><label>Deadline<input id="chg-deadline-${d.deal_id.slice(0,8)}" value="2026-12-04"></label><label>Budget S$<input id="chg-budget-${d.deal_id.slice(0,8)}" value="2700" type="number" step="0.01"></label><label class="wide"><button type="submit">Queue change request</button></label></form>
      <p class="msg-line small" id="msg-${d.deal_id.slice(0,8)}" role="status" aria-live="polite"></p></div></details>
   </section>`;
}
function versionHtml(d, v, extras) {
  const meta = {};
  (v.option_meta || []).forEach(m => { meta[m.option_id] = m; });
  const opts = v.options || [];
  const offer = dealOffer(d);
  const canEdit = ['draft','blocked','checked','evaluated'].includes(v.status) && !v.confirmed;
  const needsCheck = ['draft','evaluated'].includes(v.status) && v.outbound_status !== 'blocked' && !v.confirmed;
  const canRelease = v.status === 'checked' && !v.confirmed;
  const approvable = canEdit;
  const multi = opts.length > 1;
  const set = offeredSet(v);
  const accepted = v.accepted_option ? v.accepted_option.option_id : null;
  const rows = opts.map(o => {
    const m = meta[o.option_id] || {};
    const rowId = m.option_row_id || '';
    const name = optionName(o.option_id, o, m.label);
    const price = `<b>${money0(o.price_sgd)}</b>`;
    const picked = accepted === rowId ? ' picked' : '';
    if (!multi) {
      const shoot = offer.shoot_days ? `, ${offer.shoot_days} shoot day${offer.shoot_days === 1 ? '' : 's'}` : '';
      return `<div class="orow one${picked}"><span><b>${esc(name)}</b>${accepted === rowId ? ' <span class="st ok">Client picked this</span>' : ''}<br><span class="m">${o.deliverables} videos, ${o.caption_versions} captioned${shoot}, by ${esc(shortDate(o.deadline))}</span></span>${price}</div>`;
    }
    const on = set.has(rowId);
    return `<label class="orow ck${picked}${on ? '' : ' off'}"><span><input type="checkbox" ${on ? 'checked' : ''} ${approvable ? '' : 'disabled'} onchange="toggleOffer('${v.version_id}','${rowId}',this)"> <b>${esc(name)}</b>${accepted === rowId ? ' <span class="st ok">Picked</span>' : ''}</span><span class="m">${esc(optionDesc(o))}</span>${price}</label>`;
  }).join('');
  const draft = canEdit
    ? `<label class="sr" for="draft-${v.version_id}">Client-facing draft</label><textarea class="draft" id="draft-${v.version_id}" readonly>${esc(v.draft)}</textarea>`
    : `<div class="draft">${esc(v.draft)}</div>`;
  const clean = (v.status === 'blocked' || v.outbound_status === 'blocked') && canEdit ? cleanDraftFor(d, v) : null;
  if (clean) cleanDrafts[v.version_id] = clean;
  const est = v.estimate || {};
  const buttons = `${needsCheck ? `<button class="bbtn" onclick="doCheck('${v.version_id}')">Run check</button>` : ''}
    ${canEdit ? `<button class="bbtn p" ${canRelease ? '' : 'disabled title="Unlocks after the check passes"'} onclick="doApprove('${v.version_id}')">Approve and send</button>` : ''}
    ${clean ? `<button class="bbtn" onclick="useClean('${v.version_id}', this)">Use the clean draft</button>` : ''}
    ${canEdit ? `<button class="bbtn" id="edit-${v.version_id}" onclick="toggleEdit('${v.version_id}', this)">Edit draft</button>` : ''}
    ${canEdit ? `<button class="bbtn" onclick="doReject('${v.version_id}')">Reject</button>` : ''}${extras || ''}`;
  const helper = canEdit ? 'Creates the client’s private proposal page and gives you its link to send. Nothing goes out before you approve.'
    : (v.status === 'published' ? 'Sent. The client has a private link to this proposal.' : (v.status === 'confirmed' ? 'Booked. The hours are set aside.' : ''));
  const msgLine = `<div id="msg-${v.version_id}" role="status" aria-live="polite" class="small msg-line"></div>`;
  if (!multi) return `<div class="two">
    <div class="panel"><h5>Option the client will see</h5>${rows || '<p class="hint">No option yet.</p>'}
      <h5>Draft message</h5>${draft}
      ${similarRow(d)}
      ${canEdit && est.floor_sgd != null ? `<span class="hint">Try it: click Edit draft, add "our minimum is ${esc(money(est.floor_sgd))}", then Run check.</span>` : ''}</div>
    <div class="panel"><h5>Before it goes out</h5><div class="checks-stack" aria-live="polite">${checkRows(v)}</div>
      <div class="row">${buttons}</div>${helper ? `<span class="helper">${esc(helper)}</span>` : ''}${msgLine}</div>
  </div>`;
  const one = v.outbound_status === 'blocked' ? (() => { const [t, x] = draftLeak(v); return checkRow('bad', t, x); })()
    : (v.outbound_status === 'passed' ? checkRow('ok', 'Safe to send', 'No hours, minimum price or other client names in the message.')
      : checkRow('wait', 'Not checked yet', 'Run the check. Approve and send unlocks when it passes.'));
  return `<div class="two">
    <div class="panel"><h5>Options for the client</h5><div class="olist">${rows}
      <span class="hint">${approvable ? 'Untick any option you don\'t want to offer.' : (v.offered_option_ids ? 'Only the ticked options were offered.' : 'Every option was offered.')}</span></div></div>
    <div class="panel"><h5>Draft message</h5>${draft}<div aria-live="polite">${one}</div>
      <div class="row">${buttons}</div>${msgLine}</div>
  </div>`;
}
"""


def founder_page(conn):
    fixture_buttons = "".join(
        f'<button class="ghost" onclick="importFixture({i})">{_esc(label)}</button>'
        for i, (_sid, label, _offer, _raw) in enumerate(FIXTURES))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BidPilot — founder dashboard (private)</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&display=swap">
<style>{FOUNDER_STYLE}{DASH_STYLE}</style></head><body>
<main class="founder-shell dash">
<aside class="deal-sidebar side" aria-label="Deal navigation">
  <span class="logo"><i aria-hidden="true"></i>BidPilot</span>
  <h6>Needs you</h6>
  <div id="deal-queue" class="deal-queue" aria-live="polite"><p class="hint">Loading deals…</p></div>
  <div id="client-said"></div>
  <h6>In progress</h6>
  <div id="deal-progress" class="deal-queue"></div>
  <h6>This week</h6>
  <div id="week-capacity" class="week"><span>Loading…</span></div>
  <div class="side-foot">
    <details><summary>Studio tools</summary><nav>
      <a href="/inquire" target="_blank" rel="noopener">Buyer inquiry</a>
      <a href="/portfolio" data-owner-link>Portfolio library</a>
      <a href="/estimating" data-owner-link>Production estimates</a>
      <a href="/commerce" data-owner-link>Proposals and amendments</a>
    </nav></details>
    <span>{SAMPLE_BADGE} · Private founder view</span>
  </div>
</aside>
<section class="workspace-main main">
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

<div id="root" class="empty-state"><span class="big">…</span><p>Loading deals…</p></div>

<details class="operator-tools">
<summary>Demo &amp; system tools</summary>

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

<h2>Inbound inbox</h2>
<div id="inbox" class="small">Loading inquiries…</div>

<h2>Aggregate planning capacity (private)</h2>
<div id="capacity" class="small">Loading capacity…</div>
</details>
</section></main>
<script>
const OWNER = new URLSearchParams(location.search).get('owner') || '';
let currentRelease = {{version_id: null, url: null}};
let liveUpdates = true;
let pollTimer;
let stateRequest = 0;
let renderedDeals;
document.querySelectorAll('[data-owner-link]').forEach(link => link.search = '?owner=' + encodeURIComponent(OWNER));
let latestDeals = [];
let selectedDealId = null;
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
  latestDeals = st.deals;
  latestCapacity = st.capacity;
  const dealSnapshot = JSON.stringify(st.deals);
  if (dealSnapshot === renderedDeals) return;
  if (automatic && root.contains(document.activeElement)
      && document.activeElement.matches('input, textarea, select')) return;
  if (automatic && Array.from(root.querySelectorAll('input, textarea'))
      .some(field => field.value !== field.defaultValue)) return;
  renderedDeals = dealSnapshot;
  renderWorkspace(st.deals);
}}
function schedulePolling() {{
  clearInterval(pollTimer);
  if (liveUpdates && !document.hidden) pollTimer = setInterval(() => refreshState(true), 4000);
}}

document.addEventListener('visibilitychange', () => {{
  schedulePolling();
  if (!document.hidden && liveUpdates) refreshState(true);
}});
function money(v) {{ return v == null ? '—' : 'S$' + Number(v).toLocaleString('en-SG',
  {{minimumFractionDigits: 2}}); }}
function hours(value) {{ return Number(value).toFixed(2); }}
function esc(s) {{ const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  // Keep HTML entities out of the raw script text.  Browsers treat script
  // contents as raw text, but HTML extraction/sanitising tools may decode an
  // entity before parsing the JavaScript.  Building the entity at runtime is
  // safe in both cases.
  return d.innerHTML.replace(/"/g, '&' + 'quot;').replace(/'/g, '&' + '#39;'); }}
const PILL_TONE = {{new:'review', evaluated:'review', change_requested:'review',
  needs_information:'review', draft:'review', checked:'review', published:'approved',
  blocked:'blocked', confirmed:'booked', closed:'neutral', superseded:'neutral',
  rejected:'neutral'}};
const STATUS_LABELS = {{needs_information: 'Needs info'}};
function statusChip(s) {{
  const tone = PILL_TONE[s] || 'review';
  const label = STATUS_LABELS[s] || String(s).replace(/_/g,' ');
  return '<span class="status-chip ' + esc(s) + ' pill-' + tone + '">' + esc(label) + '</span>';
}}
function latestVersion(d) {{ return d.versions.length ? d.versions[d.versions.length - 1] : null; }}
function needsAttention(d) {{
  const v = latestVersion(d);
  return d.status === 'needs_information' || !v || ['evaluated', 'draft', 'blocked', 'checked'].includes(v.status)
    || (v.status === 'published' && v.accepted_at && !v.confirmed);
}}
function queueTone(d) {{
  const v = latestVersion(d);
  if (v && v.status === 'blocked') return 'blocked';
  if (v && ['published', 'confirmed'].includes(v.status)) return v.status === 'confirmed' ? 'booked' : 'approved';
  return 'review';
}}
function selectDeal(dealId) {{
  selectedDealId = dealId;
  renderWorkspace(latestDeals);
}}
const STEP_LABELS = {{claimed: 'Claimed', resume: 'Claimed (resume)', intake: 'Intake',
  needs_information: 'Needs information',
  scoping: 'Scoping', engine: 'Engine', engine_evaluate: 'Engine (evaluation)',
  reevaluate: 'Engine (re-evaluation)', draft: 'Draft', edit_draft: 'Draft (edit)',
  outbound: 'Outbound', outbound_check: 'Outbound (check)', review: 'Review',
  approve: 'Review (approve)', reject: 'Review (reject)', client_change: 'Client change',
  client_accept: 'Client accepted', capacity_revalidate: 'Capacity revalidated',
  rotate_token: 'Link reissued', confirm_booking: 'Booking confirmed',
  close_deal: 'Deal closed'}};
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
const STAGE_DEFS = [
  {{key: 'intake', label: 'Intake', skill: 'bidpilot-intake',
    steps: ['claimed', 'resume', 'intake', 'needs_information'], outputs: ['intake']}},
  {{key: 'scoping', label: 'Scoping', skill: 'bidpilot-scoping',
    steps: ['scoping'], outputs: ['scoping']}},
  {{key: 'engine', label: 'Engine evaluation', skill: 'bidpilot-engine',
    steps: ['engine', 'engine_evaluate', 'reevaluate'], outputs: []}},
  {{key: 'draft', label: 'Negotiator draft', skill: 'bidpilot-negotiator',
    steps: ['draft', 'edit_draft'], outputs: ['negotiator']}},
  {{key: 'outbound', label: 'Outbound check', skill: 'bidpilot-engine outbound gate',
    steps: ['outbound', 'outbound_check'], outputs: []}},
  {{key: 'review', label: 'Founder review', skill: 'founder dashboard',
    steps: ['review', 'approve', 'reject', 'capacity_revalidate', 'rotate_token'],
    outputs: []}},
  {{key: 'client', label: 'Client response', skill: 'client page',
    steps: ['client_change', 'client_accept'], outputs: []}},
  {{key: 'booking', label: 'Booking', skill: 'founder dashboard',
    steps: ['confirm_booking', 'close_deal'], outputs: []}}
];
// Normalise a raw run-step status to one timeline tone. blocked/error and the
// capacity_changed guard all read as "blocked" (red); ok/passed read as done.
function normStatus(s) {{
  if (s === 'blocked' || s === 'error' || s === 'capacity_changed') return 'blocked';
  if (s === 'running') return 'running';
  if (s === 'queued') return 'queued';
  if (s === 'skipped') return 'skipped';
  return 'ok';
}}
const STAGE_TONE = {{ok: 'approved', running: 'review', queued: 'review',
  blocked: 'blocked', pending: 'neutral', skipped: 'neutral'}};
const STAGE_LABEL = {{ok: 'done', running: 'running', queued: 'queued',
  blocked: 'blocked', pending: 'not started', skipped: 'skipped'}};
function stageEvents(d, def) {{
  const events = d.steps.filter(s => def.steps.includes(s.step_key)).map(s => ({{
    at: s.finished_at || s.started_at,
    label: STEP_LABELS[s.step_key] || String(s.step_key).replace(/_/g, ' '),
    status: s.status, note: s.private_note, trigger: s.trigger}}));
  d.agent_outputs.filter(a => def.outputs.includes(a.kind)).forEach(a => events.push({{
    at: a.created_at, label: def.skill + ' output saved', status: 'ok',
    note: null, trigger: null}}));
  return events.sort((a, b) => (a.at || 0) - (b.at || 0));
}}
// Stage state reflects the most recent event, so a stage that was blocked and
// then cleared reads as done — while the blocked event itself stays red below.
function stageStatus(events) {{
  if (!events.length) return 'pending';
  return normStatus(events[events.length - 1].status);
}}
function stageTimelineHtml(d) {{
  const items = STAGE_DEFS.map(def => {{
    const events = stageEvents(d, def);
    const status = stageStatus(events);
    const last = events[events.length - 1];
    const blockedCount = events.filter(e => normStatus(e.status) === 'blocked').length;
    const triggers = Array.from(new Set(events.map(e => e.trigger).filter(Boolean)));
    const when = last && last.at ? new Date(last.at * 1000).toISOString() : null;
    const note = last && last.note ? String(last.note).slice(0, 160) : '';
    const triggerHtml = triggers.length
      ? `<span class="stage-trigger">${{triggers.map(esc).join(' + ')}} trigger</span>`
      : '<span class="stage-trigger">trigger not recorded</span>';
    const blockedBadge = (blockedCount && status !== 'blocked')
      ? `<span class="status-chip pill-blocked">${{blockedCount}} blocked earlier</span>` : '';
    const stepsHtml = events.length > 1 ? `<ul class="stage-steps">${{events.map(e => {{
        const tone = normStatus(e.status);
        const t = e.at ? new Date(e.at * 1000).toISOString() : '';
        return `<li class="ev-${{tone}}"><b>${{esc(e.label)}}</b>
          <span class="status-chip pill-${{STAGE_TONE[tone]}}">${{esc(e.status)}}</span>
          ${{e.trigger ? '<span class="stage-trigger">' + esc(e.trigger) + '</span>' : ''}}
          <time class="small" datetime="${{t}}">${{t}}</time>
          ${{e.note ? `<div class="small stage-note">${{esc(String(e.note).slice(0, 160))}}</div>` : ''}}</li>`;
      }}).join('')}}</ul>` : '';
    return `<li class="stage stage-${{status}}" data-stage="${{def.key}}">
      <div class="stage-head">
        <span class="stage-label">${{esc(def.label)}}</span>
        <span class="status-chip pill-${{STAGE_TONE[status]}}">${{esc(STAGE_LABEL[status])}}</span>
        ${{blockedBadge}}
        <span class="stage-skill small">${{esc(def.skill)}}</span>
        ${{triggerHtml}}
      </div>
      ${{when ? `<time class="small" datetime="${{when}}">${{when}}</time>`
              : '<span class="small">no recorded time</span>'}}
      ${{note ? `<div class="small stage-note">${{esc(note)}}</div>` : ''}}
      ${{stepsHtml}}
    </li>`;
  }}).join('');
  return `<ol class="stage-timeline" id="stage-timeline-${{d.deal_id.slice(0,8)}}"
    aria-label="Deal stage timeline">${{items}}</ol>`;
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
{DASH_JS}
function msg(id, text) {{
  const el = document.getElementById('msg-' + id);
  if (el) el.textContent = text;
  else showError(text);
}}
async function previewCapacity(version, option, button) {{
  const output = button.nextElementSibling;
  const r = await api('/api/capacity-preview', {{version_id: version, option_id: option}});
  if (!r.data.ok || !r.data.fits) {{
    output.textContent = r.data.error || 'This option no longer fits the available capacity.';
    return;
  }}
  const weeks = r.data.weeks.map(w => w.week + ': ' + hours(w.before) + ' → ' + hours(w.after) + ' free');
  const conflicts = r.data.affected.map(d => d.client + ': ' + d.lost.map(o => o.deliverables + ' videos by ' + o.deadline).join(', ')
      + ' would no longer fit (' + d.remaining + ' other feasible options).');
  output.textContent = weeks.concat(conflicts.length ? conflicts : ['Other outstanding proposals still fit.']).join(' · ');
}}
async function doEvaluate(dealId, offerId) {{
  const r = await api('/api/evaluate', {{deal_id: dealId, offer_id: offerId}});
  msg(offerId, r.data.ok ? 'Engine evaluation complete.' : 'Evaluate failed.');
  refresh();
}}
async function doCheck(id) {{ const r = await api('/api/check', {{version_id: id}});
  msg(id, r.data.ok ? 'Check passed. Approve and send is unlocked.' : 'Blocked. Fix the draft, then run the check again.');
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
  const r = await api('/api/approve', approvePayload(id));
  if (r.data.ok) {{
    msg(id, 'Released.');
    showRelease(id, r.data.url);
  }} else {{
    msg(id, 'Refused: ' + (r.data.reasons || []).join(', '));
  }}
  refresh();
}}
async function doReject(id) {{ const r = await api('/api/reject', {{version_id: id}});
  msg(id, r.data.ok ? 'Rejected; nothing released.'
    : 'Reject refused: ' + (r.data.reasons || []).join(', ')); refresh(); }}
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
  let h = 5381, g = 0; const s = dealId + '|' + body;
  for (let i = 0; i < s.length; i++) {{
    const c = s.charCodeAt(i);
    h = ((h << 5) + h + c) >>> 0;
    g = (c + (g << 6) + (g << 16) - g) >>> 0;
  }}
  const requestId = 'founder-' + h.toString(16) + '-' + g.toString(16);
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

class StudioServer(ThreadingHTTPServer):
    """One media consumer per serving process. SQLite leases coordinate restarts."""
    def serve_forever(self, poll_interval=0.5):
        stop = threading.Event()
        def consume():
            while not stop.is_set():
                try:
                    if conversation.process_media_once(self.db_path):
                        continue
                except (OSError, ValueError, sqlite3.Error):
                    pass
                stop.wait(1)
        worker = threading.Thread(target=consume, name="reference-media", daemon=True)
        worker.start()
        try:
            super().serve_forever(poll_interval)
        finally:
            stop.set()
            worker.join(timeout=2)


def load_owner_token(path):
    """Reuse the owner token kept in a private local file, creating it once.

    The file lives outside the repository (default ~/.bidpilot/owner_token)
    with 0600 permissions, so restarting the server keeps the same founder
    link working.
    """
    path = Path(path).expanduser()
    try:
        token = path.read_text().strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
            return token
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token + "\n")
    os.chmod(path, 0o600)
    return token


def make_server(db_path, port=0, public_origin=None, embed_origin=None,
                qwenwork_secret_file=None, owner_token_file=None):
    """Build a server on loopback with the founder owner token.

    Returns (server, owner_token). Without owner_token_file the token is fresh
    per launch (tests); with it, the token is read from that private file so
    it survives restarts. It is never logged to the database or server log
    beyond the launch line.
    """
    public_origin = https_origin(public_origin) if public_origin else None
    embed_origins = ([https_origin(part.strip()) for part in embed_origin.split(",") if part.strip()]
                     if embed_origin else [])
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    try:
        core.load_seed(conn)
    finally:
        conn.close()
    server = StudioServer(("127.0.0.1", port), Handler)
    server.db_path = db_path
    server.public_origin = public_origin
    server.embed_origins = embed_origins
    server.owner_token = (load_owner_token(owner_token_file) if owner_token_file
                          else secrets.token_urlsafe(24))
    server.qwenwork_secret = qwenwork_callback.load_secret(qwenwork_secret_file)
    return server, server.owner_token


def main():
    parser = argparse.ArgumentParser(description="Run the loopback-only BidPilot dashboard.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--public-origin", type=https_origin, help="HTTPS origin served by the buyer-facing reverse proxy")
    parser.add_argument("--embed-origin", help="Comma-separated HTTPS origins allowed to embed the inquiry (Hashmeta site and any embed shell host)")
    parser.add_argument("--owner-token-file",
                        default=os.environ.get("BIDPILOT_OWNER_TOKEN_FILE", "~/.bidpilot/owner_token"),
                        help="Private file holding the founder token so links survive restarts"
                             " (use 'none' for a fresh token each launch)")
    parser.add_argument("--qwenwork-secret-file", help="File holding the shared secret for POST /qwenwork/callback (or set BIDPILOT_QWENWORK_SECRET)")
    args = parser.parse_args()
    token_file = None if str(args.owner_token_file).lower() == "none" else args.owner_token_file
    server, owner_token = make_server(args.db, args.port, args.public_origin, args.embed_origin,
                                      args.qwenwork_secret_file, owner_token_file=token_file)
    port = server.server_address[1]
    with server:
        print(f"BidPilot founder dashboard: http://127.0.0.1:{port}/?owner={owner_token}",
              flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
