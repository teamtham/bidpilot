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
from datetime import date, datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core
import deal_logic
import price_list
import dashboard_ops
import change_chat
import commerce
import conversation
import estimating
import portfolio
import qwenwork_callback
import qwenwork_api
from studio_modules import modules
import db

DB_PATH = Path(__file__).resolve().parent / "bidpilot.db"
SAMPLE_BADGE = core.SAMPLE_BADGE
QUOTE_LIMIT = 16 * 1024
_quote_lock = threading.Lock()
_quote_times = []


OWNER_LINK_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Open your owner link</title>
<style>body{margin:0;background:#f2f4f3;color:#17201d;font:15px/1.5 'Instrument Sans',system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;padding:16px}
main{background:#fff;border:1px solid #dde2df;border-radius:10px;padding:24px;max-width:460px}h1{font-size:20px;margin:0 0 8px}p{margin:0 0 8px;color:#5d6a65}</style></head>
<body><main><h1>Open your owner link on this Mac</h1>
<p>The founder dashboard only opens from your private owner link, the one that ends in <b>?owner=</b>.</p>
<p>It is the last line starting with "BidPilot founder dashboard" in <b>~/hackathon/BidPilot/server.log</b>. Open it once in this browser and the dashboard will remember you.</p></main></body></html>"""


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
        # A browser opening an owner page on this Mac without the owner link
        # gets a short plain page instead of raw JSON. The token is never
        # shown; API callers and the public tunnel keep the JSON 403.
        public = getattr(self, "server", None) and getattr(self.server, "public_origin", None)
        on_public = bool(public) and self.headers.get("Host", "").lower() == urllib.parse.urlsplit(public).netloc
        if (self.command == "GET" and not on_public and msg == "owner token required"
                and "text/html" in (self.headers.get("Accept") or "")):
            return self.send_html(OWNER_LINK_PAGE, code=403)
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
            if path == '/qwenwork/callback':
                try:
                    self.server.qwenwork_api.finish_login(self.query())
                    return self.send_html('<h1>QwenWork connected, you can close this tab</h1>')
                except qwenwork_api.APIError as exc:
                    return self.send_html('<h1>QwenWork connection failed</h1><p>' + html.escape(str(exc)) + '</p>', 400)
            if path == '/qwenwork/connect':
                if not self.owner_ok():
                    return self.forbidden('owner token required')
                try:
                    url = self.server.qwenwork_api.start_login()
                except qwenwork_api.APIError as exc:
                    return self.send_json({'ok': False, 'error': str(exc)}, 503)
                self.send_response(302)
                self.send_header('Location', url)
                self.send_header('Cache-Control', 'no-store')
                self._owner_cookie_header()
                self.end_headers()
                return
            if (path == "/estimating" or path.startswith("/api/estimating/")) and not self.owner_ok():
                return self.forbidden("owner token required")
            if any(module.dispatch_get(self, path) for module in modules()):
                return
            if path == "/quote":
                return self.send_html(quote_page())
            if path == "/" or path == "/dashboard":
                if not self.owner_ok():
                    return self.forbidden("owner token required")
                return self.send_html(founder_page(self.conn, getattr(self.server, 'qwenwork_api', None)))
            if path == "/submitted-briefs":
                if not self.owner_ok():
                    return self.forbidden("owner token required")
                return self.send_html(submitted_briefs_page(self.conn))
            if path == "/api/state":
                if not self.owner_ok():
                    return self.forbidden("owner token required")
                # Catch calendar changes made anywhere else (a client booking,
                # another tool) so the dashboard never shows an out-of-date estimate.
                refresh_stale_estimates(self.conn)
                self.conn.execute("BEGIN")
                return self.send_json(state(self.conn))
            if path.startswith("/p/"):
                token = path[3:].strip("/")
                return self.client_page(token)
            if path.startswith("/stock/"):
                return self.stock_media(path[7:])
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
        if path == '/api/qwenwork/draft':
            try:
                result = self.server.qwenwork_api.request_draft(self.conn, data['version_id'])
            except qwenwork_api.APIError as exc:
                return self.send_json({'ok': False, 'error': str(exc)}, 400)
            return self.send_json({'ok': True, **result}, 202)
        if estimating.dispatch_post(self, path, data):
            return
        if path in ('/api/calendar/hours', '/api/calendar/block', '/api/calendar/remove'):
            try:
                revision = core.cap.change_calendar(self.conn, path.rsplit('/', 1)[-1], data)
            except (ValueError, TypeError) as exc:
                return self.send_json({'ok': False, 'message': str(exc)}, 400)
            # Estimates follow the calendar: re-run the ones this change affects.
            return self.send_json({'ok': True, 'revision': revision,
                                   'rerun': refresh_stale_estimates(self.conn)})
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

        if path in ("/api/price-list", "/api/price-list-estimate", "/api/send-question"):
            try:
                if path == "/api/price-list":
                    saved = price_list.save(self.conn, data)
                    template = (price_list.save_template_settings(self.conn, data["template"])
                                if isinstance(data.get("template"), dict) else None)
                    return self.send_json({"ok": True, "items": saved, "template": template})
                if path == "/api/send-question":
                    return self.send_json(core.send_question(self.conn, data.get("deal_id"), data.get("question")))
                return self.send_json({"ok": True, **price_list.estimate(self.conn, data)})
            except (ValueError, KeyError, TypeError) as exc:
                self.conn.rollback()
                text = str(exc)
                message = text if " " in text and text[:1].isupper() else deal_logic.explain(text)
                return self.send_json({"ok": False, "error": text, "message": message}, 400)

        if path == "/api/manual-estimate":
            try:
                result = core.enter_manual_estimate(self.conn, data)
            except (ValueError, KeyError, TypeError) as exc:
                return self.send_json({"ok": False, "error": str(exc), "message": "Enter positive hours and list price for an open deal with a saved offer."}, 400)
            return self.send_json({"ok": True, **result})

        if path == "/api/check":
            if not isinstance(data.get('version_id'), str) or not data['version_id'].strip():
                return self.send_json(_explained({'ok': False, 'reasons': ['version_not_found']}), 400)
            try:
                outcome = core.check_draft(self.conn, data["version_id"], offered_option_ids=data.get('option_ids'))
            except ValueError as exc:
                return self.send_json(_explained({'ok': False, 'reasons': [str(exc)]}), 409)
            body = {"ok": outcome["passed"], "result": outcome}
            if not outcome["passed"]:
                messages = [deal_logic.explain_violation(v) for v in outcome["violations"]]
                body["message"] = " ".join(dict.fromkeys(messages))
                body['code'] = outcome['violations'][0]['code'] if outcome['violations'] else 'check_refused'
            return self.send_json(body)

        if path == "/api/draft":
            new_id = core.save_draft(self.conn, data["version_id"], data["body"])
            return self.send_json({"ok": True, "version_id": new_id})

        if path == '/api/release-review':
            import delivery
            version = self.conn.execute('SELECT * FROM proposal_versions WHERE id=?', (data['version_id'],)).fetchone()
            if not version:
                return self.send_json({'ok': False}, 404)
            current_digest, _ = delivery.review(self.conn, version)
            if data.get('expected_digest') is not None and data['expected_digest'] != current_digest:
                return self.send_json({'ok': False, 'error': 'review_mismatch'}, 409)
            digest, ids = delivery.review(self.conn, version, data.get('option_ids'))
            return self.send_json({'ok': True, 'reviewed_digest': digest, 'option_ids': ids})

        if path == '/api/retry-email':
            import delivery
            try:
                result = delivery.deliver_once(self.conn, data['version_id'], retry=True)
                return self.send_json({'ok': result['status'] == 'accepted', **result})
            except ValueError as exc:
                return self.send_json({'ok': False, 'error': str(exc)}, 409)

        if path in ('/api/approve', '/api/approve-and-send'):
            res = core.approve(self.conn, data["version_id"], data.get("option_id"),
                               offered_option_ids=data.get("option_ids"),
                               reviewed_digest=data.get('reviewed_digest'), send_email=path == '/api/approve-and-send')
            if res['ok'] and path == '/api/approve-and-send':
                import delivery
                result = delivery.deliver_once(self.conn, data['version_id'])
                res['email_status'] = result['status']
            return self.send_json(_explained(res), 200 if res["ok"] else 409)

        if path == "/api/reject":
            res = core.reject(self.conn, data["version_id"], data.get("note", ""))
            return self.send_json(_explained(res), 200 if res["ok"] else 409)

        if path == "/api/rotate-token":
            res = core.rotate_token(self.conn, data["version_id"])
            return self.send_json(_explained(res), 200 if res["ok"] else 409)

        if path == "/api/refresh-estimate":
            try:
                res = refresh_estimate_for_send(self.conn, data["version_id"], bool(data.get("force")))
            except ValueError as exc:
                return self.send_json({"ok": False, "error": str(exc), **deal_logic.refusal([str(exc)])}, 409)
            return self.send_json(res, 200 if res["ok"] else 404)

        if path == "/api/reevaluate":
            try:
                offer_id = core.reevaluate(self.conn, data["offer_id"])
            except ValueError as exc:
                return self.send_json({"ok": False, "error": str(exc),
                                       **deal_logic.refusal([str(exc)])}, 400)
            eval_id, version_id = core.evaluate(
                self.conn, data["deal_id"], offer_id)
            return self.send_json({"ok": True, "offer_id": offer_id,
                                   "eval_id": eval_id, "version_id": version_id})

        if path == "/api/confirm-booking":
            res = core.confirm_booking(self.conn, data["version_id"])
            if res["ok"]:
                res["rerun"] = refresh_stale_estimates(self.conn)
            code = 200 if res["ok"] else 409
            return self.send_json(_explained(res), code)

        if path == "/api/close-deal":
            res = core.close_deal(self.conn, data["deal_id"])
            return self.send_json(_explained(res), 200 if res["ok"] else 409)

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
                # Price the change straight away with the same engine as a new
                # brief, so Alex sees what it costs. Idempotent: an agent that
                # evaluates the same offer later gets this result back.
                try:
                    core.evaluate(self.conn, version["deal_id"], offer_id)
                except Exception as exc:  # Alex can still run the estimate by hand.
                    try:
                        self.conn.rollback()
                    except sqlite3.Error:
                        pass
                    sys.stderr.write("change estimate deferred: %s\n" % type(exc).__name__)
                return self.send_json({"ok": True, "status": "offered", "offer_id": offer_id})
            chat = re.fullmatch(r"/p/([^/]+)/change-chat", path)
            if chat:
                try:
                    if data.get("reset") is True:
                        result = change_chat.reset(self.conn, chat[1])
                    else:
                        result = change_chat.message(self.conn, chat[1], data.get("text"))
                except ValueError as exc:
                    code = 404 if str(exc) == "not_found" else 400
                    return self.send_json({"ok": False, "error": str(exc)}, code)
                return self.send_json({"ok": True, **{k: result[k] for k in
                                       ("terms", "changed", "turns", "ready", "client_words")}})
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

    def stock_media(self, name):
        """Public stock footage used on client proposal pages (fixed allow-list)."""
        if name not in STOCK_MEDIA:
            return self.not_found()
        content = (STOCK_DIR / name).read_bytes()
        return portfolio.send_bytes(self, content, STOCK_MEDIA[name], cache="public, max-age=86400")

    def preview_page(self, version_id):
        """Owner-only: exactly what the client will see, before approval."""
        version = conn_version(self.conn, version_id)
        if not version:
            return self.not_found()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        preview_ids = query.get('option_id')
        if preview_ids is not None:
            allowed = set(db.uj(version['public_option_ids_json']) or [])
            if len(set(preview_ids)) != len(preview_ids) or not set(preview_ids) <= allowed:
                return self.not_found()
        return self.send_html(render_client_html(self.conn, version, preview=True, preview_ids=preview_ids))


def quote_page():
    page = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Request a quote</title>
<style>__QUOTE_STYLE__</style></head>
<body>
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
<div class="actions"><button type="submit">Submit request</button></div></form>
</main>
</body></html>"""
    return (page.replace("QUOTE_REQUEST_ID", secrets.token_urlsafe(18))
                .replace("QUOTE_RESUME_PROOF", secrets.token_urlsafe(32))
                .replace("__SAMPLE_BADGE__", SAMPLE_BADGE)
                .replace("__QUOTE_STYLE__", QUOTE_STYLE))


def conn_version(conn, version_id):
    return conn.execute("SELECT * FROM proposal_versions WHERE id=?",
                         (version_id,)).fetchone()


# One shared option label set (wireframe wording) for the client page, the
# founder dashboard and agent drafts. hold_scope names the real video count.
OPTION_LABELS = deal_logic.OPTION_LABELS
_COUNT_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
                "nine", "ten")


def _explained(res):
    """Add plain-English refusal text (codes stay in `reasons`)."""
    if not res.get("ok"):
        reasons = res.get('reasons') or [res.get('error', 'action_refused')]
        return {**res, 'code': str(reasons[0]).split(':', 1)[0].split('=', 1)[0], **deal_logic.refusal(reasons)}
    return res


def option_label(kind, terms=None):
    return deal_logic.option_label(kind, terms)


PROPOSAL_STYLE = """
:root{color-scheme:light;--s-paper:#faf7f3;--s-ink:#2a1f1a;--s-mute:#7a6c63;--s-line:#e4dcd3;--s-brown:#7b4b2f;--s-tint:#f1e9e1;--s-dark:#3a2a21;--ok:#2c7a3f;--warn:#a2620a;--display:'Newsreader','Iowan Old Style',Georgia,serif;--ui:'Instrument Sans','Helvetica Neue',Arial,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--s-paper);color:var(--s-ink);font:15px/1.5 var(--ui);-webkit-font-smoothing:antialiased}
.site-top{display:flex;align-items:center;justify-content:space-between;padding:14px 28px;border-bottom:1px solid var(--s-line);gap:16px;flex-wrap:wrap}.brand{font:600 19px var(--display)}
.site-body{padding:28px;display:grid;gap:26px;max-width:1240px;margin:auto}.hero2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.15fr);gap:28px;align-items:center}.hero2.top{align-items:start}.col{display:grid;gap:14px;min-width:0;align-content:start}
h3{font:500 30px/1.1 var(--display);margin:0;text-wrap:balance}h4{font:500 20px var(--display);margin:0}h5{margin:0;font:600 15px var(--ui)}.lede{color:var(--s-mute);max-width:58ch;margin:0}.muted{color:var(--s-mute)}.small{font-size:12.5px}figcaption{font-size:12px;color:var(--s-mute)}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.row form{margin:0}.between{justify-content:space-between}
.btn{display:inline-flex;align-items:center;gap:8px;border-radius:6px;padding:10px 16px;font:600 13.5px var(--ui);border:1px solid transparent;cursor:pointer;white-space:nowrap}.btn.p{background:var(--s-brown);color:#fff}.btn.g{border-color:var(--s-line);color:var(--s-ink);background:#fff}button.btn:hover{filter:brightness(1.08)}button.btn.g:hover{background:#f6f2ee}button:disabled,button:disabled:hover{opacity:.45;cursor:not-allowed;filter:none}
.chip{font-size:12px;padding:4px 10px;border-radius:999px;background:var(--s-tint);color:var(--s-ink)}.chip.budget{font-size:13px;padding:6px 12px}.tag{justify-self:start;font-size:11.5px;font-weight:600;padding:2px 8px;border-radius:999px;background:var(--s-tint);color:var(--s-brown)}
.banner{background:var(--s-tint);border-radius:8px;padding:10px 14px;font-size:13px;margin:0}.replacement{background:var(--s-tint);border-radius:8px;padding:10px 14px;font-size:13px}
.opt{border:1px solid var(--s-line);border-radius:10px;padding:16px;background:#fff;display:grid;gap:10px;position:relative;align-content:start;min-width:0}.optgrid .opt{cursor:pointer}.optgrid .opt:hover{border-color:var(--s-brown)}.opt>input{position:absolute;opacity:0;width:1px;height:1px;pointer-events:none}.optgrid .opt:has(input:checked){border:2px solid var(--s-brown);box-shadow:0 0 0 4px var(--s-tint);padding:15px}.opt:has(input:focus-visible){outline:2px solid var(--s-brown);outline-offset:3px}.opt:has(input:disabled){cursor:default}
.opt-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap}.price{font:500 26px var(--display);font-variant-numeric:tabular-nums}.fit{font-size:12.5px;font-weight:600}.fit.ok{color:var(--ok)}.fit.over{color:var(--warn)}
.optgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.ticks{list-style:none;margin:0;padding:0;display:grid;gap:5px;font-size:13px}.ticks li{display:flex;gap:8px}.ticks li:before{content:'';flex:none;width:12px;height:7px;margin-top:5px;border-left:2px solid var(--s-brown);border-bottom:2px solid var(--s-brown);transform:rotate(-45deg)}
.gantt{display:grid;gap:6px;font-size:12px}.g-row{display:grid;grid-template-columns:92px 1fr;gap:10px;align-items:center}.g-bar{height:14px;background:var(--s-tint);border-radius:3px;position:relative}.g-bar i{position:absolute;top:0;bottom:0;background:var(--s-brown);border-radius:3px}.g-bar i.lite{background:#b98b6c}.g-axis{display:flex;justify-content:space-between}
.story{display:grid;gap:6px}.story h4{font-size:18px}
.media{position:relative;aspect-ratio:16/9;max-width:100%;border-radius:6px;overflow:hidden;color:#fff;isolation:isolate;background:linear-gradient(180deg,#1f1611,#3d2a1f)}.media.reel{aspect-ratio:9/16}.media.photo{aspect-ratio:4/3}
.media>video,.media>img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;z-index:-1;background:#1d1714}
.lb{position:absolute;left:0;right:0;height:9%;background:#000;opacity:.85;z-index:1}.lb.t{top:0}.lb.b{bottom:0}
.play{position:absolute;inset:0;margin:auto;width:52px;height:52px;border-radius:50%;background:rgba(255,255,255,.9);display:grid;place-items:center;z-index:1;pointer-events:none}.play::after{content:'';width:0;height:0;border-left:15px solid #2a1f1a;border-block:9px solid transparent;margin-left:4px}
.media.playing .play{opacity:0;transition:opacity .2s}.media.playing:hover .play{opacity:1}.media.playing .play::after{width:12px;height:16px;border:0;border-left:4px solid #2a1f1a;border-right:4px solid #2a1f1a;margin:0}
.tc{position:absolute;left:10px;bottom:calc(9% + 6px);font:600 11px var(--ui);font-variant-numeric:tabular-nums;background:rgba(0,0,0,.55);padding:2px 6px;border-radius:3px;z-index:1}
.shot{position:absolute;right:10px;top:calc(9% + 6px);max-width:70%;font-size:11px;line-height:1.3;background:rgba(0,0,0,.55);padding:3px 7px;border-radius:3px;text-align:right;z-index:1}.media.photo .shot,.media.reel .shot{top:8px}
.scrub{position:absolute;left:10px;right:10px;bottom:calc(9% - 10px);height:3px;background:rgba(255,255,255,.3);border-radius:2px;z-index:1}.scrub i{display:block;height:100%;width:0;background:#fff;border-radius:2px}
.media[data-play]{cursor:pointer}.media[data-play]:focus-visible{outline:2px solid var(--s-brown);outline-offset:3px}
.strip{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}figure{margin:0;display:grid;gap:6px}
.formpeek{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;border:1px solid var(--s-line);border-radius:10px;padding:16px;background:#fff;margin:0}.formpeek label{display:grid;gap:4px;font-size:12.5px;color:var(--s-mute)}.formpeek .wide{grid-column:1/-1}.formpeek input,.formpeek textarea{font:13.5px var(--ui);color:var(--s-ink);border:1px solid var(--s-line);border-radius:6px;padding:8px 10px;background:#fff;width:100%}.formpeek textarea{min-height:72px;resize:vertical}.formpeek p{margin:0}
.booked{display:grid;gap:14px;max-width:560px}.booked .media{max-width:560px}
.library{border-top:1px solid var(--s-line);padding-top:22px;max-width:760px}.library>*{margin-bottom:10px!important}.library h2{font:500 22px var(--display);margin:0}.library p{margin:0;color:var(--s-mute)}.library label{font-size:12.5px;color:var(--s-mute);margin-right:6px}.library select,.library input{font:13.5px var(--ui);color:var(--s-ink);border:1px solid var(--s-line);border-radius:6px;padding:7px 10px;background:#fff;margin-right:10px}.library form{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.library button{font:600 13px var(--ui);border-radius:6px;padding:8px 14px;border:1px solid var(--s-line);background:#fff;color:var(--s-ink);cursor:pointer}
.change{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:18px;align-items:start;border-top:1px solid var(--s-line);padding-top:22px}.change .wide,.change-head{grid-column:1/-1}.change-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.chat{background:#fff;border:1px solid var(--s-line);border-radius:10px;display:flex;flex-direction:column;min-width:0}.chat-head{display:flex;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid var(--s-line)}.chat-head b{font-size:13.5px}.ava{width:30px;height:30px;border-radius:50%;background:var(--s-dark);color:#fff;font:600 12px/30px var(--ui);text-align:center;flex:none}.live{margin-left:auto;font-size:11.5px;color:var(--ok);display:inline-flex;align-items:center;gap:6px}.live::before{content:'';width:7px;height:7px;border-radius:50%;background:var(--ok)}
.msgs{padding:14px;display:grid;gap:10px;min-height:220px;max-height:440px;overflow-y:auto;align-content:start}.m{max-width:86%;padding:9px 12px;border-radius:12px;font-size:13.5px;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere}.m.bot{background:var(--s-tint);border-top-left-radius:4px}.m.me{background:var(--s-dark);color:#fff;justify-self:end;border-top-right-radius:4px}.m.wait{color:var(--s-mute)}
.compose{display:flex;gap:8px;padding:10px 14px;border-top:1px solid var(--s-line);align-items:center;margin:0}.compose input{flex:1;min-width:0;border:1px solid var(--s-line);border-radius:8px;padding:9px 10px;font:13.5px var(--ui);color:var(--s-ink);background:#fff}.compose .btn{padding:8px 14px}.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.summary{background:#fff;border:1px solid var(--s-line);border-radius:10px;padding:16px;display:grid;gap:12px;min-width:0}.summary dl{display:grid;grid-template-columns:auto 1fr;gap:7px 14px;margin:0;font-size:13.5px}.summary dt{color:var(--s-mute)}.summary dd{margin:0;font-variant-numeric:tabular-nums}.summary del{color:var(--s-mute);margin-right:4px}.summary ins{text-decoration:none;font-weight:600}.summary .btn.p{justify-content:center}
.said{font-size:12.5px;background:var(--s-tint);border-radius:8px;padding:8px 10px;overflow-wrap:anywhere}.lock{font-size:12px;color:var(--s-mute)}.sent{display:none;background:#e4f3e7;color:#22622f;border-radius:8px;padding:10px 12px;font-size:13px}.summary.done .sent{display:block}.summary.done #cc-send,.summary.done #cc-reset{display:none}
.sent-note{background:#e4f3e7;color:#22622f;border-radius:8px;padding:12px 14px;font-size:13.5px}.collapsed{border:1px dashed var(--s-line);border-radius:10px;padding:12px 14px;font-size:13px;color:var(--s-mute);display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}.collapsed b{color:var(--s-ink)}.btn.link{background:none;border:0;padding:0;color:var(--s-brown);text-decoration:underline;font-weight:500;justify-self:start}
#msg{margin:0;font-size:13px}#msg:empty{display:none}[hidden]{display:none!important}:focus-visible{outline:2px solid var(--s-brown);outline-offset:3px}
@media(max-width:820px){.hero2,.optgrid,.formpeek,.change{grid-template-columns:1fr}.site-body,.site-top{padding-left:16px;padding-right:16px}.btn{white-space:normal}h3{font-size:26px}}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
"""

# Stock footage (Pexels, free licence) shown where the wireframe has media
# and no sharing-cleared portfolio piece was picked for this proposal.
STOCK_DIR = Path(__file__).with_name("stock")
STUDIO_TZ = ZoneInfo("Asia/Singapore")
STOCK_MEDIA = {name: "video/mp4" for name in (
    "coffee.mp4", "bakery.mp4", "vertical.mp4", "espresso.mp4", "street.mp4", "bts.mp4")}


def _media_html(src, mime="video/mp4", shot="", kind="", playable=True, alt=""):
    """Wireframe media frame: letterbox, label, play button, scrubber, timecode."""
    src = _esc(src)
    if mime.startswith("image/"):
        inner = f'<img src="{src}" alt="{_esc(alt or shot)}">'
        playable = False
    else:
        start = "#t=0.5" if playable else "#t=2"
        inner = (f'<video src="{src}{start}" playsinline preload="{"metadata" if playable else "auto"}"'
                 f'{"" if playable else " muted"} aria-hidden="true"></video>')
    label = f'<span class="shot">{_esc(shot)}</span>' if shot else ""
    if not playable:
        return f'<div class="media {kind}">{inner}{label}</div>'
    letterbox = '' if kind else '<span class="lb t"></span><span class="lb b"></span>'
    return (f'<div class="media {kind}" data-play role="button" tabindex="0" aria-label="Play video">'
            f'{inner}{letterbox}{label}<span class="play"></span>'
            f'<span class="scrub"><i></i></span><span class="tc">00:00</span></div>')


def _client_label(label, counter):
    """Client-facing wording from the wireframe: cards lead with what the client keeps."""
    if not counter:
        return label
    if label.startswith("Keep the "):
        return "Keeps your " + label[len("Keep the "):]
    if label.startswith("Keep all "):
        return "Keeps all " + label[len("Keep all "):]
    return label


def _plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _change_chat_html(conn, version, preview, counter_design, change_panel):
    """The studio-assistant change chat that replaced "Explore studio work"."""
    sent = conn.execute("SELECT 1 FROM messages WHERE version_id=? AND direction='inbound'",
                        (version["id"],)).fetchone()
    head = ('<div class="change-head"><h4>' + ('Want something else changed?' if counter_design else 'Want something changed?') +
            '</h4><span class="small muted">Tell the studio assistant. I review every change myself.</span></div>')
    if sent and not preview:
        return ('<section class="change" id="change">' + head + '<div class="sent-note wide" role="status">'
                '<b>Your change request is with Alex.</b> I\'ll check the calendar and reply with an updated proposal. '
                'This proposal stays open until then.</div></section>')
    try:
        st = change_chat.state(conn, version)
    except (KeyError, TypeError, ValueError):
        return ''
    base, terms = st["base"], st["terms"]

    def fmt(key, value):
        if value is None:
            return 'Not given'
        if key == 'deadline':
            return deal_logic.client_date(value)
        if key == 'budget':
            return deal_logic.client_amount(value)
        return str(value)

    def cell(key):
        if key in st["changed"]:
            return '<del>' + _esc(fmt(key, base.get(key))) + '</del> <ins>' + _esc(fmt(key, terms.get(key))) + '</ins>'
        return _esc(fmt(key, terms.get(key)))
    turns = [{"sender": "assistant", "text": change_chat.greeting(base)}] + st["turns"]
    msgs = ''.join('<div class="m ' + ('me' if t["sender"] == "client" else 'bot') + '">' + _esc(t["text"]) + '</div>'
                   for t in turns)
    n = len(st["changed"])
    disabled = ' disabled' if preview else ''
    said = ('You said: "' + st["client_words"] + '"') if st["client_words"] else ''
    return (
        '<section class="change" id="change" data-base="' + _esc(json.dumps(base)) + '">' + head +
        '<div class="chat"><div class="chat-head"><span class="ava" aria-hidden="true">AT</span><div><b>Studio assistant</b>'
        '<div class="small muted">Turns your change into clear terms for me</div></div><span class="live">Live</span></div>'
        '<div class="msgs" id="cc-msgs" aria-live="polite">' + msgs + '</div>'
        '<form class="compose" id="cc-form"><label for="cc-in" class="sr">Message the studio assistant</label>'
        '<input id="cc-in" type="text" maxlength="1000" autocomplete="off" placeholder="' +
        ('Chat opens on the client\'s link' if preview else 'Say what you\'d like changed') + '"' + disabled + '>'
        '<button class="btn p" type="submit"' + disabled + '>Send</button></form></div>'
        '<aside class="summary" id="cc-card"><div class="row between"><h4>Your change so far</h4><span class="small muted" id="cc-count">' +
        (str(n) + ' change' + ('s' if n != 1 else '') if n else 'Nothing yet') + '</span></div><dl>'
        '<dt>Videos</dt><dd id="cc-count-v">' + cell('count') + '</dd>'
        '<dt>Captioned versions</dt><dd id="cc-captions">' + cell('captions') + '</dd>'
        '<dt>Deadline</dt><dd id="cc-deadline">' + cell('deadline') + '</dd>'
        '<dt>Your budget</dt><dd id="cc-budget">' + cell('budget') + '</dd></dl>'
        '<div class="said" id="cc-said"' + ('' if said else ' hidden') + '>' + _esc(said) + '</div>'
        '<button class="btn p" type="button" id="cc-send" onclick="sendChange(this)"' + ('' if n and not preview else ' disabled') + '>Send to Alex</button>'
        '<div class="sent" role="status">Sent. Alex will check the calendar and reply with an updated proposal. This proposal stays open until then.</div>'
        '<span class="lock">Nothing changes until I reply. The assistant can\'t agree prices or dates.</span>'
        '<button class="btn link" type="button" id="cc-reset" onclick="resetChat()"' + ('' if n and not preview else ' hidden') + '>Start over</button></aside>'
        + ('' if preview else '<div class="collapsed wide"><span><b>Prefer a form?</b> The four things you can change, same as before.</span>'
           '<button class="btn link" type="button" aria-controls="change-panel" onclick="toggleChange(this)">Open the form</button></div>' + change_panel) +
        '</section>')


def render_client_html(conn, version, preview=False, preview_ids=None):
    """Client-facing page. Only approved public option terms cross the
    boundary: no floor, capacity, internal metrics or other client names."""
    option_rows = core.offered_options_of(conn, version)
    if preview and preview_ids is not None:
        option_rows = [r for r in option_rows if r['id'] in preview_ids]
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
    money = deal_logic.client_amount
    when = deal_logic.client_date

    if accepted:
        status_banner = (
            "<p class='banner accepted'>Thank you — your booking is confirmed. I'll be in touch.</p>"
            if version["status"] == "confirmed" else
            "<p class='banner accepted'>Thank you — your acceptance is recorded"
            " and awaits my confirmation.</p>")
        change_form = ""
    elif preview:
        status_banner = "<p class='banner'>Owner preview — nothing has been sent.</p>"
        change_form = ""
    else:
        status_banner = ""
        change_form = f"""
        <form class="formpeek rc" method="post" onsubmit="return submitChangeForm(event)">
            <label class="rc-field">Number of videos
              <input id="rc-count" type="number" min="1" value="{int(selected_terms.get('deliverables', 1))}"></label>
            <label class="rc-field">New deadline
              <input id="rc-deadline" type="date" value="{_esc(selected_terms.get('deadline', ''))}"></label>
            <label class="rc-field">Captioned versions
              <input id="rc-captions" type="number" min="0" value="{int(selected_terms.get('caption_versions', 0))}"></label>
            <label class="rc-field">Your budget (S$)
              <input id="rc-budget" type="number" step="0.01" value="{selected_terms.get('price_sgd', 0)}"></label>
            <label class="wide" for="rc">Your message
              <textarea id="rc" name="body" rows="2" placeholder=
            "Tell me what you'd like changed: number of videos, captioned versions, deadline or budget."></textarea></label>
            <p class="wide small muted">I read your message as written. The numbers above are the terms I'll look at.</p>
            <div class="wide"><button class="btn p" type="submit">Send change request</button></div>
        </form>"""
    # A composition/edit increments version_no without making a counteroffer.
    earlier = list(conn.execute(
        'SELECT v.id, COALESCE(v.published_at, (SELECT MAX(a.decided_at) FROM approvals a '
        'WHERE a.version_id=v.id AND a.action=\'approve\')) AS published_at FROM proposal_versions v WHERE v.deal_id=? '
        'AND (v.published_at IS NOT NULL OR EXISTS '
        '(SELECT 1 FROM approvals a WHERE a.version_id=v.id AND a.action=\'approve\')) '
        'AND v.version_no<? ORDER BY v.version_no DESC',
        (version['deal_id'], version['version_no'])))
    previous_published = earlier[0] if earlier else None
    counter_design = previous_published is not None
    client_version = len(earlier) + 1
    title = "Alex Tan Productions — your proposal" if not preview else "Owner preview — client page"
    story_html = commerce.proposal_html(conn, version, preview)
    hero_title = composed["headline"] or ("Pick the option that fits" if counter_design else "Your proposal")
    hero_intro = composed["introduction"] or version["draft_body"]
    # Keep all prose in the saved, checked composition. Never invent a shoot
    # slot, first-cut schedule, payment split or budget-fit claim for decoration.
    sections = ''.join('<div class="story"><h4>' + _esc(s.get('title', '')) +
                       '</h4><p class="lede">' + _esc(s.get('body', '')) + '</p></div>'
                       for s in composed['sections'])

    # -- media: QwenWork's confirmed portfolio pick first, stock footage otherwise
    picks = []
    for item in composed['portfolio']:
        asset = portfolio.public_asset(conn, item.get('asset_id', ''))
        if asset:
            picks.append((asset, item))
    if picks:
        asset, item = picks[0]
        name = item.get('title') or asset['title']
        featured = _media_html(asset['media_url'], asset['mime'], 'Similar work: ' + name, alt=name)
        featured_caption = _esc(item.get('description') or 'From my portfolio, picked to match your brief')
    else:
        featured = _media_html('/stock/coffee.mp4', shot='Similar work: a launch promo')
        featured_caption = 'Video, from my recent launch work'
    stills = [p for p in picks[1:]]
    if stills:
        strip = ''.join(_media_html(a['media_url'], a['mime'], kind='reel', playable=False,
                                    alt=i.get('title') or a['title']) for a, i in stills[:3])
    else:
        strip = ''.join(_media_html('/stock/' + clip, shot='Reel frame', kind='reel', playable=False)
                        for clip in ('bakery.mp4', 'vertical.mp4', 'espresso.mp4'))
    media_column = ('<div class="col"><figure>' + featured + '<figcaption>' + featured_caption +
                    '</figcaption></figure><div class="strip">' + strip + '</div>'
                    '<figcaption>Stills from vertical deliverables, so you can picture the format</figcaption></div>')

    offer_row = conn.execute('SELECT offer_json, as_of FROM offers WHERE id=?', (version['offer_id'],)).fetchone()
    offer = db.uj(offer_row['offer_json']) if offer_row else {}
    offer = offer.get('offer', offer)
    budget = offer.get('budget_sgd')
    has_budget = isinstance(budget, (int, float)) and not isinstance(budget, bool)
    budget_html = ('<span class="chip budget">Your budget: ' + money(budget) + '</span>'
                   if has_budget else '')

    def headline(terms):
        phases = terms.get('phases') or []
        if phases:
            first, rest = phases[0], phases[1:]
            text = str(first['deliverables']) + ' by ' + when(first['deadline'])
            for p in rest:
                text += ', then ' + str(p['deliverables']) + ' more by ' + when(p['deadline'])
            return text
        return _plural(terms['deliverables'], 'video') + ' by ' + when(terms['deadline'])

    def tick_list(terms):
        ticks = []
        if terms.get('phases'):
            ticks.append('Launch videos first, the rest after')
        if 'caption_versions' in terms:
            ticks.append(_plural(terms['caption_versions'], 'captioned version'))
        if terms.get('shoot_days'):
            ticks.append(_plural(terms['shoot_days'], 'shoot day') +
                         (' at ' + _plural(terms['locations'], 'location') if terms.get('locations') else ''))
        if 'revision_rounds' in terms:
            ticks.append(_plural(terms['revision_rounds'], 'revision round') + ' included')
        return ''.join('<li>' + _esc(t) + '</li>' for t in ticks)

    option_cards = []
    accept_labels = {}
    for row in option_rows:
        terms = db.uj(row['public_terms_json'])
        label = _client_label(option_label(row['option_key'], terms), counter_design)
        accept_labels[row['id']] = ('Accept phased' if terms.get('phases') else
                                    'Accept ' + headline(terms))
        fit = ''
        if has_budget:
            difference = round(terms['price_sgd'] - budget, 2)
            if counter_design:
                fit_text = ('Matches your budget' if difference == 0 else
                            money(abs(difference)) + (' over your budget' if difference > 0 else ' under your budget'))
            else:
                fit_text = ('Your budget: ' + money(budget) + '. ' +
                            ('This proposal fits it.' if difference <= 0 else
                             'This proposal is ' + money(difference) + ' over it.'))
            fit = '<span class="fit ' + ('over' if difference > 0 else 'ok') + '">' + fit_text + '</span>'
        checked = row['id'] == selected or len(option_rows) == 1 or row['id'] == accepted
        radio = ('<input type="radio" name="proposal-option" value="' + _esc(row['id']) + '"' +
                 ' data-accept="' + _esc(accept_labels[row['id']]) + '"' +
                 (' checked' if checked else '') + (' disabled' if accepted else '') + '>')
        if counter_design:
            body = ('<span class="tag">' + _esc(label) + '</span><h5>' + _esc(headline(terms)) +
                    '</h5><span class="price">' + money(terms['price_sgd']) + '</span>' + fit +
                    '<ul class="ticks">' + tick_list(terms) + '</ul>')
        else:
            body = ('<div class="opt-head"><h5>' + _esc(label) + '</h5><span class="price">' +
                    money(terms['price_sgd']) + '</span></div>' + fit + '<ul class="ticks"><li>' +
                    _esc(headline(terms)) + '</li>' + tick_list(terms) + '</ul>')
        option_cards.append('<label class="opt">' + radio + body + '</label>')
    single = len(option_rows) == 1
    cards_html = ('<div class="' + ('options' if single and not counter_design else 'optgrid') +
                  '" id="packages" role="radiogroup" aria-label="Proposal options">' +
                  ''.join(option_cards) + '</div>')
    picked = next((r['id'] for r in option_rows if r['id'] == selected), None) or (
        option_rows[0]['id'] if single else None)
    accept_text = (accept_labels.get(picked) if counter_design and picked else 'Accept this proposal')
    action = ('<button class="btn p" disabled>Preview only - not yet released</button>' if preview else
              '<button class="btn p" disabled>Acceptance recorded</button>' if accepted else
              '<form onsubmit="event.preventDefault(); const chosen=document.querySelector(\'input[name=proposal-option]:checked\'); if(chosen) {this.querySelector(\'button\').disabled=true; accept(chosen.value,this.querySelector(\'button\'));} else setMsg(\'Pick an option first.\'); return false"><button class="btn p" id="accept-btn" type="submit">' + _esc(accept_text) + '</button></form>')
    change_label = 'Request another change' if counter_design else 'Request a change'
    change_action = ('' if preview or accepted else
                     '<button class="btn g" type="button" aria-controls="change" onclick="jumpToChat()">' +
                     change_label + '</button>')
    change_panel = '<div id="change-panel" class="wide" hidden>' + change_form + '</div>'
    change_html = '' if accepted else _change_chat_html(conn, version, preview, counter_design, change_panel)

    # -- schedule: a rough plan from the quote date to each delivery date
    def as_date(value):
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            return None
    phases = selected_terms.get('phases') or ([{'deadline': selected_terms['deadline']}]
                                              if selected_terms.get('deadline') else [])
    deadlines = [d for d in (as_date(p['deadline']) for p in phases) if d]
    start = as_date(offer_row['as_of']) if offer_row and offer_row['as_of'] else None
    if not start:
        stamp = version['published_at'] or version['created_at'] or time.time()
        start = datetime.fromtimestamp(stamp, STUDIO_TZ).date()
    schedule = ''
    if deadlines and deadlines[-1] > start:
        span = (deadlines[-1] - start).days
        first = (deadlines[0] - start).days / span

        def bar(left, width, lite=False):
            return ('<i' + (' class="lite"' if lite else '') + ' style="left:' + format(left * 100, '.1f') +
                    '%;width:' + format(width * 100, '.1f') + '%"></i>')
        rows = [('Shoot day', bar(0.18 * first, 0.06 * first)),
                ('First cuts', bar(0.26 * first, 0.24 * first)),
                ('Revisions', bar(0.52 * first, 0.30 * first + (1 - first) * 0.85))]
        deliveries = ''.join(bar(max(0, (d - start).days / span - 0.05), 0.05, lite=i < len(deadlines) - 1)
                             for i, d in enumerate(deadlines))
        rows.append(('Delivery', deliveries))
        schedule = ('<div class="gantt" aria-label="Rough delivery plan">' +
                    ''.join('<div class="g-row"><span>' + name + '</span><div class="g-bar">' + bars +
                            '</div></div>' for name, bars in rows) +
                    '<div class="g-row"><span></span><div class="g-axis muted"><span>' + when(start.isoformat()) +
                    '</span><span>' + when(deadlines[-1].isoformat()) + '</span></div></div>'
                    '<div class="g-row"><span></span><span class="small muted">A rough plan. I\'ll confirm the shoot day by email.</span></div></div>')

    if accepted and not preview:
        chosen_row = next((r for r in option_rows if r['id'] == accepted), None)
        chosen = db.uj(chosen_row['public_terms_json']) if chosen_row else selected_terms
        confirmed = version['status'] == 'confirmed'
        summary = ''
        if chosen:
            label = _client_label(option_label(chosen_row['option_key'], chosen), counter_design) if chosen_row else ''
            summary = ('<li>' + _esc((label + ': ' if label and chosen.get('phases') else '') + headline(chosen)) + '</li>'
                       '<li>Shoot day: ' + ('confirmed by email' if confirmed else "I'll confirm the date by email") + '</li>'
                       '<li>' + money(chosen['price_sgd']) + ' total</li>')
        body_html = ('<div class="booked">' +
                     _media_html('/stock/bts.mp4', shot='My kit, ready for shoot day', kind='photo', playable=False) +
                     '<h3>' + ("You're booked in." if confirmed else 'Thanks, your choice is in.') + '</h3>'
                     '<span class="small muted">' + ("I've confirmed your booking. See you on shoot day." if confirmed else
                                                     "I'll check the calendar and confirm the booking by email shortly.") + '</span>'
                     '<ul class="ticks">' + summary + '</ul>'
                     '<div class="story"><h4>Before the shoot</h4><span class="small muted">Send any brand notes, logo files '
                     'and reference links. On shoot day, plan for a quiet space while I set up.</span></div>'
                     '<div class="row">' + action + '</div></div>')
    elif counter_design:
        replaced = ''
        if previous_published and previous_published['published_at']:
            replaced = ('This replaces the proposal from ' +
                        datetime.fromtimestamp(previous_published['published_at'], STUDIO_TZ).strftime('%d %b, %H:%M').lstrip('0') + '. ')
        side_media = (featured if picks else
                      _media_html('/stock/street.mp4', shot='From my recent work: launch teaser, then a follow-up series'))
        body_html = ('<div class="replacement">' + _esc(replaced) + _esc(hero_intro) + '</div>'
                     '<div class="row between"><h3>' + _esc(hero_title) + '</h3>' + budget_html + '</div>' +
                     cards_html + '<div class="row">' + action +
                     ('' if preview or accepted or single else '<span class="small muted">Pick a card, then accept.</span>') +
                     '</div>' + ('<div>' + schedule + '</div>' if schedule and selected_terms.get('phases') else '') +
                     '<div class="hero2"><div>' + side_media + '</div><div class="col"><h4>Not quite right?</h4>'
                     '<p class="lede">Ask for another change and I\'ll look at it. Nothing is booked until you accept one option.</p>' +
                     sections + '<div class="row">' + change_action + '</div></div></div>' + change_html)
    else:
        body_html = ('<div class="hero2 top"><div class="col"><h3>' + _esc(hero_title) +
                     '</h3><p class="lede">' + _esc(hero_intro) + '</p>' + sections + cards_html + schedule +
                     '<div class="row">' + action + change_action + '</div>' +
                     '<span class="small muted">Once you accept, I\'ll confirm the booking and the shoot day by email.</span>'
                     '</div>' + media_column + '</div>' + change_html)
    # Confirmed-booking amendments retain their existing renderer and handlers.
    # Library package selectors must obey the same exact preview/release subset.
    allowed_ids = {r['id'] for r in option_rows}
    story_html = re.sub(r'<option value="([^"]+)" data-count="[^"]+">.*?</option>',
                        lambda match: match.group(0) if match.group(1) in allowed_ids else '', story_html)
    # The change chat replaced the "Explore studio work" library on live
    # proposals; a confirmed booking keeps its amendment section.
    library_start = story_html.find('<section><h2>Explore studio work</h2>')
    if version['status'] == 'confirmed':
        body_html += '<div class="library">' + (story_html[library_start:] if library_start >= 0 else story_html) + '</div>'
    if version['status'] == 'confirmed':
        body_html += '<p class="small muted">Booking confirmed</p>'
    client = conn.execute('SELECT client,sample_data FROM deals WHERE id=?', (version['deal_id'],)).fetchone()
    badge = ''  # the sample-data banner is no longer shown to buyers
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{_esc(title)}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&family=Instrument+Sans:wght@400;500;600;700&display=swap">
<style>{PROPOSAL_STYLE}</style></head><body class="site" data-proposal-design="{'counter' if counter_design else 'initial'}">
<header class="site-top"><span class="brand">Alex Tan Productions</span><span class="small muted">Private proposal for {_esc(client['client'])}, version {client_version}</span>{badge}</header>
<main class="site-body">{status_banner}{body_html}<p id="msg" role="status" aria-live="polite"></p></main><script>
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
function toggleChange(btn) {{
  const panel = document.getElementById('change-panel');
  if (!panel) return;
  panel.hidden = !panel.hidden;
  if (btn) btn.textContent = panel.hidden ? 'Open the form' : 'Hide the form';
  if (!panel.hidden) {{ panel.scrollIntoView({{block: 'nearest'}}); const first = document.getElementById('rc-count'); if (first) first.focus(); }}
}}
function jumpToChat() {{
  const box = document.getElementById('change');
  if (!box) return;
  const still = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  box.scrollIntoView({{behavior: still ? 'auto' : 'smooth', block: 'start'}});
  const input = document.getElementById('cc-in');
  if (input && !input.disabled) setTimeout(() => input.focus({{preventScroll: true}}), still ? 0 : 400);
}}
// Change chat: the studio assistant fills the four change terms; the client
// confirms on the card and Send to Alex posts them as a change request.
let ccState = null;
function ccFmt(key, value) {{
  if (value === null || value === undefined) return 'Not given';
  if (key === 'deadline') {{ const d = new Date(value + 'T00:00:00'); return d.toLocaleDateString('en-GB', {{day: 'numeric', month: 'short'}}); }}
  if (key === 'budget') return 'S$' + Number(value).toLocaleString('en-SG', {{maximumFractionDigits: Number.isInteger(Number(value)) ? 0 : 2, minimumFractionDigits: Number.isInteger(Number(value)) ? 0 : 2}});
  return String(value);
}}
function ccRender(st) {{
  ccState = st;
  const box = document.getElementById('change');
  const base = JSON.parse(box.dataset.base || '{{}}');
  const msgs = document.getElementById('cc-msgs');
  const first = msgs.firstElementChild;
  msgs.replaceChildren(first);
  st.turns.forEach(t => {{ const m = document.createElement('div'); m.className = 'm ' + (t.sender === 'client' ? 'me' : 'bot'); m.textContent = t.text; msgs.appendChild(m); }});
  msgs.scrollTop = msgs.scrollHeight;
  const ids = {{count: 'cc-count-v', captions: 'cc-captions', deadline: 'cc-deadline', budget: 'cc-budget'}};
  Object.keys(ids).forEach(k => {{
    const dd = document.getElementById(ids[k]); dd.replaceChildren();
    if (st.changed.includes(k)) {{ const del = document.createElement('del'); del.textContent = ccFmt(k, base[k]); const ins = document.createElement('ins'); ins.textContent = ccFmt(k, st.terms[k]); dd.append(del, ' ', ins); }}
    else dd.textContent = ccFmt(k, st.terms[k]);
  }});
  const n = st.changed.length;
  document.getElementById('cc-count').textContent = n ? n + ' change' + (n === 1 ? '' : 's') : 'Nothing yet';
  const said = document.getElementById('cc-said');
  said.hidden = !st.client_words; said.textContent = st.client_words ? 'You said: "' + st.client_words + '"' : '';
  document.getElementById('cc-send').disabled = !n;
  document.getElementById('cc-reset').hidden = !n;
}}
async function ccPost(payload) {{
  const res = await fetch(location.pathname + '/change-chat', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(payload)}});
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || 'unavailable');
  ccRender(data);
}}
(function () {{
  const form = document.getElementById('cc-form');
  if (!form) return;
  form.addEventListener('submit', async function (e) {{
    e.preventDefault();
    const input = document.getElementById('cc-in'), btn = form.querySelector('button');
    const text = input.value.trim();
    if (!text || btn.disabled) return;
    const msgs = document.getElementById('cc-msgs');
    const mine = document.createElement('div'); mine.className = 'm me'; mine.textContent = text; msgs.appendChild(mine);
    const wait = document.createElement('div'); wait.className = 'm bot wait'; wait.textContent = 'Thinking…'; msgs.appendChild(wait);
    msgs.scrollTop = msgs.scrollHeight;
    input.value = ''; btn.disabled = true; input.disabled = true;
    try {{ await ccPost({{text: text}}); }}
    catch (err) {{ mine.remove(); wait.remove(); input.value = text; setMsg('The assistant could not reply just now. Try again, or use the form below.'); }}
    finally {{ btn.disabled = false; input.disabled = false; input.focus(); }}
  }});
}})();
async function resetChat() {{
  try {{ await ccPost({{reset: true}}); }} catch (e) {{ setMsg('Could not start over. Try again.'); }}
}}
async function sendChange(btn) {{
  if (!ccState || !ccState.changed.length || btn.disabled) return;
  btn.disabled = true; btn.textContent = 'Sending…';
  const t = ccState.terms;
  const terms = {{count: t.count, captions: t.captions, deadline: t.deadline, budget: t.budget}};
  const body = ccState.client_words;
  try {{
    const res = await fetch(location.pathname + '/request-change', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{body: body, terms: terms, request_id: stableRequestId({{body: body, terms: terms}})}})}});
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'unknown error');
    document.getElementById('cc-card').classList.add('done');
    document.getElementById('cc-in').disabled = true;
    document.querySelector('#cc-form button').disabled = true;
  }} catch (e) {{
    btn.disabled = false; btn.textContent = 'Send to Alex';
    setMsg('Could not send your change: ' + e.message + '. Sending again is safe.');
  }}
}}
document.querySelectorAll('input[name=proposal-option]').forEach(function (input) {{
  input.addEventListener('change', function () {{
    const btn = document.getElementById('accept-btn');
    if (btn && document.body.dataset.proposalDesign === 'counter') btn.textContent = input.dataset.accept;
  }});
}});
// Wireframe media: tap to play or pause, live timecode and scrubber.
(function () {{
  const fmt = n => {{ n = Math.trunc(n || 0); return String(Math.trunc(n / 60)).padStart(2, '0') + ':' + String(n % 60).padStart(2, '0'); }};
  document.querySelectorAll('.media[data-play]').forEach(function (m) {{
    const v = m.querySelector('video'); if (!v) return;
    const tc = m.querySelector('.tc'), sc = m.querySelector('.scrub i');
    const upd = () => {{ if (tc) tc.textContent = fmt(v.currentTime) + ' / ' + fmt(v.duration); if (sc && v.duration) sc.style.width = (v.currentTime / v.duration * 100) + '%'; }};
    v.addEventListener('loadedmetadata', upd); v.addEventListener('timeupdate', upd);
    v.addEventListener('play', () => {{ m.classList.add('playing'); m.setAttribute('aria-label', 'Pause video'); }});
    v.addEventListener('pause', () => {{ m.classList.remove('playing'); m.setAttribute('aria-label', 'Play video'); }});
    const toggle = () => {{ if (v.paused) v.play().catch(() => {{}}); else v.pause(); }};
    m.addEventListener('click', toggle);
    m.addEventListener('keydown', e => {{ if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); toggle(); }} }});
  }});
}})();
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
      ? 'Thank you — your acceptance is recorded. I\\'ll confirm the booking by email.'
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
      ? 'Your change request was sent. I\\'ll review it and reply.'
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


