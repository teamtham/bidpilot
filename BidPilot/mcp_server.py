"""BidPilot scoped STDIO MCP server.

Narrow, sample-data-only bridge over the existing Python service layer
(core.py + db.py) and the unchanged engine. Exposes exactly:
  claim_intake, get_deal, evaluate_offer, save_draft, check_draft,
  run_trace, negotiation_payload

Deliberately NOT exposed (founder-only, dashboard-authenticated): approve,
reject, rotate_token, confirm_booking, client_accept, close_deal, and any
arbitrary SQL / filesystem / seed-write / shell capability.

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout (MCP stdio).
Logs go to stderr only; no credentials are read, stored or logged.

Run: python3 mcp_server.py [db_path]
"""
import hashlib
import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import db  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "bidpilot", "version": "1.0.0"}
DB_PATH = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "BIDPILOT_MCP_DB", str(Path(__file__).resolve().parent / "bidpilot.db"))


def log(*args):
    print(*args, file=sys.stderr, flush=True)


class ToolError(Exception):
    """Tool-level failure reported as an MCP tool result error."""


def _connect():
    return db.connect(DB_PATH)


# ---- guards ---------------------------------------------------------------

def _sample_deal(conn, deal_id):
    deal = conn.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
    if deal is None:
        raise ToolError(f"deal_not_found:{deal_id}")
    if not deal["sample_data"]:
        raise ToolError("non_sample_record_rejected: MCP tools accept sample data only")
    return deal


def _offer_of_deal(conn, deal_id, offer_id):
    offer = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    if offer is None:
        raise ToolError(f"offer_not_found:{offer_id}")
    if offer["deal_id"] != deal_id:
        raise ToolError("offer_ownership_mismatch: offer does not belong to this deal")
    deal = conn.execute("SELECT sample_data FROM deals WHERE id=?", (deal_id,)).fetchone()
    if deal is None or not deal["sample_data"]:
        raise ToolError("non_sample_record_rejected: MCP tools accept sample data only")
    return offer


def _version_of_deal(conn, version_id):
    version = conn.execute(
        "SELECT * FROM proposal_versions WHERE id=?", (version_id,)).fetchone()
    if version is None:
        raise ToolError(f"version_not_found:{version_id}")
    _sample_deal(conn, version["deal_id"])
    return version


def _version_public(version):
    """Public projection of a version: never token_hash / accepted_at internals."""
    return {
        "version_id": version["id"], "deal_id": version["deal_id"],
        "offer_id": version["offer_id"], "version_no": version["version_no"],
        "status": version["status"], "outbound_status": version["outbound_status"],
        "draft_body": version["draft_body"],
    }


# ---- tools ----------------------------------------------------------------

def t_claim_intake(args):
    for field in ("source", "source_id", "client", "raw_inquiry", "offer_request"):
        if field not in args:
            raise ToolError(f"missing_field:{field}")
    if args["source"] not in ("hashmeta", "manual"):
        raise ToolError("source_not_allowed: use 'hashmeta' or 'manual' sample sources")
    for attempt in range(3):  # concurrent duplicate claims converge, never duplicate
        conn = _connect()
        try:
            deal_id, offer_id, created = core.claim_or_resume(
                conn, args["source"], args["source_id"], args["client"],
                args["raw_inquiry"], args["offer_request"], require_sample=True)
            conn.close()
            return {"deal_id": deal_id, "offer_id": offer_id, "created": created,
                    "note": "Duplicate (source, source_id) resumes the same deal and"
                            " reuses its first offer; raw evidence is preserved."}
        except ValueError as exc:
            conn.close()
            raise ToolError(str(exc))
        except (sqlite3.IntegrityError, sqlite3.OperationalError):
            conn.close()
            if attempt == 2:
                raise ToolError("concurrent_claim_conflict")
            continue


def t_get_deal(args):
    conn = _connect()
    try:
        deal = _sample_deal(conn, args["deal_id"])
        offers = conn.execute(
            "SELECT id, round, as_of, from_version_id, created_at FROM offers"
            " WHERE deal_id=? ORDER BY round", (args["deal_id"],)).fetchall()
        versions = conn.execute(
            "SELECT * FROM proposal_versions WHERE deal_id=? ORDER BY version_no",
            (args["deal_id"],)).fetchall()
        current_offer = None
        if offers:
            row = conn.execute("SELECT * FROM offers WHERE id=?",
                               (offers[-1]["id"],)).fetchone()
            current_offer = {"id": row["id"], "round": row["round"],
                             "as_of": row["as_of"],
                             "offer": db.uj(row["offer_json"]),
                             "evidence": db.uj(row["evidence_json"]),
                             "comparable_ids": db.uj(row["comparable_ids_json"])}
        return {
            "deal": {"id": deal["id"], "source": deal["source"],
                     "source_id": deal["source_id"], "client": deal["client"],
                     "raw_inquiry": deal["raw_inquiry"], "status": deal["status"],
                     "sample_data": bool(deal["sample_data"])},
            "current_offer": current_offer,
            "versions": [_version_public(v) for v in versions],
        }
    finally:
        conn.close()


