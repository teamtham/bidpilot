#!/usr/bin/env python3
"""Authorized QwenWork dispatch adapter for the scheduled-consumer deployment.

Contract: bidpilot.authorized-worker.v1. Reads one dispatch envelope on stdin
and answers on stdout. This adapter never writes business records, never
replies to buyers and never invents agent output. Agent reasoning runs in
QwenWork itself (the scheduled pending-work consumer configured by the
operator); this adapter only verifies, against the durable application
database, whether the event's work has actually been complete by that
authorized consumer.

Outcomes:
* work verified complete -> {"ok": true, "event_id": ...}, exit 0
* work still pending     -> {"ok": false, "error": "awaiting_agent"}, exit 3
  (the dispatcher requeues with backoff; the durable recovery scan keeps the
  wake alive until the consumer processes it or the attempt budget expires)

The envelope carries routing identifiers only. Identifiers are recovered from
the stable retry key; no conversation body ever crosses this boundary.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import operations


def payload_from_retry_key(conn, kind, retry_key):
    parts = retry_key.split(":")
    if kind == 'proposal_composition' and len(parts) == 2 and parts[0] == 'compose':
        row = conn.execute('SELECT deal_id FROM proposal_versions WHERE id=?', (parts[1],)).fetchone()
        return {'version_id': parts[1], 'deal_id': row['deal_id']} if row else None
    if kind == "conversation_turn" and len(parts) == 3 and parts[0] == "conversation":
        try:
            return {"inquiry_id": parts[1], "through_sequence": int(parts[2])}
        except ValueError:
            return None
    if kind == "media_update" and len(parts) == 4 and parts[0] == "media":
        return {"inquiry_id": parts[1], "analysis_id": parts[2], "status": parts[3]}
    if kind == "creative_request" and retry_key.startswith("creative:"):
        request_id = retry_key.split(":", 1)[1]
        row = conn.execute("SELECT deal_id,version_id FROM messages WHERE request_id=?",
                           (request_id,)).fetchone()
        if not row:
            return None
        return {"deal_id": row["deal_id"], "version_id": row["version_id"],
                "request_id": request_id}
    if kind == "portfolio_search_request" and retry_key.startswith("portfolio-search:"):
        request_id = retry_key.split(":", 1)[1]
        row = conn.execute("SELECT version_id FROM portfolio_search_requests WHERE request_id=?",
                           (request_id,)).fetchone()
        if not row:
            return None
        return {"request_id": request_id, "version_id": row["version_id"]}
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-db", required=True, help="Absolute path to the studio app.db")
    args = parser.parse_args(argv)
    app_db = Path(args.app_db)
    if not app_db.is_absolute() or not app_db.is_file():
        print(json.dumps({"ok": False, "error": "adapter_misconfigured"}))
        return 2
    try:
        envelope = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(json.dumps({"ok": False, "error": "adapter_invalid_envelope"}))
        return 2
    if (not isinstance(envelope, dict)
            or envelope.get("contract") != "bidpilot.authorized-worker.v1"
            or not isinstance(envelope.get("event_id"), str)
            or not isinstance(envelope.get("kind"), str)
            or not isinstance(envelope.get("retry_key"), str)):
        print(json.dumps({"ok": False, "error": "adapter_invalid_envelope"}))
        return 2
    event_id = envelope["event_id"]
    conn = sqlite3.connect(f"file:{app_db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        payload = payload_from_retry_key(conn, envelope["kind"], envelope["retry_key"])
        if payload is None:
            print(json.dumps({"ok": False, "event_id": event_id, "error": "adapter_unknown_work"}))
            return 3
        if operations.event_completed(conn, envelope["kind"], payload):
            print(json.dumps({"ok": True, "event_id": event_id}))
            return 0
    finally:
        conn.close()
    print(json.dumps({"ok": False, "event_id": event_id, "error": "awaiting_agent"}))
    return 3


if __name__ == "__main__":
    raise SystemExit(main())