def refresh_stale_estimates(conn):
    """Re-run every open estimate the calendar has moved under.

    A deal's latest, not-yet-sent version whose evaluation was made against an
    older calendar revision is re-estimated on the current calendar, exactly as
    the Re-run button did. Sent, booked, rejected and test deals are left alone.
    Returns how many were re-run.
    """
    rev = capacity_panel(conn)["revision"]
    rows = conn.execute(
        "SELECT d.id AS deal_id, d.source, d.source_id, d.raw_inquiry, v.offer_id"
        " FROM deals d JOIN proposal_versions v ON v.deal_id=d.id"
        " AND v.version_no=(SELECT MAX(version_no) FROM proposal_versions WHERE deal_id=d.id)"
        " JOIN evaluations e ON e.offer_id=v.offer_id"
        " WHERE v.status IN ('draft','checked','blocked','evaluated')"
        " AND d.status NOT IN ('closed','rejected','confirmed')"
        " AND e.capacity_revision IS NOT NULL AND e.capacity_revision != ?"
        # a newer client change still waiting to be priced is handled by its own flow
        " AND NOT EXISTS (SELECT 1 FROM offers o2 WHERE o2.deal_id=d.id AND o2.round>"
        "  (SELECT round FROM offers WHERE id=v.offer_id)"
        "  AND NOT EXISTS (SELECT 1 FROM evaluations e2 WHERE e2.offer_id=o2.id))",
        (rev,)).fetchall()
    done = 0
    for r in rows:
        if deal_logic.is_test_deal(r["source"], r["source_id"], r["raw_inquiry"]):
            continue
        try:
            offer_id = core.reevaluate(conn, r["offer_id"])
            core.evaluate(conn, r["deal_id"], offer_id)
            done += 1
        except (ValueError, KeyError) as exc:
            conn.rollback()
            print(f"auto re-estimate skipped for {r['deal_id']}: {exc}", flush=True)
    return done


