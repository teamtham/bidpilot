"""Local-first deployment operations for BidPilot studios.

This module deliberately contains no agent logic.  It gives an operator:

* one application database, operations database, private-media directory and
  credential directory per studio;
* a durable, leased notification queue for waking an explicitly configured
  QwenWork adapter;
* local operational metrics; and
* verified SQLite + media snapshots, retention and revocation.

The dispatch protocol is an adapter contract, not a QwenWork protocol.  A
command is executed without a shell and with a minimal environment.  A URL
must use HTTPS, except that HTTP is allowed for loopback hosts.  The adapter
must use an official/authorized QwenWork integration and treat ``event_id`` as
an idempotency key.  No browser state, cookies or credentials are discovered.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any, Iterable
from urllib import request
from urllib.parse import urlsplit
import uuid
import zipfile


REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS studios (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  workspace TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('active','revoked')),
  retention_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  revoked_at REAL
);
"""

OPERATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_events (
  event_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  retry_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('queued','leased','completed','dead')),
  available_at REAL NOT NULL,
  lease_owner TEXT,
  lease_token TEXT,
  lease_until REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL,
  created_at REAL NOT NULL,
  completed_at REAL,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS work_events_due
  ON work_events(status, available_at, lease_until, created_at);
CREATE TABLE IF NOT EXISTS metric_events (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  value REAL NOT NULL,
  labels_json TEXT NOT NULL,
  recorded_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS metric_events_time ON metric_events(recorded_at, name);
"""

ALLOWED_METRICS = frozenset({
    "analysis_seconds", "analysis_cost_sgd", "analysis_failed",
    "analysis_partial", "outstanding_client_questions",
    "duplicate_delivery_attempts", "manual_scope_overrides",
    "estimate_hours_error", "estimate_cost_error_sgd", "booking_conflicts",
    "dispatch_seconds", "dispatch_failures", "turn_reply_seconds",
})
SENSITIVE_LABEL = re.compile(r"(?:token|password|secret|cookie|authorization|body|message)", re.I)
SAFE_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")


class OperationsError(RuntimeError):
    """An operations invariant was not satisfied."""


class RetryConflict(OperationsError):
    """A retry key was reused with a different payload."""


class LeaseLost(OperationsError):
    """A worker attempted to finish an event after losing its lease."""


@dataclass(frozen=True)
class RetentionPolicy:
    media_days: int = 30
    inquiry_link_days: int = 30

    def __post_init__(self) -> None:
        for value in (self.media_days, self.inquiry_link_days):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3650:
                raise ValueError("retention days must be integers from 1 to 3650")

    def as_dict(self) -> dict[str, int]:
        return {"media_days": self.media_days, "inquiry_link_days": self.inquiry_link_days}


@dataclass(frozen=True)
class StudioPaths:
    studio_id: str
    workspace: Path
    app_db: Path
    operations_db: Path
    media_dir: Path
    credentials_dir: Path


@dataclass(frozen=True)
class WorkEvent:
    event_id: str
    kind: str
    payload: dict[str, Any]
    payload_hash: str
    retry_key: str
    attempt: int
    lease_token: str
    lease_until: float
    created_at: float


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_lease_seconds(value: float) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 1 <= value <= 3600):
        raise ValueError("lease_seconds must be from 1 to 3600")


class _RefuseRedirects(request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        fp.close()
        raise OperationsError("adapter_redirect_refused")

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


def _open_db(path: Path, schema: str = "") -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    if schema:
        conn.executescript(schema)
    return conn


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_private(path: Path, value: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise OperationsError(f"credential already exists: {path.name}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _contained(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve())
        return True
    except ValueError:
        return False


class StudioRegistry:
    """Creates and resolves isolated single-studio runtime directories."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).resolve()
        _private_directory(self.root)
        self.registry_db = self.root / "registry.sqlite3"
        conn = _open_db(self.registry_db, REGISTRY_SCHEMA)
        conn.close()
        os.chmod(self.registry_db, 0o600)

    def _conn(self) -> sqlite3.Connection:
        return _open_db(self.registry_db, REGISTRY_SCHEMA)

    def onboard(self, slug: str, policy: RetentionPolicy | None = None,
                credentials: dict[str, str | bytes] | None = None) -> StudioPaths:
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,47}", slug):
            raise ValueError("slug must be 2-48 lowercase letters, digits or hyphens")
        policy = policy or RetentionPolicy()
        studio_id = str(uuid.uuid4())
        relative = Path("studios") / studio_id
        workspace = self.root / relative
        paths = self._make_workspace(studio_id, workspace)
        try:
            for name, value in (credentials or {}).items():
                self._put_initial_credential(paths, name, value)
            conn = self._conn()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO studios VALUES (?,?,?,?,?,?,NULL)",
                        (studio_id, slug, str(relative), "active", _json(policy.as_dict()), time.time()),
                    )
            finally:
                conn.close()
        except Exception:
            shutil.rmtree(workspace, ignore_errors=True)
            raise
        return paths

    def _make_workspace(self, studio_id: str, workspace: Path) -> StudioPaths:
        workspace.mkdir(mode=0o700, parents=True, exist_ok=False)
        credentials = workspace / "credentials"
        media = workspace / "app.db.media"
        _private_directory(credentials)
        _private_directory(media)
        app_db = workspace / "app.db"
        # Import lazily so operations.py remains usable as a standalone helper.
        try:
            import db as bidpilot_db
            conn = bidpilot_db.connect(str(app_db))
            conn.close()
        except ImportError:
            conn = _open_db(app_db)
            conn.close()
        operations_db = workspace / "operations.db"
        conn = _open_db(operations_db, OPERATIONS_SCHEMA)
        conn.close()
        os.chmod(app_db, 0o600)
        os.chmod(operations_db, 0o600)
        return StudioPaths(studio_id, workspace, app_db, operations_db, media, credentials)

    def _put_initial_credential(self, paths: StudioPaths, name: str,
                                value: str | bytes) -> Path:
        if not SAFE_NAME.fullmatch(name):
            raise ValueError("credential name is invalid")
        data = value.encode("utf-8") if isinstance(value, str) else value
        if not isinstance(data, bytes) or not data or len(data) > 65536:
            raise ValueError("credential value must contain 1-65536 bytes")
        path = paths.credentials_dir / name
        _write_private(path, data)
        return path

    def paths(self, studio_id: str, require_active: bool = True) -> StudioPaths:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM studios WHERE id=?", (studio_id,)).fetchone()
        finally:
            conn.close()
        if not row or (require_active and row["status"] != "active"):
            raise OperationsError("studio not found or revoked")
        workspace = (self.root / row["workspace"]).resolve()
        if not _contained(workspace, self.root / "studios"):
            raise OperationsError("invalid studio workspace")
        return StudioPaths(studio_id, workspace, workspace / "app.db",
                           workspace / "operations.db", workspace / "app.db.media",
                           workspace / "credentials")

    def policy(self, studio_id: str) -> RetentionPolicy:
        conn = self._conn()
        try:
            row = conn.execute("SELECT retention_json FROM studios WHERE id=?", (studio_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            raise OperationsError("studio not found")
        return RetentionPolicy(**json.loads(row[0]))

    def credential_path(self, studio_id: str, name: str) -> Path:
        if not SAFE_NAME.fullmatch(name):
            raise ValueError("credential name is invalid")
        path = self.paths(studio_id).credentials_dir / name
        if not path.is_file() or path.is_symlink():
            raise OperationsError("credential not found")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise OperationsError("credential file permissions are too broad")
        return path

    def revoke_inquiry(self, studio_id: str, inquiry_id: str) -> bool:
        paths = self.paths(studio_id)
        conn = _open_db(paths.app_db)
        try:
            with conn:
                changed = conn.execute(
                    "UPDATE inquiries SET revoked=1 WHERE id=? AND revoked=0", (inquiry_id,)
                ).rowcount
            return bool(changed)
        finally:
            conn.close()

    def revoke(self, studio_id: str, now: float | None = None) -> dict[str, int]:
        """Revoke access and delete local credential files; business records remain."""
        paths = self.paths(studio_id)
        now = time.time() if now is None else now
        conn = _open_db(paths.app_db)
        inquiries = proposals = 0
        try:
            with conn:
                if _has_table(conn, "inquiries"):
                    inquiries = conn.execute("UPDATE inquiries SET revoked=1 WHERE revoked=0").rowcount
                if _has_table(conn, "proposal_versions"):
                    proposals = conn.execute(
                        "UPDATE proposal_versions SET token_hash=NULL WHERE token_hash IS NOT NULL"
                    ).rowcount
                if _has_table(conn, "inquiry_proposal_links"):
                    conn.execute("DELETE FROM inquiry_proposal_links")
        finally:
            conn.close()
        ops = _open_db(paths.operations_db, OPERATIONS_SCHEMA)
        try:
            with ops:
                ops.execute(
                    "UPDATE work_events SET status='dead',lease_owner=NULL,lease_token=NULL,"
                    " lease_until=NULL,last_error='studio_revoked' WHERE status IN ('queued','leased')"
                )
        finally:
            ops.close()
        removed = 0
        if paths.credentials_dir.exists():
            for item in paths.credentials_dir.iterdir():
                if item.is_file() and not item.is_symlink():
                    item.unlink()
                    removed += 1
        registry = self._conn()
        try:
            with registry:
                registry.execute(
                    "UPDATE studios SET status='revoked',revoked_at=? WHERE id=? AND status='active'",
                    (now, studio_id),
                )
        finally:
            registry.close()
        return {"inquiries": inquiries, "proposal_tokens": proposals,
                "credentials": removed}


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


class OperationsStore:
    """Durable notification queue and non-sensitive operational metrics."""

    def __init__(self, paths: StudioPaths):
        self.paths = paths
        conn = self._conn()
        conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = _open_db(self.paths.operations_db, OPERATIONS_SCHEMA)
        if not any(row[1] == "lane" for row in conn.execute("PRAGMA table_info(work_events)")):
            with conn:
                conn.execute("ALTER TABLE work_events ADD COLUMN lane TEXT")
        return conn

    def discover_app_work(self, now: float | None = None) -> dict[str, int]:
        """Project durable app work into identifier-only wake notifications.

        This is deliberately a recovery scan rather than a second source of
        truth.  App transactions own the work rows; the operations database
        only owns retryable wakes.  Repeated scans are harmless because every
        projection has a stable retry key.
        """
        now = time.time() if now is None else now
        app = _open_db(self.paths.app_db)
        projected = {"conversation_turn": 0, "media_update": 0,
                     "creative_request": 0, "portfolio_search_request": 0}
        try:
            if _has_table(app, "inquiry_jobs"):
                rows = app.execute(
                    "SELECT inquiry_id,MAX(seq) AS through_sequence FROM inquiry_jobs "
                    "WHERE status='queued' OR (status='leased' AND lease_until<=?) "
                    "GROUP BY inquiry_id ORDER BY inquiry_id", (now,)
                ).fetchall()
                for row in rows:
                    _, created = self.enqueue(
                        "conversation_turn",
                        {"inquiry_id": row["inquiry_id"],
                         "through_sequence": row["through_sequence"]},
                        f"conversation:{row['inquiry_id']}:{row['through_sequence']}",
                        max_attempts=12, lane=row["inquiry_id"], now=now,
                    )
                    projected["conversation_turn"] += int(created)

            if (_has_table(app, "media_notifications") and
                    _has_table(app, "reference_runs")):
                rows = app.execute(
                    "SELECT n.inquiry_id,n.run_id,r.status FROM media_notifications n "
                    "JOIN reference_runs r ON r.id=n.run_id "
                    "WHERE n.status='queued' ORDER BY n.inquiry_id,n.run_id"
                ).fetchall()
                for row in rows:
                    _, created = self.enqueue(
                        "media_update",
                        {"inquiry_id": row["inquiry_id"],
                         "analysis_id": row["run_id"], "status": row["status"]},
                        f"media:{row['inquiry_id']}:{row['run_id']}:{row['status']}",
                        max_attempts=12, lane=row["inquiry_id"], now=now,
                    )
                    projected["media_update"] += int(created)

            if (_has_table(app, "commerce_reference_requests") and
                    _has_table(app, "messages")):
                rows = app.execute(
                    "SELECT m.deal_id,m.version_id,m.request_id "
                    "FROM messages m JOIN commerce_reference_requests r "
                    "ON r.request_id=m.request_id "
                    "WHERE m.direction='inbound' AND m.request_id NOT LIKE 'additional:%' "
                    "AND NOT EXISTS (SELECT 1 FROM offers o WHERE o.deal_id=m.deal_id "
                    " AND o.source='change:' || m.request_id) "
                    "ORDER BY m.created_at,m.request_id"
                ).fetchall()
                for row in rows:
                    _, created = self.enqueue(
                        "creative_request",
                        {"deal_id": row["deal_id"], "version_id": row["version_id"],
                         "request_id": row["request_id"]},
                        f"creative:{row['request_id']}",
                        max_attempts=12, lane=row["deal_id"], now=now,
                    )
                    projected["creative_request"] += int(created)

            if _has_table(app, 'proposal_compositions'):
                rows = app.execute("SELECT v.id,v.deal_id FROM proposal_versions v JOIN deals d ON d.id=v.deal_id WHERE d.sample_data=1 AND v.status IN ('draft','checked','blocked') AND v.version_no=(SELECT MAX(version_no) FROM proposal_versions WHERE deal_id=v.deal_id) AND EXISTS (SELECT 1 FROM options o JOIN evaluations e ON e.id=o.evaluation_id WHERE e.offer_id=v.offer_id) AND (NOT EXISTS (SELECT 1 FROM proposal_compositions c WHERE c.version_id=v.id) OR v.outbound_status='unchecked')").fetchall()
                if rows:
                    projected['proposal_composition'] = 0
                for row in rows:
                    _, created = self.enqueue('proposal_composition', {'version_id': row['id'], 'deal_id': row['deal_id']},
                                              'compose:' + row['id'], max_attempts=12, lane=row['deal_id'], now=now)
                    projected['proposal_composition'] += int(created)

            if (_has_table(app, "portfolio_search_requests") and
                    _has_table(app, "proposal_versions") and
                    _has_table(app, "deals")):
                rows = app.execute(
                    "SELECT r.request_id,r.version_id FROM portfolio_search_requests r "
                    "JOIN proposal_versions v ON v.id=r.version_id "
                    "JOIN deals d ON d.id=v.deal_id "
                    "WHERE r.results_json IS NULL AND v.status IN ('published','confirmed') "
                    "AND d.sample_data=1 ORDER BY r.created_at,r.request_id"
                ).fetchall()
                for row in rows:
                    _, created = self.enqueue(
                        "portfolio_search_request",
                        {"request_id": row["request_id"],
                         "version_id": row["version_id"]},
                        f"portfolio-search:{row['request_id']}",
                        max_attempts=12, lane=row["version_id"], now=now,
                    )
                    projected["portfolio_search_request"] += int(created)
        finally:
            app.close()
        return projected

    def enqueue(self, kind: str, payload: dict[str, Any], retry_key: str,
                max_attempts: int = 5, now: float | None = None,
                lane: str | None = None) -> tuple[str, bool]:
        if not SAFE_NAME.fullmatch(kind):
            raise ValueError("event kind is invalid")
        if not isinstance(payload, dict):
            raise ValueError("event payload must be an object")
        if not isinstance(retry_key, str) or not 1 <= len(retry_key) <= 200:
            raise ValueError("retry_key is invalid")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be from 1 to 100")
        if lane is not None and (not isinstance(lane, str) or not 1 <= len(lane) <= 128):
            raise ValueError("lane is invalid")
        body = _json(payload)
        payload_hash = _digest(body.encode("utf-8"))
        now = time.time() if now is None else now
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute("SELECT event_id,payload_hash,kind FROM work_events WHERE retry_key=?",
                                 (retry_key,)).fetchone()
            if prior:
                if prior["payload_hash"] != payload_hash or prior["kind"] != kind:
                    raise RetryConflict("retry_key was reused with different work")
                conn.commit()
                return prior["event_id"], False
            event_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO work_events(event_id,kind,payload_json,payload_hash,retry_key,status,"
                "available_at,max_attempts,created_at,lane) VALUES (?,?,?,?,?,'queued',?,?,?,?)",
                (event_id, kind, body, payload_hash, retry_key, now, max_attempts, now, lane),
            )
            conn.commit()
            return event_id, True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim(self, worker_id: str, lease_seconds: float = 60,
              now: float | None = None) -> WorkEvent | None:
        if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 128:
            raise ValueError("worker_id is invalid")
        _validate_lease_seconds(lease_seconds)
        now = time.time() if now is None else now
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # A worker that crashed on its final allowed attempt cannot call
            # fail().  Close that terminal lease during the next recovery scan
            # instead of leaving it permanently stale.
            conn.execute(
                "UPDATE work_events SET status='dead',lease_owner=NULL,lease_token=NULL,"
                "lease_until=NULL,last_error='lease_expired' WHERE status='leased'"
                " AND lease_until<=? AND attempts>=max_attempts", (now,)
            )
            row = conn.execute(
                "SELECT * FROM work_events WHERE attempts < max_attempts AND "
                "((status='queued' AND available_at<=?) OR "
                " (status='leased' AND lease_until<=?)) AND "
                "(lane IS NULL OR lane NOT IN "
                " (SELECT lane FROM work_events WHERE status='leased' AND lease_until>? AND lane IS NOT NULL)) "
                "ORDER BY created_at,event_id LIMIT 1",
                (now, now, now),
            ).fetchone()
            if not row:
                conn.commit()
                return None
            token = secrets.token_urlsafe(24)
            lease_until = now + lease_seconds
            conn.execute(
                "UPDATE work_events SET status='leased',lease_owner=?,lease_token=?,lease_until=?,"
                "attempts=attempts+1 WHERE event_id=?",
                (worker_id, token, lease_until, row["event_id"]),
            )
            conn.commit()
            return WorkEvent(row["event_id"], row["kind"], json.loads(row["payload_json"]),
                             row["payload_hash"], row["retry_key"], row["attempts"] + 1,
                             token, lease_until, row["created_at"])
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def extend(self, event_id: str, lease_token: str, lease_seconds: float = 60,
               now: float | None = None) -> float:
        _validate_lease_seconds(lease_seconds)
        now = time.time() if now is None else now
        until = now + lease_seconds
        conn = self._conn()
        try:
            with conn:
                changed = conn.execute(
                    "UPDATE work_events SET lease_until=? WHERE event_id=? AND status='leased'"
                    " AND lease_token=? AND lease_until>?",
                    (until, event_id, lease_token, now),
                ).rowcount
            if not changed:
                raise LeaseLost("event lease is no longer owned")
            return until
        finally:
            conn.close()

    def complete(self, event_id: str, lease_token: str,
                 now: float | None = None) -> None:
        now = time.time() if now is None else now
        conn = self._conn()
        try:
            with conn:
                changed = conn.execute(
                    "UPDATE work_events SET status='completed',completed_at=?,lease_owner=NULL,"
                    "lease_token=NULL,lease_until=NULL,last_error=NULL WHERE event_id=?"
                    " AND status='leased' AND lease_token=? AND lease_until>?",
                    (now, event_id, lease_token, now),
                ).rowcount
            if not changed:
                raise LeaseLost("event lease is no longer owned")
        finally:
            conn.close()

    def fail(self, event_id: str, lease_token: str, error_code: str,
             retry_delay: float, now: float | None = None) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,99}", error_code):
            raise ValueError("error_code must be a non-sensitive machine code")
        if not 0 <= retry_delay <= 86400:
            raise ValueError("retry delay is invalid")
        now = time.time() if now is None else now
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attempts,max_attempts FROM work_events WHERE event_id=? AND status='leased'"
                " AND lease_token=? AND lease_until>?", (event_id, lease_token, now)
            ).fetchone()
            if not row:
                raise LeaseLost("event lease is no longer owned")
            status = "dead" if row["attempts"] >= row["max_attempts"] else "queued"
            conn.execute(
                "UPDATE work_events SET status=?,available_at=?,lease_owner=NULL,lease_token=NULL,"
                "lease_until=NULL,last_error=? WHERE event_id=?",
                (status, now + retry_delay, error_code, event_id),
            )
            conn.commit()
            return status
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_metric(self, name: str, value: float,
                      labels: dict[str, str | int | bool] | None = None,
                      now: float | None = None) -> str:
        if name not in ALLOWED_METRICS:
            raise ValueError("unknown operational metric")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("metric value must be numeric")
        labels = labels or {}
        if not isinstance(labels, dict) or len(labels) > 12:
            raise ValueError("invalid metric labels")
        clean: dict[str, str | int | bool] = {}
        for key, item in labels.items():
            if not SAFE_NAME.fullmatch(key) or SENSITIVE_LABEL.search(key):
                raise ValueError("sensitive or invalid metric label")
            if not isinstance(item, (str, int, bool)) or isinstance(item, str) and len(item) > 100:
                raise ValueError("invalid metric label value")
            clean[key] = item
        ident = str(uuid.uuid4())
        conn = self._conn()
        try:
            with conn:
                conn.execute("INSERT INTO metric_events VALUES (?,?,?,?,?)",
                             (ident, name, float(value), _json(clean),
                              time.time() if now is None else now))
        finally:
            conn.close()
        return ident

    def metrics(self, since: float = 0, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        conn = self._conn()
        try:
            statuses = {row["status"]: row["n"] for row in conn.execute(
                "SELECT status,COUNT(*) AS n FROM work_events GROUP BY status")}
            oldest = conn.execute(
                "SELECT MIN(created_at) FROM work_events WHERE status IN ('queued','leased')"
            ).fetchone()[0]
            grouped: dict[str, dict[str, float]] = {}
            for row in conn.execute(
                    "SELECT name,COUNT(*) n,SUM(value) total,AVG(value) average,MAX(value) maximum"
                    " FROM metric_events WHERE recorded_at>=? GROUP BY name", (since,)):
                grouped[row["name"]] = {key: row[key] for key in ("n", "total", "average", "maximum")}
            return {"queue": {"status_counts": statuses,
                              "oldest_pending_seconds": max(0, now - oldest) if oldest else 0},
                    "metrics": grouped, "since": since, "through": now}
        finally:
            conn.close()

    def dead_letters(self, limit: int = 50) -> list[dict[str, Any]]:
        """Inspect exhausted events. Payloads hold routing identifiers only."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be from 1 to 500")
        conn = self._conn()
        try:
            return [{"event_id": row["event_id"], "kind": row["kind"],
                     "retry_key": row["retry_key"], "payload": json.loads(row["payload_json"]),
                     "attempts": row["attempts"], "max_attempts": row["max_attempts"],
                     "last_error": row["last_error"], "created_at": row["created_at"]}
                    for row in conn.execute(
                        "SELECT * FROM work_events WHERE status='dead' "
                        "ORDER BY created_at DESC, event_id LIMIT ?", (limit,))]
        finally:
            conn.close()

    def requeue(self, event_id: str, now: float | None = None) -> None:
        """Operator action: give a dead-lettered event a fresh attempt budget."""
        now = time.time() if now is None else now
        conn = self._conn()
        try:
            with conn:
                changed = conn.execute(
                    "UPDATE work_events SET status='queued',attempts=0,available_at=?,"
                    "lease_owner=NULL,lease_token=NULL,lease_until=NULL,last_error=NULL "
                    "WHERE event_id=? AND status='dead'", (now, event_id)
                ).rowcount
            if not changed:
                raise OperationsError("event_not_dead")
        finally:
            conn.close()


def event_completed(app: sqlite3.Connection, kind: str, payload: dict[str, Any]) -> bool:
    """Verify against durable app state that an event's work actually finished.

    The adapter's self-attestation is never sufficient on its own; the app
    database is the source of truth for completion.
    """
    try:
        if kind == 'proposal_composition':
            row = app.execute('SELECT deal_id,version_no FROM proposal_versions WHERE id=?', (payload.get('version_id'),)).fetchone()
            if not row:
                return False
            latest = app.execute('SELECT id,status,outbound_status FROM proposal_versions WHERE deal_id=? ORDER BY version_no DESC LIMIT 1', (row['deal_id'],)).fetchone()
            composed = app.execute('SELECT 1 FROM proposal_compositions WHERE version_id=?', (latest['id'],)).fetchone()
            return bool(composed and latest['outbound_status'] == 'passed') or latest['status'] in ('rejected','superseded')
        if kind == "conversation_turn":
            inquiry, seq = payload.get("inquiry_id"), payload.get("through_sequence")
            row = app.execute("SELECT processed_seq FROM inquiries WHERE id=?", (inquiry,)).fetchone()
            if not row or not isinstance(seq, int) or row["processed_seq"] < seq:
                return False
            return app.execute(
                "SELECT 1 FROM inquiry_turns WHERE inquiry_id=? AND sender='assistant' AND seq>?",
                (inquiry, seq)).fetchone() is not None
        if kind == "media_update":
            return app.execute(
                "SELECT 1 FROM media_notifications WHERE run_id=? AND inquiry_id=? AND status='done'",
                (payload.get("analysis_id"), payload.get("inquiry_id"))).fetchone() is not None
        if kind == "creative_request":
            return app.execute(
                "SELECT 1 FROM offers WHERE deal_id=? AND source='change:' || ?",
                (payload.get("deal_id"), payload.get("request_id"))).fetchone() is not None
        if kind == "portfolio_search_request":
            return app.execute(
                "SELECT 1 FROM portfolio_search_requests WHERE request_id=? AND results_json IS NOT NULL",
                (payload.get("request_id"),)).fetchone() is not None
    except sqlite3.Error:
        return False
    return False


class AppWorkVerifier:
    """Read-only completion check against the studio's application database."""

    def __init__(self, app_db: Path):
        self.app_db = app_db

    def __call__(self, event: WorkEvent) -> bool:
        conn = sqlite3.connect(f"file:{self.app_db}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return event_completed(conn, event.kind, event.payload)
        finally:
            conn.close()


@dataclass(frozen=True)
class DispatchConfig:
    command: tuple[str, ...] | None = None
    url: str | None = None
    token_file: Path | None = None
    timeout_seconds: float = 30

    def __post_init__(self) -> None:
        if (self.command is None) == (self.url is None):
            raise ValueError("configure exactly one dispatch command or URL")
        if not 1 <= self.timeout_seconds <= 300:
            raise ValueError("dispatch timeout must be from 1 to 300 seconds")
        if self.command is not None:
            if not self.command or not Path(self.command[0]).is_absolute():
                raise ValueError("dispatch executable must be an explicit absolute path")
            if not Path(self.command[0]).is_file():
                raise ValueError("dispatch executable does not exist")
            if self.token_file is not None:
                raise ValueError("command credentials must be explicit command arguments, not token_file")
        else:
            parts = urlsplit(self.url or "")
            if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.fragment:
                raise ValueError("invalid dispatch URL")
            if parts.scheme == "http" and not _loopback(parts.hostname):
                raise ValueError("plain HTTP dispatch is allowed only on loopback")
            if self.token_file is None:
                raise ValueError("URL dispatch requires an explicit private token file")
            _read_token_file(self.token_file)


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _read_token_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise OperationsError("dispatch token file must be a regular file")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise OperationsError("dispatch token permissions are too broad")
    data = path.read_bytes()
    if not 1 <= len(data) <= 8192:
        raise OperationsError("dispatch token file is empty or too large")
    return data.decode("utf-8").strip()


class TurnDispatcher:
    """Claim one notification and invoke the configured authorized adapter."""

    def __init__(self, store: OperationsStore, config: DispatchConfig,
                 worker_id: str, lease_seconds: float | None = None,
                 verifier: Any = None):
        self.store = store
        self.config = config
        self.worker_id = worker_id
        self.verifier = verifier
        # A lease longer than the subprocess timeout prevents a second claimant
        # while the first adapter call is still running.
        self.lease_seconds = (config.timeout_seconds + 15
                              if lease_seconds is None else lease_seconds)
        _validate_lease_seconds(self.lease_seconds)
        if self.lease_seconds <= config.timeout_seconds:
            raise ValueError("lease_seconds must exceed dispatch timeout")

    def dispatch_one(self, now: float | None = None) -> bool:
        started = time.time()
        event = self.store.claim(self.worker_id, self.lease_seconds, now=now)
        if not event:
            return False
        envelope = {
            "contract": "bidpilot.authorized-worker.v1",
            "studio_id": self.store.paths.studio_id,
            "event_id": event.event_id,
            "kind": event.kind,
            "retry_key": event.retry_key,
            "attempt": event.attempt,
            "payload_sha256": event.payload_hash,
            "instruction": "Read pending local work through the configured scoped BidPilot MCP connector.",
        }
        # Renew the lease while a slow adapter runs so a long agent turn is
        # not redelivered to a second worker mid-flight.
        stop = threading.Event()

        def renew():
            while not stop.wait(max(1.0, self.lease_seconds / 3)):
                try:
                    self.store.extend(event.event_id, event.lease_token, self.lease_seconds)
                except LeaseLost:
                    return

        heartbeat = threading.Thread(target=renew, name="dispatch-lease", daemon=True)
        heartbeat.start()
        try:
            result = self._invoke(envelope)
            if result.get("ok") is not True or result.get("event_id") != event.event_id:
                raise OperationsError("adapter_rejected")
            if self.verifier is not None and not self.verifier(event):
                raise OperationsError("adapter_unverified")
            self.store.complete(event.event_id, event.lease_token)
            self.store.record_metric("dispatch_seconds", time.time() - started,
                                     {"kind": event.kind, "result": "completed"})
        except Exception as exc:
            error_code = _dispatch_error_code(exc)
            self.store.record_metric("dispatch_failures", 1,
                                     {"kind": event.kind, "error": error_code})
            delay = min(300, 2 ** min(event.attempt, 8))
            try:
                self.store.fail(event.event_id, event.lease_token, error_code, delay)
            except LeaseLost:
                pass
        finally:
            stop.set()
        return True

    def _invoke(self, envelope: dict[str, Any]) -> dict[str, Any]:
        body = _json(envelope).encode("utf-8")
        if self.config.command is not None:
            # Do not inherit the server environment (which may contain unrelated
            # secrets).  The adapter gets only its explicit argv and stdin.
            env = {"PATH": os.defpath, "LANG": "C.UTF-8"}
            proc = subprocess.run(self.config.command, input=body, capture_output=True,
                                  timeout=self.config.timeout_seconds, env=env,
                                  cwd=self.store.paths.workspace, check=False)
            if proc.returncode != 0:
                # An adapter may report a machine-readable reason on stdout,
                # e.g. {"error": "awaiting_agent"} while work is still pending.
                try:
                    reported = json.loads(proc.stdout.decode("utf-8")).get("error")
                    if isinstance(reported, str) and re.fullmatch(r"[a-z][a-z0-9_.-]{0,99}", reported):
                        raise OperationsError(reported)
                except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                    pass
                raise OperationsError("adapter_exit")
            if len(proc.stdout) > 65536:
                raise OperationsError("adapter_response_too_large")
            return json.loads(proc.stdout.decode("utf-8"))
        token = _read_token_file(self.config.token_file)  # reload for rotation
        req = request.Request(self.config.url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
            "Idempotency-Key": envelope["event_id"],
        })
        # Never forward the bearer token to a redirect target, even on the same host.
        opener = request.build_opener(_RefuseRedirects())
        with opener.open(req, timeout=self.config.timeout_seconds) as response:
            data = response.read(65537)
        if len(data) > 65536:
            raise OperationsError("adapter_response_too_large")
        return json.loads(data.decode("utf-8"))


def _dispatch_error_code(exc: Exception) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "adapter_timeout"
    if isinstance(exc, json.JSONDecodeError):
        return "adapter_invalid_json"
    if isinstance(exc, OperationsError) and re.fullmatch(r"[a-z_]+", str(exc)):
        return str(exc)
    return "adapter_unavailable"


def create_snapshot(paths: StudioPaths, archive: str | os.PathLike[str],
                    now: float | None = None) -> dict[str, Any]:
    """Create and immediately verify a snapshot. Credentials are never included."""
    archive = Path(archive).resolve()
    archive.parent.mkdir(parents=True, exist_ok=True)
    if _contained(archive, paths.workspace):
        raise OperationsError("snapshot destination must be outside the studio workspace")
    with tempfile.TemporaryDirectory(prefix="bidpilot-snapshot-") as raw:
        stage = Path(raw)
        _backup_sqlite(paths.app_db, stage / "app.db")
        _backup_sqlite(paths.operations_db, stage / "operations.db")
        media_target = stage / "app.db.media"
        _copy_media_stable(paths.media_dir, media_target)
        entries = []
        for item in sorted(p for p in stage.rglob("*") if p.is_file()):
            rel = item.relative_to(stage).as_posix()
            entries.append({"path": rel, "size": item.stat().st_size,
                            "sha256": _digest(item.read_bytes())})
        manifest = {"format": "bidpilot-snapshot-v1", "studio_id": paths.studio_id,
                    "created_at": time.time() if now is None else now,
                    "credentials_included": False, "files": entries}
        (stage / "manifest.json").write_text(_json(manifest), encoding="utf-8")
        temp_archive = archive.with_name(archive.name + ".tmp-" + secrets.token_hex(5))
        try:
            with zipfile.ZipFile(temp_archive, "w", zipfile.ZIP_DEFLATED) as out:
                for item in sorted(p for p in stage.rglob("*") if p.is_file()):
                    out.write(item, item.relative_to(stage).as_posix())
            os.replace(temp_archive, archive)
        finally:
            temp_archive.unlink(missing_ok=True)
    verified = verify_snapshot(archive, expected_studio_id=paths.studio_id)
    verified["archive_sha256"] = _digest(archive.read_bytes())
    return verified


def _backup_sqlite(source: Path, target: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise OperationsError("SQLite source is missing or unsafe")
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise OperationsError("SQLite snapshot failed integrity check")
    finally:
        dst.close()
        src.close()


def _copy_media_stable(source: Path, target: Path) -> None:
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not source.exists():
        return
    for item in sorted(source.rglob("*")):
        if item.is_symlink():
            raise OperationsError("private media must not contain symlinks")
        rel = item.relative_to(source)
        destination = target / rel
        if item.is_dir():
            destination.mkdir(mode=0o700, exist_ok=True)
            continue
        before = item.stat()
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(item, destination)
        after = item.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise OperationsError("private media changed during snapshot")
        os.chmod(destination, 0o600)


def verify_snapshot(archive: str | os.PathLike[str],
                    expected_studio_id: str | None = None) -> dict[str, Any]:
    archive = Path(archive)
    with zipfile.ZipFile(archive, "r") as bundle:
        names = bundle.namelist()
        if names.count("manifest.json") != 1 or len(names) != len(set(names)):
            raise OperationsError("snapshot manifest is missing or duplicate")
        for info in bundle.infolist():
            path = Path(info.filename)
            if path.is_absolute() or ".." in path.parts or info.is_dir():
                raise OperationsError("snapshot contains an unsafe entry")
            mode = info.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                raise OperationsError("snapshot contains a symlink")
        manifest = json.loads(bundle.read("manifest.json"))
        if manifest.get("format") != "bidpilot-snapshot-v1" or manifest.get("credentials_included") is not False:
            raise OperationsError("unsupported snapshot format")
        if expected_studio_id and manifest.get("studio_id") != expected_studio_id:
            raise OperationsError("snapshot belongs to another studio")
        listed = {entry["path"]: entry for entry in manifest.get("files", [])}
        if set(names) != set(listed) | {"manifest.json"}:
            raise OperationsError("snapshot file list does not match manifest")
        for name, entry in listed.items():
            data = bundle.read(name)
            if len(data) != entry["size"] or _digest(data) != entry["sha256"]:
                raise OperationsError("snapshot digest mismatch")
        return {"studio_id": manifest["studio_id"], "created_at": manifest["created_at"],
                "file_count": len(listed), "verified": True}


def restore_snapshot(paths: StudioPaths, archive: str | os.PathLike[str]) -> dict[str, Any]:
    """Verify then restore state atomically, preserving the current credentials directory.

    The caller must stop the web service and workers first.  A non-expired lease
    is rejected as a useful guard against restoring over an active worker.
    """
    verified = verify_snapshot(archive, expected_studio_id=paths.studio_id)
    ops = _open_db(paths.operations_db, OPERATIONS_SCHEMA)
    try:
        active = ops.execute(
            "SELECT 1 FROM work_events WHERE status='leased' AND lease_until>? LIMIT 1",
            (time.time(),),
        ).fetchone()
    finally:
        ops.close()
    if active:
        raise OperationsError("cannot restore while a work lease is active")
    parent = paths.workspace.parent
    stage = Path(tempfile.mkdtemp(prefix=paths.workspace.name + ".restore-", dir=parent))
    rollback = parent / (paths.workspace.name + ".rollback-" + secrets.token_hex(5))
    try:
        os.chmod(stage, 0o700)
        with zipfile.ZipFile(archive, "r") as bundle:
            for entry in json.loads(bundle.read("manifest.json"))["files"]:
                target = stage / entry["path"]
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with bundle.open(entry["path"]) as src, open(target, "xb") as dst:
                    shutil.copyfileobj(src, dst)
                os.chmod(target, 0o600)
        for database in (stage / "app.db", stage / "operations.db"):
            conn = sqlite3.connect(database)
            try:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise OperationsError("restored SQLite file failed integrity check")
            finally:
                conn.close()
        credentials = paths.credentials_dir
        if credentials.exists():
            shutil.copytree(credentials, stage / "credentials", symlinks=False)
        else:
            _private_directory(stage / "credentials")
        os.rename(paths.workspace, rollback)
        try:
            os.rename(stage, paths.workspace)
        except Exception:
            os.rename(rollback, paths.workspace)
            raise
        shutil.rmtree(rollback)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return verified


def apply_retention(registry: StudioRegistry, studio_id: str,
                    now: float | None = None, dry_run: bool = True) -> dict[str, Any]:
    """Expire inquiry links and delete expired original/derived private media."""
    now = time.time() if now is None else now
    paths = registry.paths(studio_id)
    policy = registry.policy(studio_id)
    media_cutoff = now - policy.media_days * 86400
    link_cutoff = now - policy.inquiry_link_days * 86400
    conn = _open_db(paths.app_db)
    assets: list[sqlite3.Row] = []
    expired_inquiries = expired_proposals = 0
    try:
        if _has_table(conn, "reference_assets"):
            assets = conn.execute(
                "SELECT id,path FROM reference_assets WHERE created_at<? ORDER BY id", (media_cutoff,)
            ).fetchall()
        if _has_table(conn, "inquiries"):
            expired_inquiries = conn.execute(
                "SELECT COUNT(*) FROM inquiries WHERE revoked=0 AND created_at<?", (link_cutoff,)
            ).fetchone()[0]
        if _has_table(conn, "proposal_versions"):
            expired_proposals = conn.execute(
                "SELECT COUNT(*) FROM proposal_versions WHERE token_hash IS NOT NULL"
                " AND published_at IS NOT NULL AND published_at<?", (link_cutoff,)
            ).fetchone()[0]
        result = {"dry_run": dry_run, "media_assets": len(assets),
                  "inquiry_links": expired_inquiries, "proposal_links": expired_proposals}
        if dry_run:
            return result
        # Validate every path before changing either files or rows.
        for asset in assets:
            if not _contained(Path(asset["path"]), paths.media_dir):
                raise OperationsError("reference asset path escapes private media")
        quarantine = paths.workspace / (".retention-" + secrets.token_hex(5))
        _private_directory(quarantine)
        moved: list[tuple[Path, Path]] = []
        run_ids: list[str] = []
        try:
            if assets and _has_table(conn, "reference_runs"):
                marks = ",".join("?" for _ in assets)
                run_ids = [r[0] for r in conn.execute(
                    f"SELECT id FROM reference_runs WHERE asset_id IN ({marks})",
                    tuple(a["id"] for a in assets),
                )]
            for source in [Path(a["path"]) for a in assets] + [paths.media_dir / r for r in run_ids]:
                if source.exists():
                    if source.is_symlink():
                        raise OperationsError("retained media path is a symlink")
                    target = quarantine / (str(len(moved)) + "-" + source.name)
                    os.rename(source, target)
                    moved.append((source, target))
            conn.execute("BEGIN IMMEDIATE")
            if run_ids and _has_table(conn, "reference_observations"):
                marks = ",".join("?" for _ in run_ids)
                conn.execute(f"DELETE FROM reference_observations WHERE run_id IN ({marks})", run_ids)
                if _has_table(conn, "media_notifications"):
                    conn.execute(f"DELETE FROM media_notifications WHERE run_id IN ({marks})", run_ids)
            if assets:
                marks = ",".join("?" for _ in assets)
                ids = tuple(a["id"] for a in assets)
                if _has_table(conn, "reference_runs"):
                    conn.execute(f"DELETE FROM reference_runs WHERE asset_id IN ({marks})", ids)
                conn.execute(f"DELETE FROM reference_assets WHERE id IN ({marks})", ids)
            if _has_table(conn, "inquiries"):
                conn.execute("UPDATE inquiries SET revoked=1 WHERE revoked=0 AND created_at<?", (link_cutoff,))
            if _has_table(conn, "proposal_versions"):
                conn.execute("UPDATE proposal_versions SET token_hash=NULL WHERE token_hash IS NOT NULL"
                             " AND published_at IS NOT NULL AND published_at<?", (link_cutoff,))
            if _has_table(conn, "inquiry_proposal_links"):
                conn.execute("DELETE FROM inquiry_proposal_links WHERE version_id IN"
                             " (SELECT id FROM proposal_versions WHERE token_hash IS NULL)")
            conn.commit()
        except Exception:
            conn.rollback()
            for source, target in reversed(moved):
                if target.exists():
                    os.rename(target, source)
            raise
        finally:
            shutil.rmtree(quarantine, ignore_errors=True)
        return result
    finally:
        conn.close()


__all__ = [
    "ALLOWED_METRICS", "DispatchConfig", "LeaseLost", "OperationsError",
    "OperationsStore", "RetentionPolicy", "RetryConflict", "StudioPaths",
    "StudioRegistry", "TurnDispatcher", "apply_retention", "create_snapshot",
    "restore_snapshot", "verify_snapshot",
]
