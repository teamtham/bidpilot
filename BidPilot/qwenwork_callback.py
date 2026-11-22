"""QwenWork API result callback: POST /qwenwork/callback.

QwenWork calls this route when a job finishes, so BidPilot hears about the
result immediately instead of waiting for the next scheduled poll.

Contract (bidpilot.qwenwork-callback.v1)
---------------------------------------
Request body (JSON)::

    {"job_id": "<QwenWork job id>",
     "status": "completed" | "failed",
     "inquiry_id": "<BidPilot inquiry id>",      # or metadata.inquiry_id
     "result": {"draft_body": "<client-facing quote text>"},
     "error": "<optional failure text>"}

Authentication, either of:

* ``X-BidPilot-Timestamp: <unix seconds>`` and
  ``X-BidPilot-Signature: sha256=<hex HMAC-SHA256(secret, timestamp + "." + raw body)>``
  (preferred; the timestamp must be within five minutes), or
* ``Authorization: Bearer <secret>`` for senders that only support a fixed header.

Behaviour:

* A completed result for an inquiry whose deal has an editable proposal is
  saved as a NEW draft proposal version (outbound status ``unchecked``). It is
  never checked, approved or published here: the founder still reviews,
  checks and approves it on the dashboard.
* A result for an inquiry that has not been priced yet, or whose latest
  proposal version is no longer a draft, is stored as ``held`` for the
  founder; nothing changes.
* Each job_id is processed once. A repeat delivery with the same body gets
  the original answer (200, ``duplicate: true``); the same job_id with a
  different body is rejected with 409.

The shared secret is read from ``BIDPILOT_QWENWORK_SECRET`` or from the file
given by ``--qwenwork-secret-file``. With no secret configured the route is
disabled (503).
"""
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import core
import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS qwenwork_callbacks (job_id TEXT PRIMARY KEY,
  payload_hash TEXT NOT NULL, inquiry_id TEXT, deal_id TEXT, version_id TEXT,
  outcome TEXT NOT NULL, response_json TEXT NOT NULL, payload_json TEXT NOT NULL,
  received_at REAL NOT NULL);