def refresh_estimate_for_send(conn, version_id, force=False):
    """Before a proposal goes out: make sure it rests on the current calendar.

    If the version's estimate was made on an older calendar (or the caller saw
    the send refused for capacity), re-run it now on the same terms and return
    the fresh version for the founder to review. Nothing is sent here; the
    approval gate and the outbound check still run on the fresh version.
    """
    v = conn.execute("SELECT * FROM proposal_versions WHERE id=?", (version_id,)).fetchone()
    if v is None:
        return {"ok": False, "error": "version_not_found"}
    e = conn.execute("SELECT capacity_revision FROM evaluations WHERE offer_id=?", (v["offer_id"],)).fetchone()
    stale = bool(e and e["capacity_revision"] is not None
                 and e["capacity_revision"] != core.cap.ledger_revision(conn))
    if not (stale or force) or v["status"] not in ("draft", "checked", "blocked", "evaluated"):
        return {"ok": True, "rerun": False, "version_id": version_id}
    offer_id = core.reevaluate(conn, v["offer_id"])
    _eval_id, new_version = core.evaluate(conn, v["deal_id"], offer_id)
    return {"ok": True, "rerun": True, "version_id": new_version}


def capacity_panel(conn):
    """Private founder panel: aggregate weekly planning capacity (sample only)."""
    led = core.cap.ledger(conn)
    calendar = {row['week_ending']: row for row in core.cap.calendar_data(conn)['calendar_weeks']}
    weeks = [{"week": w, "budget_hours": s["budget"] / 100,
              "allocated_hours": s["allocated"] / 100,
              "remaining_hours": s["residual"] / 100,
              "working_hours": float(calendar[w]['working_hours'])}
             for w, s in sorted(led.items())]
    jobs = [dict(r) for r in conn.execute(
        "SELECT d.client, d.id AS deal_id, a.week_ending AS week, "
        "a.planning_hundredths / 100.0 AS hours FROM allocations a "
        "JOIN reservations r ON r.id=a.reservation_id "
        "JOIN deals d ON d.id=r.deal_id WHERE r.status='active' "
        "ORDER BY d.client, a.week_ending").fetchall()]
    setting = conn.execute('SELECT working_hours FROM calendar_settings WHERE id=1').fetchone()
    blocks = [dict(row) for row in conn.execute(
        'SELECT id, week_ending AS week, hours, reason FROM calendar_blocks ORDER BY week_ending, created_at')]
    return {"jobs": jobs, "blocks": blocks, "working_hours": setting[0] if setting else None,
            "label": "Aggregate planning capacity (sample booking calendar —"
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


STATE_STEP_LIMIT = 50


def chat_brief_facts(conn, deal_id):
    """Latest buyer facts from the studio chat linked to a deal (read-only)."""
    try:
        rows = conn.execute(
            "SELECT f.id, f.subject, f.value_json, f.status, f.supersedes FROM inquiry_facts f"
            " JOIN inquiries i ON i.id=f.inquiry_id WHERE i.deal_id=? ORDER BY f.created_at, f.rowid",
            (deal_id,)).fetchall()
    except sqlite3.OperationalError:
        return []
    gone = {r["supersedes"] for r in rows if r["supersedes"]}
    latest = {}
    for r in rows:
        if r["id"] in gone:
            continue
        try:
            value = json.loads(r["value_json"])
        except ValueError:
            continue
        latest[r["subject"]] = {"subject": r["subject"], "value": value, "status": r["status"]}
    return list(latest.values())


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
            prior_snapshot = db.uj(eval_row['capacity_snapshot_json']) if eval_row and eval_row['capacity_snapshot_json'] else {}
            prior_weeks = {w['week']: w for w in prior_snapshot.get('weeks', [])}
            capacity_changes = []
            if capacity_stale:
                for week in panel['weeks']:
                    previous = prior_weeks.get(week['week'])
                    if previous is not None and previous['residual'] != round(week['remaining_hours'] * 100):
                        capacity_changes.append({'week': week['week'], 'before': previous['residual'] / 100,
                                                 'after': week['remaining_hours']})
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
                "offer_id": v["offer_id"],
                "outbound_status": v["outbound_status"], "draft": v["draft_body"],
                "page": db.uj(v["page_json"]) if v["page_json"] else None,
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
                                 "label": option_label(r["option_key"], db.uj(r["public_terms_json"]))}
                                for r in core.options_of(conn, v)],
                "comparables": (result or {}).get("comparables"),
                "option_metrics": (result or {}).get("option_metrics"),
                **__import__('delivery').state(conn, v),
                "capacity_stale": capacity_stale,
                "capacity_changes": capacity_changes,
                "impacts": [db.uj(row[0]) for row in conn.execute(
                    "SELECT details FROM capacity_impacts WHERE version_id=? ORDER BY rowid DESC", (v["id"],))],
                "pricing_evidence": (result or {}).get("historical_pricing"),
                "comparison": _offer_comparison(offer_map, version_offer, v),
                "client_url": (v["client_url_private"] if "client_url_private" in v.keys()
                               and v["status"] in ("published", "confirmed") else None),
                "unpriced": ({"missing": (result or {}).get("missing_fields") or [],
                              "clarifications": (result or {}).get("clarifications") or []}
                             if eval_row and eval_row["verdict"] == "clarify" else None),
                "check_violations": [
                    {**item, "message": deal_logic.explain_violation(item)}
                    for item in (db.uj(v["check_violations_json"])
                                 if "check_violations_json" in v.keys() and v["check_violations_json"]
                                 else [])],
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
            needs_info = core.needs_information_of(conn, deal["id"])
        deal_phase = deal_logic.phase(deal["status"], versions, offers)
        latest_version = versions[-1] if versions else None
        fresh_reason = deal_logic.fresh_estimate_reason(latest_version, approvals)
        if latest_version:
            latest_approvals = [dict(r) for r in conn.execute('SELECT action,note FROM approvals WHERE version_id=?', (latest_version['version_id'],))]
            fresh_reason = deal_logic.fresh_estimate_reason(latest_version, latest_approvals)
        if deal_phase not in deal_logic.REVIEW_PHASES:
            for version in versions:
                version['historical_verdict'] = version['verdict']
                version['historical_reason_codes'] = version['reason_codes']
                if deal_phase in ('booked', 'rejected', 'closed'):
                    version['verdict'] = deal_phase
                    version['reason_codes'] = []
                    version['capacity_stale'] = False
                version['active_warnings'] = []
                version['estimate_checks_applicable'] = False
        else:
            for version in versions:
                version['active_warnings'] = version['reason_codes']
                version['estimate_checks_applicable'] = version is latest_version
        # Latest output of each kind per deal, and a capped recent timeline;
        # totals say how much history exists (nothing is deleted).
        latest_outputs = {}
        for output in agent_outputs:
            latest_outputs[output["kind"]] = output
        deals.append({"deal_id": deal["id"], "source": deal["source"],
                      "source_id": deal["source_id"], "client": deal["client"],
                      "status": deal["status"], "raw_inquiry": deal["raw_inquiry"],
                      "created_at": deal["created_at"],
                      "phase": deal_phase, "phase_label": deal_logic.PHASE_LABELS[deal_phase],
                      "show_estimate_checks": deal_phase in deal_logic.REVIEW_PHASES,
                      "needs_fresh_estimate": bool(fresh_reason) and deal_phase in deal_logic.REVIEW_PHASES,
                      "fresh_estimate_reason": fresh_reason if deal_phase in deal_logic.REVIEW_PHASES else None,
                      "estimate_required": deal_phase == "needs_estimate",
                      "client_name_needed": deal_logic.looks_like_description(deal["client"]),
                       "scope": 'test' if deal_logic.is_test_deal(deal['source'], deal['source_id'], deal['raw_inquiry']) else 'production',
                       "missing_items": (needs_info or {}).get('missing_fields', []),
                       "is_sample": (deal["source_id"] or "").startswith("SAMPLE-"),
                      "brief_facts": chat_brief_facts(conn, deal["id"]),
                      "is_test": deal_logic.is_test_deal(deal["source"], deal["source_id"],
                                                         deal["raw_inquiry"]),
                      "versions": versions, "offers": offers,
                      "steps": steps[-STATE_STEP_LIMIT:], "steps_total": len(steps),
                      "agent_outputs": sorted(latest_outputs.values(), key=lambda o: o["created_at"] or 0),
                      "agent_outputs_total": len(agent_outputs),
                      "messages": messages, "approvals": approvals,
                      "queued_clarifications": queued, "needs_information": needs_info,
                      "similar_work": similar_work(conn, deal, versions)})
    inbox = [{"source_id": r["source_id"], "fields": db.uj(r["fields_json"]),
              "received_at": r["received_at"], "claimed": bool(r["deal_id"])}
             for r in conn.execute("SELECT i.*, d.id AS deal_id FROM inbox i LEFT JOIN deals d ON d.source=i.source AND d.source_id=i.source_id ORDER BY i.received_at DESC").fetchall()]
    portfolio_items = [dict(r) for r in conn.execute(
        "SELECT id, project_id, title, description, tags, mime, shared FROM portfolio_assets ORDER BY title")]
    for item in portfolio_items:
        project = conn.execute("SELECT format, deliverables, shoot_days FROM seed_past_projects"
                               " WHERE project_id=?", (item["project_id"],)).fetchone() if _has_table(conn, "seed_past_projects") else None
        item.update(format=project["format"] if project else None,
                    deliverables=project["deliverables"] if project else None,
                    shoot_days=project["shoot_days"] if project else None)
    return {"badge": SAMPLE_BADGE, "capacity": panel, "deals": deals, "inbox": inbox,
            "price_list": price_list.items(conn), "promo_template": price_list.template_settings(conn),
            "portfolio": portfolio_items}