def t_evaluate_offer(args):
    conn = _connect()
    try:
        _offer_of_deal(conn, args["deal_id"], args["offer_id"])
        eval_id, version_id = core.evaluate(conn, args["deal_id"], args["offer_id"])
        row = conn.execute("SELECT * FROM evaluations WHERE id=?", (eval_id,)).fetchone()
        result = db.uj(row["result_json"])
        version = _version_public(conn.execute(
            "SELECT * FROM proposal_versions WHERE id=?", (version_id,)).fetchone())
        # public option terms exactly as the engine emitted them
        return {"evaluation_id": eval_id, "verdict": result["verdict"],
                "reason_codes": result["reason_codes"],
                "estimate": result.get("estimate"),
                "options": result.get("options"),
                "draft_version": version,
                "note": "Idempotent: a re-run returns the stored evaluation"
                        " without re-running the engine."}
    finally:
        conn.close()


def t_save_draft(args):
    if not str(args.get("request_id", "")).strip():
        raise ToolError("missing_field:request_id")
    if not str(args.get("draft_body", "")).strip():
        raise ToolError("missing_field:draft_body")
    request_id = str(args["request_id"]).strip()
    version_id = str(args["version_id"]).strip() if args.get("version_id") else ""
    if not version_id:
        raise ToolError("missing_field:version_id")
    body = str(args["draft_body"])
    payload_hash = hashlib.sha256(json.dumps(
        {"tool": "save_draft", "version_id": version_id, "draft_body": body},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        .encode("utf-8")).hexdigest()
    # Durable exactly-once semantics: the unique operation record and the new
    # proposal version are written in ONE write transaction. Identical retries
    # replay the stored result; a reused request_id with a different payload
    # is rejected; concurrent retries converge on the single first result.
    for attempt in range(3):
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            op = conn.execute(
                "SELECT * FROM mcp_operations WHERE request_id=?",
                (request_id,)).fetchone()
            if op is not None:
                if op["payload_hash"] != payload_hash:
                    conn.rollback()
                    conn.close()
                    raise ToolError(
                        "request_id_conflict: this request_id was already used with"
                        " a different version_id/draft_body payload")
                result = db.uj(op["result_json"])
                conn.rollback()
                conn.close()
                return result
            version = _version_of_deal(conn, version_id)
            if version["status"] in ("published", "confirmed"):
                conn.rollback()
                conn.close()
                raise ToolError(
                    f"record_not_editable: version status is {version['status']};"
                    " approved or published records are not modified via MCP")
            new_id = core.save_draft(conn, version["id"], body, commit=False)
            new_version = _version_public(conn.execute(
                "SELECT * FROM proposal_versions WHERE id=?", (new_id,)).fetchone())
            result = {"draft_version": new_version,
                      "note": "Edit creates a new version; outbound status resets to"
                              " unchecked and a fresh check is required."
                              " Retrying with the same request_id and payload returns"
                              " this same version."}
            conn.execute(
                "INSERT INTO mcp_operations (request_id, tool, payload_hash,"
                " result_json, created_at) VALUES (?,?,?,?,?)",
                (request_id, "save_draft", payload_hash, db.j(result), db.now()))
            conn.commit()
            conn.close()
            return result
        except ToolError:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            conn.close()
            raise
        except (sqlite3.IntegrityError, sqlite3.OperationalError):
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            conn.close()
            if attempt == 2:
                raise ToolError("concurrent_save_conflict")
            continue


def t_negotiation_payload(args):
    conn = _connect()
    try:
        ev = conn.execute("SELECT * FROM evaluations WHERE id=?",
                          (args["evaluation_id"],)).fetchone()
        if ev is None:
            raise ToolError(f"evaluation_not_found:{args['evaluation_id']}")
        offer = conn.execute("SELECT * FROM offers WHERE id=?",
                             (ev["offer_id"],)).fetchone()
        _sample_deal(conn, offer["deal_id"])
        option_rows = conn.execute(
            "SELECT * FROM options WHERE evaluation_id=? ORDER BY rowid",
            (ev["id"],)).fetchall()
        versions = conn.execute(
            "SELECT draft_body, outbound_status FROM proposal_versions"
            " WHERE offer_id=? ORDER BY version_no", (offer["id"],)).fetchall()
        # Outward-safe client message: the latest draft that passed the
        # outbound check. No floor, capacity, estimate or run history is
        # ever included in this projection.
        message = next((v["draft_body"] for v in reversed(versions)
                        if v["outbound_status"] == "passed"), None)
        result = {"evaluation_id": ev["id"], "offer_id": offer["id"],
                  "deal_id": offer["deal_id"], "verdict": ev["verdict"],
                  "options": [db.uj(r["public_terms_json"]) for r in option_rows],
                  "client_message": message}
        if message is None:
            result["note"] = ("No draft for this evaluation has passed the outbound"
                              " check yet; only the option terms above are"
                              " outward-safe.")
        return result
    finally:
        conn.close()


def t_check_draft(args):
    conn = _connect()
    try:
        version = _version_of_deal(conn, args["version_id"])
        outcome = core.check_draft(conn, version["id"])
        after = _version_public(conn.execute(
            "SELECT * FROM proposal_versions WHERE id=?", (version["id"],)).fetchone())
        return {"outcome": outcome, "version": after}
    finally:
        conn.close()


def t_run_trace(args):
    conn = _connect()
    try:
        deal = _sample_deal(conn, args["deal_id"])
        steps = conn.execute(
            "SELECT step_key, status, offer_id, private_note, started_at,"
            " finished_at FROM run_steps WHERE deal_id=? ORDER BY rowid",
            (args["deal_id"],)).fetchall()
        messages = conn.execute(
            "SELECT direction, body, request_id, created_at FROM messages"
            " WHERE deal_id=? ORDER BY rowid", (args["deal_id"],)).fetchall()
        versions = conn.execute(
            "SELECT id, offer_id, version_no, status, outbound_status,"
            " checked_digest FROM proposal_versions WHERE deal_id=?"
            " ORDER BY version_no", (args["deal_id"],)).fetchall()
        return {"deal": {"id": deal["id"], "client": deal["client"],
                         "status": deal["status"]},
                "steps": [dict(s) for s in steps],
                "messages": [dict(m) for m in messages],
                "versions": [dict(v) for v in versions]}
    finally:
        conn.close()


TOOLS = [
    ("claim_intake", t_claim_intake,
     "Claim or resume a synthetic sample inquiry by stable (source, source_id)."
     " Requires the engine's structured offer_request (as_of, offer fields,"
     " comparable_ids, founder-supplied direct costs). Duplicate claims resume"
     " the same deal and reuse its first offer."),
    ("get_deal", t_get_deal,
     "Read a scoped sample deal with its current offer (structured terms and raw"
     " evidence) and version history. Sample-data records only."),
    ("evaluate_offer", t_evaluate_offer,
     "Run the unchanged BidPilot Python evaluator for an offer owned by the"
     " given deal; idempotent. Returns verdict, reason codes, estimate,"
     " engine options and the draft version it created."),
    ("save_draft", t_save_draft,
     "Record an agent-generated draft body as a NEW proposal version of the"
     " same offer, exactly once per stable caller-supplied request_id:"
     " identical retries return the original version, a reused request_id with"
     " different payload is rejected. Editing invalidates any prior check;"
     " a fresh check is required."),
    ("check_draft", t_check_draft,
     "Run the engine's outbound check on a draft version against the exact"
     " engine option set. Blocks private values and other-client names."),
    ("run_trace", t_run_trace,
     "Read the private, append-only run trace (steps, messages, versions) for"
     " a sample deal. Sample-data records only."),
    ("negotiation_payload", t_negotiation_payload,
     "Read the outward-safe negotiation material for one evaluation: the"
     " engine option terms and the client message from the latest draft that"
     " passed the outbound check. Private values (floor, capacity, effort,"
     " history) are never included."),
]

TOOLS_BY_NAME = {name: fn for name, fn, _ in TOOLS}


# ---- JSON-RPC dispatch ------------------------------------------------------

def handle(msg):
    if "id" not in msg:  # notification (e.g. notifications/initialized)
        return None
    method = msg.get("method")
    msg_id = msg["id"]
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [
            {"name": name, "description": desc,
             "inputSchema": {"type": "object",
                             "properties": _schema(name)}}
            for name, _, desc in TOOLS]}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        fn = TOOLS_BY_NAME.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": -32602, "message": f"unknown_tool:{name}."
                " Approve/release/token/booking actions are not exposed via MCP;"
                " they stay in the authenticated founder dashboard."}}
        try:
            result = fn(params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(result, ensure_ascii=False,
                                                 indent=2)}]}}
        except ToolError as exc:
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "isError": True,
                "content": [{"type": "text", "text": str(exc)}]}}
        except Exception:
            log("tool_error", name, traceback.format_exc())
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "isError": True,
                "content": [{"type": "text",
                             "text": "internal_error"}]}}
    return {"jsonrpc": "2.0", "id": msg_id, "error": {
        "code": -32601, "message": f"method_not_found:{method}"}}


def _schema(name):
    if name == "claim_intake":
        return {"source": {"type": "string"}, "source_id": {"type": "string"},
                "client": {"type": "string"}, "raw_inquiry": {"type": "string"},
                "offer_request": {"type": "object"}}
    if name == "get_deal":
        return {"deal_id": {"type": "string"}}
    if name == "evaluate_offer":
        return {"deal_id": {"type": "string"}, "offer_id": {"type": "string"}}
    if name == "save_draft":
        return {"version_id": {"type": "string"}, "draft_body": {"type": "string"},
                "request_id": {"type": "string"}}
    if name == "check_draft":
        return {"version_id": {"type": "string"}}
    if name == "run_trace":
        return {"deal_id": {"type": "string"}}
    if name == "negotiation_payload":
        return {"evaluation_id": {"type": "string"}}
    return {}


def main():
    log(f"bidpilot MCP server starting; db={DB_PATH}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {
                "code": -32700, "message": "parse_error"}}
        else:
            resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