"""

PATH = "/qwenwork/callback"
MAX_BODY = 256 * 1024
MAX_SKEW = 300
SECRET_ENV = "BIDPILOT_QWENWORK_SECRET"


class CallbackError(Exception):
    def __init__(self, code, error):
        super().__init__(error)
        self.code = code
        self.error = error


def load_secret(path=None):
    value = os.environ.get(SECRET_ENV, "")
    if not value and path:
        value = Path(path).expanduser().read_text(encoding="utf-8")
    value = value.strip()
    if value and len(value) < 32:
        raise ValueError("QwenWork callback secret must be at least 32 characters")
    return value or None


def sign(secret, timestamp, raw):
    mac = hmac.new(secret.encode("utf-8"), str(timestamp).encode("ascii") + b"." + raw,
                   hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def authenticate(headers, raw, secret, now=None):
    signature = headers.get("X-BidPilot-Signature")
    if signature:
        stamp = headers.get("X-BidPilot-Timestamp", "")
        if not stamp.isdigit() or abs((now or time.time()) - int(stamp)) > MAX_SKEW:
            raise CallbackError(401, "stale_or_missing_timestamp")
        if not hmac.compare_digest(signature.encode("utf-8"),
                                   sign(secret, stamp, raw).encode("utf-8")):
            raise CallbackError(401, "bad_signature")
        return
    auth = headers.get("Authorization", "")
    if auth.startswith("Bearer ") and hmac.compare_digest(
            auth[7:].strip().encode("utf-8"), secret.encode("utf-8")):
        return
    raise CallbackError(401, "unauthenticated")


def _parse(raw):
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=core_reject)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise CallbackError(400, "bad_json")
    if not isinstance(data, dict):
        raise CallbackError(400, "bad_json")
    job = data.get("job_id")
    if not isinstance(job, str) or not job.strip() or len(job) > 200:
        raise CallbackError(400, "job_id_required")
    status = data.get("status", "completed")
    if status not in ("completed", "failed"):
        raise CallbackError(400, "invalid_status")
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    inquiry = data.get("inquiry_id") or meta.get("inquiry_id")
    if not isinstance(inquiry, str) or not inquiry.strip():
        raise CallbackError(400, "inquiry_id_required")
    body = None
    if status == "completed":
        result = data.get("result")
        body = result.get("draft_body") if isinstance(result, dict) else None
        if not isinstance(body, str) or not body.strip() or len(body) > 20000:
            raise CallbackError(400, "result.draft_body_required")
    return job.strip(), status, inquiry.strip(), body, data


def core_reject(value):
    raise ValueError("non-finite number")


def process(conn, raw):
    """Apply one authenticated delivery. Returns (http_code, response)."""
    job, status, inquiry_id, body, data = _parse(raw)
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    conn.executescript(SCHEMA)
    conn.execute("BEGIN IMMEDIATE")
    try:
        prior = conn.execute("SELECT * FROM qwenwork_callbacks WHERE job_id=?", (job,)).fetchone()
        if prior:
            conn.rollback()
            if prior["payload_hash"] != payload_hash:
                raise CallbackError(409, "job_id_conflict")
            return 200, {**json.loads(prior["response_json"]), "duplicate": True}
        inquiry = conn.execute("SELECT * FROM inquiries WHERE id=?", (inquiry_id,)).fetchone()
        if inquiry is None:
            raise CallbackError(404, "unknown_inquiry")
        deal_id = inquiry["deal_id"]
        version = None
        if deal_id:
            version = conn.execute("SELECT * FROM proposal_versions WHERE deal_id=?"
                                   " ORDER BY version_no DESC LIMIT 1", (deal_id,)).fetchone()
        new_version = None
        if status == "failed":
            outcome = "failed"
        elif version is None:
            outcome = "held_not_priced"
        elif conn.execute('SELECT 1 FROM studio_submissions WHERE inquiry_id=?',(inquiry_id,)).fetchone() and __import__('inquiry_quote').scope_error(conn, version):
            outcome = "held_stale_scope"
        elif version["status"] != "draft":
            # Released, accepted or rejected: never rewrite it from a callback.
            outcome = "held_not_editable"
        else:
            new_version = core.save_draft(conn, version["id"], body, commit=False)
            core._step(conn, deal_id, "qwenwork_callback", "ok",
                       "QwenWork job %s delivered a draft; founder review required" % job,
                       version["offer_id"])
            outcome = "draft_saved"
        response = {"ok": True, "job_id": job, "inquiry_id": inquiry_id, "outcome": outcome,
                    "draft_version_id": new_version,
                    "founder_approval_required": outcome == "draft_saved"}
        conn.execute("INSERT INTO qwenwork_callbacks VALUES (?,?,?,?,?,?,?,?,?)",
                     (job, payload_hash, inquiry_id, deal_id, new_version, outcome,
                      db.j(response), payload, db.now()))
        conn.commit()
        return 200, response
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def handle(h):
    """HTTP entry point, called from server.Handler.do_POST before origin checks."""
    secret = getattr(h.server, "qwenwork_secret", None)
    if not secret:
        return h.send_json({"ok": False, "error": "callback_disabled"}, 503)
    try:
        length = int(h.headers.get("Content-Length") or 0)
    except ValueError:
        return h.send_json({"ok": False, "error": "bad_length"}, 400)
    if length <= 0 or length > MAX_BODY:
        return h.send_json({"ok": False, "error": "bad_length"}, 413 if length > MAX_BODY else 400)
    raw = h.rfile.read(length)
    try:
        authenticate(h.headers, raw, secret)
        h.open_db()
        try:
            code, response = process(h.conn, raw)
        finally:
            h.conn.close()
    except CallbackError as exc:
        return h.send_json({"ok": False, "error": exc.error}, exc.code)
    return h.send_json(response, code)