def _has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


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
.stage-steps .status-chip { font-size: 12px; padding: 0 8px; }
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
.queue-deal:hover, .queue-deal.is-selected { background: var(--neutral-bg); border-color: var(--accent); color: var(--text); }.queue-line { display: flex; gap: 8px; justify-content: space-between; align-items: start; font-size: 13px; }.queue-meta { display: block; color: var(--muted); font-size: 12px; margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.queue-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--warn); margin-top: 5px; flex: 0 0 auto; }.queue-dot.blocked { background: var(--danger); }.queue-dot.approved, .queue-dot.booked { background: var(--ok); }
.sidebar-sync { color: var(--muted); font-size: 12px; line-height: 1.5; border-top: 1px solid var(--border); margin: auto 8px 0; padding-top: 14px; }
.workspace-main { min-width: 0; padding: 24px 30px 60px; }.workspace-toolbar { display: flex; justify-content: space-between; gap: 16px; align-items: center; margin: 0 auto 16px; max-width: 900px; }.workspace-toolbar .ctl { margin: 0; }
.operator-tools { max-width: 900px; margin: 32px auto 0; border-top: 1px solid var(--border); padding-top: 12px; }.operator-tools > summary { color: var(--muted); cursor: pointer; font-size: 13px; font-weight: 650; }.operator-tools fieldset { box-shadow: none; }
.cal-head { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; } .cal-head h1 { font-size:26px; margin:0; } .cal-layout { display:grid; grid-template-columns:minmax(0,1fr) 300px; gap:16px; } .cal-panel-head { display:flex; justify-content:space-between; } .tool-group {margin:24px 0} .tool-group h5 {color:var(--b-mute);font-size:12px;letter-spacing:.06em} .tool-grid {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px} .tool-card {text-decoration:none;text-align:left;color:var(--b-ink);background:#fff;border:1px solid var(--b-line);border-radius:10px;padding:16px;display:grid;gap:8px;min-height:150px;font-size:14px}.tool-card span {color:var(--b-mute);font-weight:400}.tool-card strong {color:var(--b-teal)} .calendar-week { display:grid; grid-template-columns:96px minmax(0,1fr) 130px; gap:12px; align-items:center; padding:4px 0; } .cal-layout label {display:grid;gap:4px;font-size:13px;font-weight:600;} .cal-layout input,.cal-layout select {width:100%;border:1px solid var(--b-line);border-radius:6px;padding:8px 10px;background:#fff;} .cal-block-row {display:flex;justify-content:space-between;gap:10px;} .calendar-link {border:0;background:none;color:var(--b-teal);padding:2px 0;text-decoration:underline;} .cal-red {color:var(--bad)} .cal-legend {display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--b-mute)} .cal-legend i {display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:5px} .cal-table {overflow-x:auto} .cal-table td {text-align:right}
.calendar-bar { height:34px; position:relative; border:1px solid var(--b-line); border-radius:6px; overflow:hidden; background:#fff; } .calendar-bar.is-over {border-color:var(--bad)} .cal-segment {position:absolute;top:0;bottom:0;display:flex;align-items:center;padding:0 8px;font-size:12px;white-space:nowrap;overflow:hidden;color:#17201d;} .cal-blocked {position:absolute;top:0;bottom:0;background:repeating-linear-gradient(45deg,#eef1ef 0 5px,#dde2df 5px 10px);color:var(--b-mute);display:flex;align-items:center;padding:0 8px;font-size:12px;white-space:nowrap;overflow:hidden;} .legend-blocked {background:repeating-linear-gradient(45deg,#eef1ef 0 3px,#dde2df 3px 6px);border:1px dashed #5d6a65;} .cal-table th,.cal-table td {border:0;border-bottom:1px solid var(--b-line);background:transparent;} .cal-over {position:absolute;top:0;bottom:0;z-index:2;background:repeating-linear-gradient(45deg,var(--bad) 0 4px,transparent 4px 8px);opacity:.75;} .cal-limit {position:absolute;top:0;bottom:0;width:0;border-left:2px dashed #17201d;z-index:3;}
#section-view .panel { margin:16px 0; }
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
/* Founder dashboard: values copied from the Alex Tan Productions wireframe (Scenes 3 and 5). */
[hidden] { display: none !important; }
:root { --b-bg: #f2f4f3; --b-panel: #ffffff; --b-ink: #17201d; --b-mute: #5d6a65; --b-line: #dde2df;
  --b-teal: #0e6b5b; --b-teal-tint: #dff0eb;
  --ok: #2c7a3f; --ok-tint: #e4f3e7; --warn: #a2620a; --warn-tint: #fbf0dc; --bad: #b3261e; --bad-tint: #fbe4e2;
  --ui: "Instrument Sans", "Helvetica Neue", Arial, sans-serif;
  /* shared design-system tokens re-pointed at the wireframe palette */
  --bg: var(--b-bg); --surface: #fff; --border: var(--b-line); --border-strong: #c9d0d7; --text: var(--b-ink);
  --muted: var(--b-mute); --accent: var(--b-teal); --accent-strong: #0f574f; --accent-soft: var(--b-teal-tint);
  --danger: var(--bad); --danger-bg: var(--bad-tint); --danger-border: #f1c0bb; --ok-bg: var(--ok-tint);
  --ok-border: #bfdcc6; --warn-bg: var(--warn-tint); --warn-border: #ecd5a6; --neutral-bg: #eef1f4;
  --neutral-text: #4b5663; --radius: 10px; --radius-sm: 8px; --shadow: none; --font: var(--ui); }
body { background: var(--b-bg); color: var(--b-ink); font: 14px/1.5 var(--ui); -webkit-font-smoothing: antialiased; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
button { font: 600 13.5px var(--ui); border-radius: 7px; padding: 10px 16px; }
button.ghost { background: #fff; color: var(--b-ink); border: 1px solid var(--b-line); }
button.ghost:hover { background: #f6f2ee; border-color: var(--b-line); color: var(--b-ink); }
.topnav { grid-column: 1 / -1; display:flex; align-items:center; gap:12px; padding:12px 0; margin:0 24px; border-bottom:1px solid var(--b-line); background:var(--b-bg); }
.topnav button { background:transparent; color:var(--b-mute); border:0; } .topnav button[aria-current="page"] { background:var(--b-teal-tint);color:var(--b-teal); } .navlogo {display:flex;gap:8px;align-items:center;font-size:16px} .navlogo i {width:22px;height:22px;background:url(data:image/svg+xml,%3Csvg%20xmlns=%27http://www.w3.org/2000/svg%27%20viewBox=%270%200%2024%2024%27%3E%3Cpath%20fill=%27%231e9e5a%27%20d=%27M22%202%202%209.5l8.2%203.3L13.5%2021z%27/%3E%3Cpath%20fill=%27none%27%20stroke=%27%23fff%27%20stroke-width=%271.4%27%20stroke-linecap=%27round%27%20d=%27M22%202%2010.2%2012.8%27/%3E%3C/svg%3E) center/contain no-repeat} .rail-toggle {display:none;}
.topnav span { margin-left:auto; color:var(--b-mute); font-size:12px; }
.founder-shell.dash { max-width: none; min-height: 100vh; margin: 0; padding: 0; display: grid;
  grid-template-columns: 272px minmax(0, 1fr); column-gap:24px; padding:0 24px; background: var(--b-bg); color: var(--b-ink); }
.side { background: transparent; border-right: 0; padding: 20px 0; display: grid;
  align-content: start; gap: 10px; }
.side .logo { font: 700 15px var(--ui); display: flex; align-items: center; gap: 8px; }
.side .logo i { width: 20px; height: 20px; background: url(data:image/svg+xml,%3Csvg%20xmlns=%27http://www.w3.org/2000/svg%27%20viewBox=%270%200%2024%2024%27%3E%3Cpath%20fill=%27%231e9e5a%27%20d=%27M22%202%202%209.5l8.2%203.3L13.5%2021z%27/%3E%3Cpath%20fill=%27none%27%20stroke=%27%23fff%27%20stroke-width=%271.4%27%20stroke-linecap=%27round%27%20d=%27M22%202%2010.2%2012.8%27/%3E%3C/svg%3E) center/contain no-repeat; display: inline-block; }
.side h6 { margin: 8px 0 0; font: 600 12px var(--ui); color: var(--b-mute); }
.deal-queue { display: grid; gap: 10px; margin: 0; }
button.rail-deal { border: 1px solid var(--b-line); border-radius: 8px; padding: 9px 10px; display: grid; gap: 3px;
  font: 400 12.5px/1.45 var(--ui); background: #fff; color: var(--b-ink); text-align: left; width: 100%; }
button.rail-deal:hover { background: #fff; border-color: var(--b-teal); color: var(--b-ink); }
button.rail-deal.sel { border-color: var(--b-teal); box-shadow: inset 3px 0 0 var(--b-teal); }
button.rail-deal b { font-size: 13px; }
.dmeta { color: var(--b-mute); }
.st { display: inline-block; font-size: 12px; font-weight: 600; padding: 1px 7px; border-radius: 999px; justify-self: start; white-space: nowrap; }
.st.rev { background: var(--warn-tint); color: var(--warn); }
.st.ok { background: var(--ok-tint); color: var(--ok); }
.st.bad { background: var(--bad-tint); color: var(--bad); }
.st.new { background: var(--b-teal-tint); color: var(--b-teal); }
.st.mute { background: var(--neutral-bg); color: var(--b-mute); }
.said { margin: 0; border: 1px solid var(--b-line); border-radius: 8px; padding: 10px 12px; font-size: 12.5px;
  line-height: 1.55; background: #fbfcfd; overflow-wrap: anywhere; }
.said small { display: block; margin-top: 6px; font-size: 12px; color: var(--b-mute); }
.week { display: grid; gap: 6px; font-size: 12px; color: var(--b-mute); }
.side-foot { margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--b-line); display: grid; gap: 8px;
  font-size: 12px; color: var(--b-mute); }
.side-foot summary { cursor: pointer; font-weight: 600; }
.side-foot nav { display: grid; gap: 4px; margin-top: 6px; }
.side-foot a { color: var(--b-teal); text-decoration: none; }
.side-foot a:hover { text-decoration: underline; }
.main { padding: 20px 0; min-width: 0; }
.cal-head { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; } .cal-head h1 { font-size:26px; margin:0; } .cal-layout { display:grid; grid-template-columns:minmax(0,1fr) 300px; gap:16px; } .cal-panel-head { display:flex; justify-content:space-between; } .tool-group {margin:24px 0} .tool-group h5 {color:var(--b-mute);font-size:12px;letter-spacing:.06em} .tool-grid {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px} .tool-card {text-decoration:none;text-align:left;color:var(--b-ink);background:#fff;border:1px solid var(--b-line);border-radius:10px;padding:16px;display:grid;gap:8px;min-height:150px;font-size:14px}.tool-card span {color:var(--b-mute);font-weight:400}.tool-card strong {color:var(--b-teal)} .calendar-week { display:grid; grid-template-columns:96px minmax(0,1fr) 130px; gap:12px; align-items:center; padding:4px 0; } .cal-layout label {display:grid;gap:4px;font-size:13px;font-weight:600;} .cal-layout input,.cal-layout select {width:100%;border:1px solid var(--b-line);border-radius:6px;padding:8px 10px;background:#fff;} .cal-block-row {display:flex;justify-content:space-between;gap:10px;} .calendar-link {border:0;background:none;color:var(--b-teal);padding:2px 0;text-decoration:underline;} .cal-red {color:var(--bad)} .cal-legend {display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--b-mute)} .cal-legend i {display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:5px} .cal-table {overflow-x:auto} .cal-table td {text-align:right}
.calendar-bar { height:34px; position:relative; border:1px solid var(--b-line); border-radius:6px; overflow:hidden; background:#fff; } .calendar-bar.is-over {border-color:var(--bad)} .cal-segment {position:absolute;top:0;bottom:0;display:flex;align-items:center;padding:0 8px;font-size:12px;white-space:nowrap;overflow:hidden;color:#17201d;} .cal-blocked {position:absolute;top:0;bottom:0;background:repeating-linear-gradient(45deg,#eef1ef 0 5px,#dde2df 5px 10px);color:var(--b-mute);display:flex;align-items:center;padding:0 8px;font-size:12px;white-space:nowrap;overflow:hidden;} .legend-blocked {background:repeating-linear-gradient(45deg,#eef1ef 0 3px,#dde2df 3px 6px);border:1px dashed #5d6a65;} .cal-table th,.cal-table td {border:0;border-bottom:1px solid var(--b-line);background:transparent;} .cal-over {position:absolute;top:0;bottom:0;z-index:2;background:repeating-linear-gradient(45deg,var(--bad) 0 4px,transparent 4px 8px);opacity:.75;} .cal-limit {position:absolute;top:0;bottom:0;width:0;border-left:2px dashed #17201d;z-index:3;}
#section-view .panel { margin:16px 0; }
.workbench { max-width: none; margin: 0; display: grid; gap: 16px; }
.main-head { display: flex; flex-wrap: wrap; gap: 10px; align-items: baseline; justify-content: space-between; }
.main-head h1 { margin: 0; font: 600 26px var(--ui); }
.main-head .st { font-size: 12px; }
.panel { background: var(--b-panel); border: 1px solid var(--b-line); border-radius: 10px; padding: 14px;
  display: grid; gap: 10px; min-width: 0; align-content: start; }
.panel h5 { margin: 0; font: 600 13px var(--ui); color: var(--b-mute); }
.figs { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.fig { display: grid; gap: 2px; }
.fig small { font-size: 12px; color: var(--b-mute); }
.fig b { font: 600 18px var(--ui); font-variant-numeric: tabular-nums; }
.fig.priv b::after { content: " private"; font: 600 12px var(--ui); color: var(--b-mute); vertical-align: middle; }
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
.release-confirm {border:1px solid var(--b-teal);border-radius:8px;background:var(--b-teal-tint);padding:14px;white-space:pre-wrap;overflow-wrap:anywhere;} .release-panel { margin: 0 0 16px; border-radius: 10px; background: #0f574f; }
th { background: #eef1f4; }
.quote { border-left-color: var(--b-teal); background: var(--b-teal-tint); font-style: normal; color: var(--b-ink); }
@media (max-width: 900px) {
  .founder-shell.dash { grid-template-columns: 1fr; }
  .side { display:none; } .side.rail-open {display:grid;} .rail-toggle {display:inline-block;} .founder-shell.dash {padding:0 16px;}
  .cal-layout,.tool-grid {grid-template-columns:1fr;} .calendar-week {grid-template-columns:78px minmax(0,1fr);} .calendar-week > div:last-child {grid-column:2;}
  .topnav { flex-wrap:wrap; padding:10px; } .topnav span { width:100%; margin:0; }
  .figs { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .two, .warns, .warns.reason-links { grid-template-columns: 1fr; }
  .orow { grid-template-columns: 1fr auto; }
  .orow .m { grid-column: 1 / -1; order: 3; }
}
.preview-sheet { position:fixed; right:0; top:0; bottom:0; width:min(520px,100%); z-index:30; overflow:auto; background:#fff; border-left:1px solid var(--b-line); box-shadow:-8px 0 24px rgba(0,0,0,.18); padding:20px; display:grid; gap:14px; align-content:start; }
.preview-segment,.preview-key {background:repeating-linear-gradient(45deg,#0e6b5b 0 4px,#53a892 4px 8px);color:white} .preview-sheet .calendar-week {grid-template-columns:64px minmax(0,1fr) 85px;font-size:12px}.preview-sheet .preview-head {display:flex;justify-content:space-between;gap:12px;align-items:start}.preview-sheet .preview-head h2 {font-size:18px;margin:0}.preview-sheet .cal-table td {text-align:right}.preview-sheet .cal-table th {background:transparent;border:0;border-bottom:1px solid var(--b-line)}
/* Calendar-only header styles preserve the existing Deals layout. */
.topnav .whats-new { display:none; }
/* Calendar layout mirrors the founder wireframe at desktop and mobile sizes. */
body[data-dashboard-section="calendar"] .founder-shell.dash { grid-template-rows:68px minmax(0,1fr); align-content:start; }
body[data-dashboard-section="calendar"] .founder-shell.dash .topnav { margin:0; min-width:0; padding:12px 0; gap:4px; }
body[data-dashboard-section="calendar"] .founder-shell.dash .topnav .navlogo { margin-right:12px; }
body[data-dashboard-section="calendar"] .founder-shell.dash .topnav button[data-section] { padding:7px 12px; min-height:36px; font-size:14px; font-weight:500; border-radius:6px; }
body[data-dashboard-section="calendar"] .founder-shell.dash .topnav button[data-section][aria-current="page"] { font-weight:600; }
body[data-dashboard-section="calendar"] .founder-shell.dash .topnav .sample-pill { margin-left:auto; border:1px solid var(--b-line); border-radius:999px; padding:3px 10px; background:#fff; font-size:12px; }
body[data-dashboard-section="calendar"] .founder-shell.dash .topnav .whats-new { display:inline-flex; margin-left:12px; border:1px solid var(--b-line); background:#fff; color:var(--b-ink); border-radius:6px; padding:8px 14px; min-height:36px; font-size:13px; }
.cal-head { display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:12px; }
.cal-head h1 { font:600 26px/1.2 var(--ui); letter-spacing:0; margin:0; }
.cal-head .hint { margin:4px 0 0; font-size:13px; }
.cal-navigation { display:flex; align-items:center; gap:8px; }
.cal-navigation .bbtn { min-height:30px; padding:5px 10px; font-size:13px; }
.cal-layout { display:grid; grid-template-columns:minmax(0,1fr) 300px; gap:16px; align-items:start; margin-top:16px; }
.cal-main,.cal-sidebar { display:grid; gap:16px; min-width:0; align-content:start; }
#section-view .cal-panel { margin:0; padding:16px; border-radius:10px; box-shadow:0 1px 2px rgba(20,30,26,.06); gap:0; }
.cal-panel h3 { font:600 13px/1.5 var(--ui); color:var(--b-mute); margin:0 0 10px; }
.cal-panel-head { display:flex; justify-content:space-between; gap:8px; align-items:center; }
.cal-note { font:400 12px/1.5 var(--ui); color:var(--b-mute); margin:0 0 14px; }
.calendar-week { display:grid; grid-template-columns:96px minmax(0,1fr) 130px; gap:12px; align-items:center; padding:4px 0; min-height:55px; }
.cal-date b,.cal-date span { display:block; }
.cal-date b { font-weight:600; }
.cal-date span { color:var(--b-mute); font-size:12px; }
.cal-remaining { font-size:13px; display:grid; gap:2px; }
.cal-remaining .hint { line-height:1.4; }
.calendar-bar { position:relative; height:34px; }
.cal-unavailable { position:absolute;top:0;bottom:0;display:flex;align-items:center;padding:0 8px;font-size:12px;white-space:nowrap;overflow:hidden;color:#5d6a65;background:#e9eeeb; }
.cal-blocked { background:repeating-linear-gradient(45deg,#eef1ef 0 5px,#dde2df 5px 10px); }
.cal-legend { display:flex;gap:12px;flex-wrap:wrap;color:var(--b-mute);font-size:12px;margin-top:10px; }
.cal-legend i { width:12px;height:12px;display:inline-block;border-radius:3px;margin-right:5px;vertical-align:-2px; }
.legend-unavailable { background:#e9eeeb;border:1px solid #ccd4cf; }
.cal-table { overflow-x:auto; }
.cal-table table { border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums; }
.cal-table th,.cal-table td { padding:7px 8px;border:0;border-bottom:1px solid var(--b-line);background:transparent; }
.cal-table th { color:var(--b-mute);font-size:12px;font-weight:600; }
.cal-table thead th:not(:first-child),.cal-table td { text-align:right; }
.cal-table tbody th { text-align:left; }
.cal-sidebar label { display:grid;gap:4px;font-size:13px;font-weight:600;margin:0 0 10px; }
.cal-sidebar input,.cal-sidebar select { border:1px solid var(--b-line);border-radius:6px;padding:8px 10px;background:#fff;min-width:0;min-height:37px;font:inherit; }
.cal-sidebar .bbtn.sm { padding:5px 10px;min-height:30px;font-size:13px; }
.cal-block-list { display:grid;gap:8px;margin-top:12px; }
.cal-block-list:empty { display:none; }
.cal-block-row { display:flex;justify-content:space-between;gap:12px;align-items:center; }
.cal-sidebar .calendar-link { font-size:13px; }
.cal-toast { position:fixed;left:50%;bottom:16px;transform:translateX(-50%);background:#17201d;color:#f2f4f3;padding:10px 16px;border-radius:8px;z-index:40;max-width:calc(100% - 32px);font-size:13px;box-shadow:0 2px 8px #0002; }
@media (max-width:900px) {
 body[data-dashboard-section="calendar"] .founder-shell.dash { grid-template-rows:auto auto 1fr; }
 body[data-dashboard-section="calendar"] .founder-shell.dash .topnav { flex-wrap:wrap; }
 body[data-dashboard-section="calendar"] .founder-shell.dash .topnav .sample-pill { margin-left:0; }
 .cal-layout { grid-template-columns:minmax(0,1fr); }
 .calendar-week { grid-template-columns:78px minmax(0,1fr); }
 .cal-remaining { grid-column:2; }
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
  if (v && v.capacity_stale && ['draft','checked','blocked','evaluated'].includes(v.status)) return ['Needs fresh estimate', 'rev'];
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
  return checkRow('wait', 'Safe to send', 'Checked when you press Approve and send.')
    + checkRow('wait', 'Price matches the estimate', 'Checked when you press Approve and send.');
}

const NUM_WORDS = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'];
const offeredSel = {};
const cleanDrafts = {};
const similarPick = {};
function oneDecimal(v) { return String(Number(Number(v).toFixed(1))); }
function verdictChip(d, v) {
  if (v && (v.confirmed || ['confirmed', 'rejected'].includes(v.status)) || d.status === 'closed') {
    const [label, tone] = dealStage(d);
    return '<span class="st ' + tone + '">' + esc(label) + '</span>';
  }
  if (v && v.verdict === 'accept') return '<span class="st ok">Fits as asked</span>';
  if (v && v.verdict === 'counter') return '<span class="st bad">Doesn’t fit as asked</span>';
  if (v && v.verdict) return '<span class="st rev">Needs your estimate</span>';
  const [label, tone] = dealStage(d);
  return '<span class="st ' + tone + '">' + esc(label) + '</span>';
}
function railMeta(d) {
  const v = latestVersion(d);
  const stamp = lastActivity(d) || d.created_at;
  const when = stamp ? new Date(stamp * 1000).toLocaleString('en-GB', {weekday:'short', day:'numeric', month:'short', hour:'2-digit', minute:'2-digit', timeZone:'Asia/Singapore'}) : ''; 
  if (v && (v.status === 'confirmed' || v.confirmed)) return 'Booked' + (when ? ', ' + when : '');
  if (v && v.status === 'published') return 'Proposal sent';
  const changed = v && v.version_no > 1 && d.messages.some(m => m.direction === 'inbound');
  return (changed ? 'Change requested' : (SOURCE_LABELS[d.source] || d.source)) + (when ? ', ' + when : '');
}
function railDeal(d) {
  const [label, tone] = dealStage(d);
  const sel = d.deal_id === selectedDealId ? ' sel' : '';
  return `<button type="button" class="rail-deal${sel}" ${sel ? 'aria-current="true"' : ''} onclick="selectDeal('${d.deal_id}')"><b>${esc(d.client)}</b><span class="dmeta">${esc(railMeta(d))}</span><span class="st ${tone}">${esc(label)}</span></button>`;
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
  const closed = deals.filter(d => d.status === 'closed' || d.status === 'rejected' || latestVersion(d)?.status === 'rejected');
  const others = deals.filter(d => !needsAttention(d) && !closed.includes(d));
  document.getElementById('week-capacity').innerHTML = weekHtml();
  if (!deals.length) {
    queue.innerHTML = '<p class="hint">Nothing needs you right now.</p>';
    progress.innerHTML = '<p class="hint">Nothing in progress.</p>';
    document.getElementById('deal-closed').innerHTML = '';
    root.className = 'empty-state';
    root.innerHTML = '<span class="big">·</span><h2>No inquiries yet</h2><p class="muted">New Hashmeta inquiries will appear here after QwenWork claims them.</p>';
    return;
  }
  const byRecent = (a, b) => lastActivity(b) - lastActivity(a);
  attention.sort(byRecent); others.sort(byRecent); closed.sort(byRecent);
  if (!selectedDealId || !deals.some(d => d.deal_id === selectedDealId))
    selectedDealId = (attention[0] || others[0] || closed[0]).deal_id;
  queue.innerHTML = attention.map(railDeal).join('') || '<p class="hint">Nothing needs you right now.</p>';
  progress.innerHTML = others.map(railDeal).join('') || '<p class="hint">Nothing in progress.</p>';
  const d = deals.find(x => x.deal_id === selectedDealId);
  document.getElementById('deal-closed').innerHTML = closed.map(railDeal).join('') || '<p class="hint">No closed deals.</p>';
  root.className = '';
  root.innerHTML = dealHtml(d);
}
function estimatePanel(d, v) {
  const offer = (v.comparison && v.comparison.offer && Object.keys(v.comparison.offer).length) ? v.comparison.offer : dealOffer(d);
  const reasons = plainReasons(v, offer);
  if (v.confirmed || ['confirmed','rejected'].includes(v.status) || d.status === 'closed') return '';
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
  const confirmation = document.getElementById('msg-' + versionId);
  if (confirmation?.querySelector('.release-confirm')) confirmation.textContent = 'Options changed. Review and check again before release.';
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
function selectedSimilar(d, v) {
  const items = d.similar_work || [];
  if (!items.length) return null;
  if (similarPick[d.deal_id] == null) {
    const current = v?.page?.portfolio?.[0]?.asset_id;
    similarPick[d.deal_id] = Math.max(0, items.findIndex(x => x.asset_id === current));
  }
  return items[similarPick[d.deal_id] % items.length];
}
function similarRow(d) {
  const it = selectedSimilar(d, latestVersion(d));
  if (!it) return '<h5>Similar work to show</h5><p class="hint">Add shared work to your portfolio to include it on a proposal.</p>';
  const items = d.similar_work || [];
  return `<h5>Similar work to show</h5><div class="orow one"><span><b>${esc(it.title)}</b><br><span class="m">${esc(it.reason)}</span></span>${items.length > 1 ? `<button type="button" class="bbtn sm" onclick="swapSimilar('${d.deal_id}')">Change</button>` : ''}</div><p class="hint">This work will be included when the proposal is checked.</p>`;
}
function swapSimilar(dealId) { const d = latestDeals.find(x => x.deal_id === dealId); const v = d && latestVersion(d); const input = v && document.getElementById('draft-' + v.version_id); const unsaved = input ? input.value : null; similarPick[dealId] = (similarPick[dealId] || 0) + 1; renderWorkspace(latestDeals); const restored = v && document.getElementById('draft-' + v.version_id); if (restored && unsaved !== null) restored.value = unsaved; }

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
    <div class="main-head"><h1>${title}</h1>${verdictChip(d, v)}</div>
    <p class="hint">${esc(railMeta(d))}</p>${v && v.capacity_stale && ['draft','checked','blocked','evaluated'].includes(v.status) ? `<div class="warn"><span class="wi">!</span><span><b>Your calendar changed since this estimate</b>${(v.capacity_changes || []).length ? `<ul>${v.capacity_changes.map(c => `<li>Week of ${esc(shortDate(weekStart(c.week)))}: ${hrs(c.before)} free before, ${hrs(c.after)} free now.</li>`).join('')}</ul>` : 'The capacity revision changed.'}Re-run the estimate before approving this proposal.<div class="row"><button class="bbtn sm" onclick="doReestimate('${d.deal_id}','${latestOffer.offer_id}')">Re-run the estimate</button><button class="bbtn sm" onclick="showSection('calendar')">See it on the calendar</button></div></span></div>` : ''}
    ${clientSaidHtml(d)}
    ${needsInfoHtml}${extraPanel}
    ${v && (v.reason_codes || []).includes('MANUAL_ESTIMATE_REQUIRED') ? `<section class="panel"><h5>This job needs your estimate</h5><p>Enter your total planning hours and list price using your approved studio rates. No missing estimate is filled in automatically.</p><div class="row"><label>Planning hours<input id="manual-hours-${d.deal_id}" type="number" min="0.01" step="0.01"></label><label>List price S$<input id="manual-price-${d.deal_id}" type="number" min="0.01" step="0.01"></label></div><div class="row"><button class="bbtn p" onclick="submitManualEstimate('${d.deal_id}')">Run estimate</button><a href="/estimating?owner=${encodeURIComponent(OWNER)}">View your studio rates</a></div><p class="hint">A saved studio rate and available resources are required.</p></section>` : ''}
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
  const approvable = canEdit && !v.capacity_stale && opts.length > 0 && v.composition_ready && v.proposal_recipient?.confirmed;
  const multi = opts.length > 1;
  const set = offeredSet(v);
  const accepted = v.accepted_option ? v.accepted_option.option_id : null;
  const rows = opts.map(o => {
    const m = meta[o.option_id] || {};
    const rowId = m.option_row_id || '';
    const name = optionName(o.option_id, o, m.label);
    const price = `<b>${money0(o.price_sgd)}</b>`;
    const picked = accepted === rowId ? ' picked' : '';
    const preview = rowId && ['draft','checked','published'].includes(v.status)
      ? `<div class="row"><button type="button" class="bbtn sm" onclick="previewCapacity('${v.version_id}','${rowId}',this)">Preview on calendar</button><p role="status" class="small"></p></div>` : '';
    if (!multi) {
      const shoot = offer.shoot_days ? `, ${offer.shoot_days} shoot day${offer.shoot_days === 1 ? '' : 's'}` : '';
      return `<div class="orow one${picked}"><span><b>${esc(name)}</b>${accepted === rowId ? ' <span class="st ok">Client picked this</span>' : ''}<br><span class="m">${o.deliverables} videos, ${o.caption_versions} captioned${shoot}, by ${esc(shortDate(o.deadline))}</span></span>${price}</div>${preview}`;
    }
    const on = set.has(rowId);
    return `<label class="orow ck${picked}${on ? '' : ' off'}"><span><input type="checkbox" ${on ? 'checked' : ''} ${approvable ? '' : 'disabled'} onchange="toggleOffer('${v.version_id}','${rowId}',this)"> <b>${esc(name)}</b>${accepted === rowId ? ' <span class="st ok">Picked</span>' : ''}</span><span class="m">${esc(optionDesc(o))}</span>${price}</label>${preview}`;
  }).join('');
  const draft = canEdit
    ? `<label class="sr" for="draft-${v.version_id}">Client-facing draft</label><textarea class="draft" id="draft-${v.version_id}">${esc(v.draft)}</textarea>`
    : `<div class="draft">${esc(v.draft)}</div>`;
  const clean = (v.status === 'blocked' || v.outbound_status === 'blocked') && canEdit ? cleanDraftFor(d, v) : null;
  if (clean) cleanDrafts[v.version_id] = clean;
  const est = v.estimate || {};
  const deliveryStatus = v.email_delivery ? `<p class="hint">Email: ${esc(v.email_delivery.status)} ${esc(v.email_delivery.error || '')}</p>${v.email_delivery.status === 'failed' ? `<button class="bbtn" onclick="retryEmail('${v.version_id}')">Retry this email</button>` : ''}` : '';
  const buttons = `${deliveryStatus}${canEdit ? `<button class="bbtn p" ${approvable ? '' : (v.capacity_stale ? 'disabled title="The calendar changed. Re-run the estimate first."' : 'disabled title="A priced option is required."')} onclick="doApprove('${v.version_id}')">Approve and send</button>` : ''}
    ${clean ? `<button class="bbtn" onclick="useClean('${v.version_id}', this)">Use the clean draft</button>` : ''}
    ${canEdit ? `<button class="bbtn" onclick="doReject('${v.version_id}')">Reject</button>` : ''}${extras || ''}`;
  const helper = canEdit ? (!v.composition_ready || !v.proposal_recipient?.confirmed ? v.proposal_status : v.capacity_stale ? 'Approve is locked. The calendar changed since this estimate. Re-run the estimate first.' : !opts.length ? 'Approve is locked until a priced option is available.' : 'Runs the checks, then creates the client’s private proposal page. Nothing goes out before you approve.')
    : (v.status === 'published' ? 'Sent. The client has a private link to this proposal.' : (v.status === 'confirmed' ? 'Booked. The hours are set aside.' : ''));
  const msgLine = `<div id="msg-${v.version_id}" role="status" aria-live="polite" class="small msg-line"></div>`;
  if (!multi) return `<div class="two">
    <div class="panel"><h5>Option the client will see</h5>${rows || '<p class="hint">No option yet.</p>'}
      <h5>Draft message</h5>${draft}
      ${similarRow(d)}
</div>
    <div class="panel"><h5>Before it goes out</h5><div class="checks-stack" aria-live="polite">${checkRows(v)}</div>
      <div class="row">${buttons}</div>${helper ? `<span class="helper">${esc(helper)}</span>` : ''}${msgLine}</div>
  </div>`;
  const one = v.outbound_status === 'blocked' ? (() => { const [t, x] = draftLeak(v); return checkRow('bad', t, x); })()
    : (v.outbound_status === 'passed' ? checkRow('ok', 'Safe to send', 'No hours, minimum price or other client names in the message.')
      : checkRow('wait', 'Not checked yet', 'Checked when you press Approve and send.'));
  return `<div class="two">
    <div class="panel"><h5>Options for the client</h5><div class="olist">${rows}
      <span class="hint">${approvable ? 'Untick any option you don\'t want to offer.' : (v.offered_option_ids ? 'Only the ticked options were offered.' : 'Every option was offered.')}</span></div></div>
    <div class="panel">${similarRow(d)}<h5>Draft message</h5>${draft}<div aria-live="polite">${one}</div>
      <div class="row">${buttons}</div>${msgLine}</div>
  </div>`;
}
"""


CALENDAR_JS = r"""
let calendarStart = 0;
let calendarLastRevision = null;
let calendarToastTimer;
function weekStart(s) { const d = new Date(s + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate()-4); return d.toISOString().slice(0,10); }
function weekEnd(s) { const d = new Date(s + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate()+2); return d.toISOString().slice(0,10); }
function calHours(n) { return Number(n).toFixed(1); }
function calChangeWindow(n) { const max = Math.max(0, ((latestCapacity || {}).weeks || []).length - 6); calendarStart = n === 0 ? 0 : Math.max(0, Math.min(max, calendarStart + n)); renderCalendar(); }
function calToast(message) {
  let el = document.getElementById('cal-toast');
  if (!el) { el = document.createElement('div'); el.id = 'cal-toast'; el.className = 'cal-toast'; el.setAttribute('role', 'status'); document.body.append(el); }
  el.textContent = message; el.hidden = false;
  clearTimeout(calendarToastTimer); calendarToastTimer = setTimeout(() => { el.hidden = true; }, 5000);
}
async function calendarWrite(action, data) {
  const buttons = [...document.querySelectorAll('#section-view .cal-control')];
  buttons.forEach(b => b.disabled = true);
  try {
    const r = await api('/api/calendar/' + action, data);
    if (!r.data.ok) { calToast(r.data.message || 'Could not update the calendar.'); return; }
    await refresh();
    renderCalendar();
    const affected = latestDeals.filter(d => { const v=latestVersion(d); return v && v.capacity_stale && ['draft','checked','blocked','evaluated'].includes(v.status); }).length;
    const actionText = action === 'hours' ? 'Working hours saved.' : action === 'block' ? 'Time blocked.' : 'Time block removed.';
    calToast(actionText + (affected ? ' ' + affected + ' proposal' + (affected === 1 ? '' : 's') + ' need a fresh estimate.' : ''));
  } finally { buttons.forEach(b => b.disabled = false); }
}
function saveCalendarHours() {
  const el = document.getElementById('cal-hours');
  if (!el.value || !el.checkValidity()) { calToast('Enter working hours between 1 and 80.'); el.focus(); return; }
  calendarWrite('hours', {hours: Number(el.value)});
}
function addCalendarBlock() {
  const el = document.getElementById('cal-block-hours');
  if (!el.value || !el.checkValidity()) { calToast('Enter block hours between 1 and 80.'); el.focus(); return; }
  calendarWrite('block', {week: document.getElementById('cal-week').value, hours: Number(el.value), reason: document.getElementById('cal-reason').value});
}
function renderCalendar() {
  const c = latestCapacity || {weeks:[],jobs:[],blocks:[]};
  calendarLastRevision = c.revision;
  const all = c.weeks || [], weeks = all.slice(calendarStart, calendarStart + 6), jobs = c.jobs || [], blocks = c.blocks || [];
  const jobIds = [...new Set(jobs.map(j => j.deal_id))];
  const colors = ['#7a97c4','#c9a45c','#be7f95','#91a985','#8d83b0'];
  const blockedFor = w => blocks.filter(b => b.week === w.week).reduce((n,b) => n+b.hours,0);
  const scale = Math.max(1, ...weeks.map(w => Math.max(w.working_hours, w.allocated_hours)));
  const bar = weeks.map(w => {
    const start = weekStart(w.week), end = weekEnd(w.week), entries = jobs.filter(j => j.week === w.week);
    const over = Math.max(0,w.allocated_hours-w.budget_hours), blocked = blockedFor(w);
    const unavailable = Math.max(0,w.working_hours-w.budget_hours);
    const shownBlocked = Math.min(unavailable, blocked);
    const existing = Math.max(0,unavailable-shownBlocked);
    const otherSeg = existing ? `<div class="cal-unavailable" title="Existing commitments and planning reserve" style="left:${w.budget_hours/scale*100}%;width:${existing/scale*100}%">${existing/scale>0.17 ? 'Unavailable '+calHours(existing)+' h' : ''}</div>` : '';
    const reason = blocks.find(b=>b.week===w.week)?.reason || 'Blocked';
    const blockSeg = shownBlocked ? `<div class="cal-blocked" title="${esc(reason)}: ${calHours(blocked)} h blocked" style="left:${(w.budget_hours+existing)/scale*100}%;width:${shownBlocked/scale*100}%">${shownBlocked/scale>0.14 ? esc(reason)+' '+calHours(shownBlocked)+' h' : ''}</div>` : '';
    let left = 0;
    const seg = entries.map(j => { const at=left; left+=j.hours; const width=j.hours/scale*100; return `<div class="cal-segment" title="${esc(j.client)}: ${calHours(j.hours)} h" style="left:${at/scale*100}%;width:${width}%;background:${colors[jobIds.indexOf(j.deal_id)%colors.length]}">${width>17 ? esc(j.client.split(' ')[0])+' '+calHours(j.hours)+' h' : ''}</div>`; }).join('');
    const hatch = over ? `<div class="cal-over" style="left:${w.budget_hours/scale*100}%;width:${over/scale*100}%"></div>` : '';
    return `<div class="calendar-week"><div class="cal-date"><b>${esc(shortDate(start))}</b><span>to ${esc(shortDate(end))}</span></div><div class="calendar-bar ${over ? 'is-over' : ''}">${otherSeg}${blockSeg}${seg}${hatch}<i class="cal-limit" style="left:${w.budget_hours/scale*100}%"></i></div><div class="cal-remaining">${over ? `<b class="cal-red">Over by ${calHours(over)} h</b><span class="hint">${calHours(w.allocated_hours)} h booked, ${calHours(w.budget_hours)} h available</span>` : `${calHours(Math.max(0,w.remaining_hours))} h free`}</div></div>`;
  }).join('');
  const jobRows = jobIds.map(id => { const group=jobs.filter(j=>j.deal_id===id), j=group[0]; return `<tr><th scope="row"><button class="calendar-link" onclick="selectDeal('${esc(id)}')">${esc(j.client)}</button></th>${weeks.map(w => {const x=group.find(a=>a.week===w.week);return `<td>${x ? calHours(x.hours) : '–'}</td>`;}).join('')}</tr>`; }).join('');
  const affected = latestDeals.filter(d => {const v=latestVersion(d);return v && v.capacity_stale && ['draft','checked','blocked','evaluated'].includes(v.status);});
  const blockList = blocks.map(b => `<div class="cal-block-row"><span>${esc(b.reason)}, ${calHours(b.hours)} h, week of ${esc(shortDate(weekStart(b.week)))}</span><button class="calendar-link cal-control" onclick="calendarWrite('remove',{id:'${esc(b.id)}'})">Remove</button></div>`).join('');
  const range = weeks.length ? shortDate(weekStart(weeks[0].week)) + ' to ' + shortDate(weekEnd(weeks[weeks.length-1].week)) : 'No calendar weeks';
  const legend = jobIds.map((id,i)=>`<span><i style="background:${colors[i%colors.length]}"></i>${esc(jobs.find(j=>j.deal_id===id).client)}</span>`).join('') + '<span><i class="legend-blocked"></i>Blocked</span><span><i class="legend-unavailable"></i>Existing commitments and reserve</span>';
  const defaultHours = c.working_hours == null ? (weeks[0]?.working_hours ?? 30) : c.working_hours;
  const maxStart = Math.max(0, all.length - 6);
  document.getElementById('section-view').innerHTML = `<div class="cal-head"><div><h1>Studio calendar</h1><p class="hint">Booked hours per job, per week. BidPilot plans in hours, so there are no invented time slots.</p></div><div class="cal-navigation"><button class="bbtn sm" aria-label="Previous week" onclick="calChangeWindow(-1)" ${calendarStart===0?'disabled':''}>‹</button><button class="bbtn sm" onclick="calChangeWindow(0)">Today</button><button class="bbtn sm" aria-label="Next week" onclick="calChangeWindow(1)" ${calendarStart>=maxStart?'disabled':''}>›</button></div></div><div class="cal-layout"><div class="cal-main"><section class="panel cal-panel"><h3 class="cal-panel-head"><span>${esc(range)}</span><span>${c.working_hours == null ? 'Working hours vary by week' : 'Working hours: '+calHours(c.working_hours)+' h a week'}</span></h3><p class="cal-note">Each bar is one week of your working hours. Coloured blocks are booked jobs, hatched is time you blocked, and the empty part is free. The dashed line is your buffered capacity after existing commitments, and red stripes are hours over it.</p>${bar}<div class="cal-legend">${legend}</div></section><section class="panel cal-panel cal-table"><h3>Hours per job</h3><table><thead><tr><th>Job</th>${weeks.map(w=>`<th>${esc(shortDate(weekStart(w.week)))}</th>`).join('')}</tr></thead><tbody>${jobRows}<tr><th scope="row">Blocked</th>${weeks.map(w=>{const b=blockedFor(w);return `<td>${b ? calHours(b) : '–'}</td>`;}).join('')}</tr><tr><th scope="row">Free</th>${weeks.map(w=>`<td><b>${calHours(Math.max(0,w.remaining_hours))}</b></td>`).join('')}</tr></tbody></table></section></div><div class="cal-sidebar"><section class="panel cal-panel"><h3>Working hours</h3><label>Hours a week<input id="cal-hours" type="number" min="1" max="80" step="0.01" value="${defaultHours}"></label><div><button class="bbtn p sm cal-control" onclick="saveCalendarHours()">Save</button></div></section><section class="panel cal-panel"><h3>Block time</h3><label>Week<select id="cal-week">${weeks.map(w=>`<option value="${esc(w.week)}">Week of ${esc(shortDate(weekStart(w.week)))}</option>`).join('')}</select></label><label>Hours<input id="cal-block-hours" type="number" min="1" max="80" step="0.01" value="8"></label><label>Reason<select id="cal-reason"><option>Leave</option><option>Admin</option><option>Outside booking</option></select></label><div><button class="bbtn p sm cal-control" onclick="addCalendarBlock()">Block it</button></div><div class="cal-block-list">${blockList}</div></section><section class="panel cal-panel"><h3>Proposals affected</h3>${affected.map(d=>`<div><button class="calendar-link" onclick="selectDeal('${esc(d.deal_id)}')">${esc(d.client)}</button> needs a fresh estimate.</div>`).join('') || '<p class="cal-note">No proposals need a fresh estimate.</p>'}</section></div></div>`;
}

"""


def submitted_briefs_page(conn):
    """Read immutable receipts, never reconstruct a submitted scope from live chat."""
    def _esc(value):
        return html.escape('' if value is None else str(value), quote=True)

    cards = []
    rows = conn.execute(
        'SELECT s.*,i.revision AS current_revision,i.processed_seq AS current_sequence '
        'FROM studio_submissions s JOIN inquiries i ON i.id=s.inquiry_id '
        'WHERE i.revoked=0 ORDER BY s.created_at DESC,s.rowid DESC').fetchall()
    for row in rows:
        snapshot = db.uj(row['snapshot_json']) or {}
        turns = snapshot.get('turns', [])
        assets = {r['id']: r for r in snapshot.get('references', [])}
        later = row['current_revision'] != row['revision'] or row['current_sequence'] != row['sequence'] or conn.execute(
            "SELECT 1 FROM inquiry_turns WHERE inquiry_id=? AND sender='buyer' AND seq>?",
            (row['inquiry_id'], row['sequence'])).fetchone()
        body = '<p class="hint">Immutable submitted revision ' + _esc(row['revision']) + '. ' + (
            'Later buyer changes exist; this receipt is unchanged.' if later else 'No later buyer changes.') + '</p>'
        body += '<h3>Buyer preferences and sources</h3>'
        for turn in turns:
            if turn.get('sender') != 'buyer':
                continue
            body += '<section class="panel"><h4>Buyer turn ' + _esc(turn['seq']) + '</h4><p style="white-space:pre-wrap;overflow-wrap:anywhere">' + _esc(turn.get('body', '')) + '</p>'
            for ref in turn.get('references', []):
                asset = assets.get(ref.get('asset_id'), {})
                body += '<p>Selected reference: ' + _esc(asset.get('filename', 'Reference unavailable'))
                if 'start' in ref:
                    body += ' at ' + _esc(ref['start']) + ' seconds'
                if 'end' in ref:
                    body += ' through ' + _esc(ref['end']) + ' seconds'
                body += '. ' + ('Observation source: ' + _esc(ref['observation_id']) if ref.get('observation_id') else 'Buyer-selected timestamp; no observation linkage supplied.') + '</p>'
            body += '</section>'
        facts = snapshot.get('facts', [])
        replaced = {f.get('supersedes') for f in facts if f.get('supersedes')}
        body += '<h3>Structured facts and corrections</h3>'
        if not facts:
            body += '<p>No structured facts captured. Buyer text above remains source evidence, not verified extraction.</p>'
        for fact in facts:
            body += '<p><b>' + _esc(fact.get('subject', '')) + '</b>: ' + _esc(json.dumps(fact.get('value'), ensure_ascii=False)) + ' (' + _esc(fact.get('status', '')) + '). Source buyer turn ' + _esc(fact.get('source_seq', '')) + '. Fact ' + _esc(fact.get('id', ''))
            if fact.get('id') in replaced:
                body += ' [superseded]'
            if fact.get('supersedes'):
                body += '; replaces ' + _esc(fact['supersedes'])
            body += '</p>'
        body += '<h3>Analyzed cue evidence</h3><p class="hint">Machine/agent observations are not buyer confirmation or a production commitment.</p>'
        if 'reference_evidence' not in snapshot:
            body += '<p>Legacy receipt: analyzed evidence was not frozen at submission. Current observations are intentionally not substituted.</p>'
        for evidence in snapshot.get('reference_evidence', []):
            asset = assets.get(evidence.get('asset_id'), {})
            body += '<h4>' + _esc(asset.get('filename', 'Reference')) + '</h4>'
            observations = evidence.get('observations', [])
            for cue in observations:
                if cue.get('kind') != 'production_cue':
                    continue
                body += '<section class="panel"><p>' + _esc(cue.get('body', '')) + '</p><p>Time: ' + _esc(cue.get('start')) + ' through ' + _esc(cue.get('end')) + ' seconds. Cue: ' + _esc(cue.get('id', '')) + '; source observation: ' + _esc(cue.get('supersedes', '')) + '</p><p>Method: ' + _esc(cue.get('method', '')) + '. Uncertainty: ' + _esc(cue.get('uncertainty', '')) + '</p></section>'
            if not any(o.get('kind') == 'production_cue' for o in observations):
                body += '<p>No analyzed production cues captured.</p>'
        cards.append('<article class="panel"><h2>Submitted brief</h2>' + body + '</article>')
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Submitted briefs</title><style>' + _DESIGN_BASE + '</style></head><body><header><a href="/dashboard">Founder dashboard</a><h1>Submitted briefs</h1></header><main style="max-width:1000px;margin:auto;padding:24px"><p>Private studio review. Receipt storage does not notify Alex or confirm a booking.</p>' + (''.join(cards) or '<p>No submitted briefs yet.</p>') + '</main></body></html>'


def founder_page(conn, api=None):
    """The founder dashboard (wireframe v5 build, see founder_ui.py)."""
    import founder_ui
    fixtures = [{"source_id": sid, "label": label, "raw": raw, "offer_request": offer}
                for sid, label, offer, raw in FIXTURES]
    # Nothing sits above the dashboard's top bar. QwenWork drafting still runs
    # through /api/qwenwork/draft and its automatic triggers; connecting is at
    # /qwenwork/connect. The /submitted-briefs page still exists, unlinked.
    return founder_ui.page(SAMPLE_BADGE, fixtures)


class StudioServer(ThreadingHTTPServer):
    """One media consumer per serving process. SQLite leases coordinate restarts."""
    def server_close(self):
        self.qwenwork_stop.set()
        if self.qwenwork_worker:
            self.qwenwork_worker.join(timeout=20)
        super().server_close()

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
                qwenwork_secret_file=None, owner_token_file=None,
                qwenwork_client_id=None, qwenwork_redirect_uri=qwenwork_api.REDIRECT_URI):
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
        conn.executescript(qwenwork_api.SCHEMA)
        import qwenwork_runtime
        conn.executescript(qwenwork_runtime.SCHEMA)
        # Deals whose latest version was rejected before the deal status was
        # settled on reject leave the work queue (history is kept).
        # Historical reconciliation is explicit, never an implicit startup write.
    finally:
        conn.close()
    server = StudioServer(("127.0.0.1", port), Handler)
    server.db_path = db_path
    server.public_origin = public_origin
    server.embed_origins = embed_origins
    server.owner_token = (load_owner_token(owner_token_file) if owner_token_file
                          else secrets.token_urlsafe(24))
    server.qwenwork_api = qwenwork_api.Client(qwenwork_client_id, qwenwork_redirect_uri)
    server.qwenwork_stop = threading.Event()
    server.qwenwork_worker = None
    if qwenwork_client_id or (server.qwenwork_api.connected() and os.environ.get('BIDPILOT_QWENWORK_EVENT_RUNTIME') == '1'):
        server.qwenwork_worker = threading.Thread(target=server.qwenwork_api.worker,
            args=(db_path, server.qwenwork_stop), name='qwenwork-quotes', daemon=True)
        server.qwenwork_worker.start()
    server.qwenwork_secret = qwenwork_callback.load_secret(qwenwork_secret_file)
    return server, server.owner_token


def main():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / '.env', override=False)
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
    parser.add_argument('--qwenwork-client-id', default=os.environ.get('BIDPILOT_QWENWORK_CLIENT_ID'))
    parser.add_argument('--qwenwork-redirect-uri', default=qwenwork_api.REDIRECT_URI)
    args = parser.parse_args()
    token_file = None if str(args.owner_token_file).lower() == "none" else args.owner_token_file
    server, owner_token = make_server(args.db, args.port, args.public_origin, args.embed_origin,
                                      args.qwenwork_secret_file, owner_token_file=token_file,
                                      qwenwork_client_id=args.qwenwork_client_id,
                                      qwenwork_redirect_uri=args.qwenwork_redirect_uri)
    port = server.server_address[1]
    with server:
        print(f"BidPilot founder dashboard: http://127.0.0.1:{port}/?owner={owner_token}",
              flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
