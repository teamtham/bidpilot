"""BidPilot scoped STDIO MCP server.

Narrow, sample-data-only bridge over the existing Python service layer
(core.py + db.py) and the unchanged engine. Exposes:
  claim_intake, get_deal, evaluate_offer, save_draft, check_draft,
  run_trace, negotiation_payload, record_step, save_agent_output,
  get_scoping_history, list_pending_work, submit_change_offer

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
import re
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
    if args["source"] not in ("hashmeta", "manual", "site"):
        raise ToolError("source_not_allowed: use 'hashmeta', 'manual' or 'site' sample sources")
    if "evidence" in args and not isinstance(args["evidence"], dict):
        raise ToolError("invalid_evidence")
    request_id = args.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
        raise ToolError("invalid_field:request_id")
    payload_hash = hashlib.sha256(json.dumps(
        {"tool": "claim_intake", "args": args}, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    for attempt in range(3):
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if args["source"] == "site":
                inbox = conn.execute(
                    "SELECT sample_data FROM inbox WHERE source=? AND source_id=?",
                    (args["source"], args["source_id"])).fetchone()
                if inbox is not None and not inbox["sample_data"]:
                    raise ToolError("non_sample_record_rejected")
                if inbox is None and re.fullmatch(r"site-[0-9a-f]{16}", args["source_id"]):
                    existing = conn.execute(
                        "SELECT 1 FROM deals WHERE source=? AND source_id=?",
                        (args["source"], args["source_id"])).fetchone()
                    if existing is None:
                        raise ToolError("inbox_inquiry_not_found")
            if request_id is not None:
                op = conn.execute("SELECT * FROM mcp_operations WHERE request_id=?",
                                  (request_id,)).fetchone()
                if op:
                    if op["payload_hash"] != payload_hash:
                        raise ToolError("request_id_conflict")
                    _sample_deal(conn, op["deal_id"])
                    return db.uj(op["result_json"])
            deal_id, offer_id, created = core.claim_or_resume(
                conn, args["source"], args["source_id"], args["client"],
                args["raw_inquiry"], args["offer_request"], require_sample=True, commit=False)
            if "evidence" in args:
                conn.execute(
                    "INSERT OR IGNORE INTO agent_outputs "
                    "(id, deal_id, offer_id, kind, payload_json, request_id, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (db.new_id(), deal_id, offer_id, "intake", db.j(args["evidence"]),
                     "claim-intake:" + deal_id, db.now()))
            result = {"deal_id": deal_id, "offer_id": offer_id, "created": created,
                      "note": "Duplicate (source, source_id) resumes the same deal and"
                              " reuses its first offer; raw evidence is preserved."}
            if request_id is not None:
                conn.execute(
                    "INSERT INTO mcp_operations "
                    "(request_id, tool, payload_hash, result_json, created_at, deal_id) "
                    "VALUES (?,?,?,?,?,?)",
                    (request_id, "claim_intake", payload_hash, db.j(result), db.now(), deal_id))
            conn.commit()
            return result
        except ValueError as exc:
            conn.rollback()
            raise ToolError(str(exc))
        except (sqlite3.IntegrityError, sqlite3.OperationalError):
            conn.rollback()
            if attempt == 2:
                raise ToolError("concurrent_claim_conflict")
        finally:
            conn.close()


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
        conn.execute("BEGIN IMMEDIATE")
        _offer_of_deal(conn, args["deal_id"], args["offer_id"])
        eval_id, version_id = core.evaluate(conn, args["deal_id"], args["offer_id"], commit=False)
        row = conn.execute("SELECT * FROM evaluations WHERE id=?", (eval_id,)).fetchone()
        result = db.uj(row["result_json"])
        version = _version_public(conn.execute(
            "SELECT * FROM proposal_versions WHERE id=?", (version_id,)).fetchone())
        # public option terms exactly as the engine emitted them
        conn.commit()
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
                " result_json, created_at, deal_id) VALUES (?,?,?,?,?,?)",
                (request_id, "save_draft", payload_hash, db.j(result), db.now(), version["deal_id"]))
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
            "SELECT * FROM proposal_versions WHERE offer_id=? ORDER BY version_no",
            (offer["id"],)).fetchall()
        # Outward-safe client message: the latest draft that passed the
        # outbound check. No floor, capacity, estimate or run history is
        # ever included in this projection.
        message = next((v["draft_body"] for v in reversed(versions)
                        if v["outbound_status"] == "passed"
                        and v["checked_digest"] == core.digest(v, core.options_of(conn, v))
                        and not core.option_set_errors(conn, v)), None)
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
        conn.execute("BEGIN IMMEDIATE")
        version = _version_of_deal(conn, args["version_id"])
        outcome = core.check_draft(conn, version["id"], commit=False)
        after = _version_public(conn.execute(
            "SELECT * FROM proposal_versions WHERE id=?", (version["id"],)).fetchone())
        conn.commit()
        return {"outcome": outcome, "version": after}
    finally:
        conn.close()


def t_run_trace(args):
    conn = _connect()
    try:
        deal = _sample_deal(conn, args["deal_id"])
        steps = conn.execute(
            "SELECT step_key, status, offer_id, private_note, started_at,"
            ' finished_at, "trigger" FROM run_steps WHERE deal_id=? ORDER BY rowid',
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
                "versions": [dict(v) for v in versions],
                "agent_outputs": [{"kind": r["kind"], "created_at": r["created_at"],
                                   "payload": db.uj(r["payload_json"])}
                                  for r in conn.execute("SELECT * FROM agent_outputs WHERE deal_id=? ORDER BY rowid", (args["deal_id"],))]}
    finally:
        conn.close()


def _write_operation(tool, args, operation):
    request_id = args.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ToolError("missing_field:request_id")
    payload_hash = hashlib.sha256(json.dumps({"tool": tool, "args": args}, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        op = conn.execute("SELECT * FROM mcp_operations WHERE request_id=?", (request_id,)).fetchone()
        if op:
            if op["payload_hash"] != payload_hash:
                raise ToolError("request_id_conflict")
            if args.get("deal_id"):
                _sample_deal(conn, args["deal_id"])
            result = db.uj(op["result_json"])
            conn.rollback()
            return result
        result = operation(conn)
        conn.execute(
            "INSERT INTO mcp_operations "
            "(request_id, tool, payload_hash, result_json, created_at, deal_id) "
            "VALUES (?,?,?,?,?,?)",
            (request_id, tool, payload_hash, db.j(result), db.now(), args.get("deal_id")))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def t_record_step(args):
    def write(conn):
        _sample_deal(conn, args["deal_id"])
        if args.get("offer_id"):
            _offer_of_deal(conn, args["deal_id"], args["offer_id"])
        try:
            step_id = core.record_step(conn, args["deal_id"], args.get("offer_id"),
                                       args["step_key"], args["status"], args.get("private_note"),
                                       trigger=args.get("trigger"))
        except ValueError as exc:
            raise ToolError(str(exc))
        return {"step_id": step_id}
    return _write_operation("record_step", args, write)


def _validate_output(kind, payload):
    if not isinstance(payload, dict):
        raise ToolError("invalid_payload")
    keys = {"intake": ("offer", "evidence", "missing_fields", "clarification"),
            "scoping": ("scope_profile", "comparable_ids", "differences"),
            "negotiator": ("draft", "option_ids"), "clarification": ("questions",)}
    if kind not in keys or any(k not in payload for k in keys[kind]):
        raise ToolError("invalid_output_shape")
    if kind == "intake" and not isinstance(payload["missing_fields"], list):
        raise ToolError("invalid_output_shape")
    if kind == "scoping":
        if not isinstance(payload["scope_profile"], list) or not isinstance(payload["comparable_ids"], list) or not isinstance(payload["differences"], list):
            raise ToolError("invalid_output_shape")
        if any(not isinstance(item, str) for item in payload["comparable_ids"]):
            raise ToolError("invalid_output_shape")
        for key, required in (("scope_profile", ("field", "value", "evidence")), ("differences", ("project_id", "fields", "why", "evidence"))):
            if any(not isinstance(x, dict) or any(k not in x for k in required) for x in payload[key]):
                raise ToolError("invalid_output_shape")
    if kind == "clarification" and (not isinstance(payload["questions"], list) or any(not isinstance(x, str) for x in payload["questions"])):
        raise ToolError("invalid_output_shape")
    if kind == "negotiator" and (not isinstance(payload["draft"], str) or
            not isinstance(payload["option_ids"], list) or
            any(not isinstance(item, str) for item in payload["option_ids"])):
        raise ToolError("invalid_output_shape")


def t_save_agent_output(args):
    def write(conn):
        _sample_deal(conn, args["deal_id"])
        if args.get("offer_id"):
            _offer_of_deal(conn, args["deal_id"], args["offer_id"])
        _validate_output(args["kind"], args["payload"])
        output_id = db.new_id()
        conn.execute("INSERT INTO agent_outputs VALUES (?,?,?,?,?,?,?)", (output_id,
                     args["deal_id"], args.get("offer_id"), args["kind"], db.j(args["payload"]),
                     args["request_id"], db.now()))
        return {"output_id": output_id}
    return _write_operation("save_agent_output", args, write)


def t_get_scoping_history(args):
    conn = _connect()
    try:
        return {"private": True, "note": "Private scoping history; do not forward to the negotiator.",
                "projects": [dict(r) for r in conn.execute("SELECT * FROM seed_past_projects ORDER BY rowid")]}
    finally:
        conn.close()


def t_list_pending_work(args):
    conn = _connect()
    try:
        inquiries = [{"source": r["source"], "source_id": r["source_id"],
                      "fields": db.uj(r["fields_json"]), "raw_message": r["raw_message"],
                      "received_at": r["received_at"]} for r in conn.execute(
            "SELECT i.* FROM inbox i WHERE i.sample_data=1 AND NOT EXISTS "
            "(SELECT 1 FROM deals d WHERE d.source=i.source AND d.source_id=i.source_id) ORDER BY i.rowid")]
        changes = [{"deal_id": r["deal_id"], "request_id": r["request_id"], "body": r["body"],
                    "version_id": r["version_id"], "is_current_published": bool(r["is_current"])}
                   for r in conn.execute("SELECT m.*, (m.version_id=(SELECT id FROM proposal_versions WHERE deal_id=d.id AND status='published' ORDER BY version_no DESC LIMIT 1)) AS is_current "
                     "FROM messages m JOIN deals d ON d.id=m.deal_id JOIN proposal_versions v ON v.id=m.version_id "
                     "WHERE d.sample_data=1 AND m.direction='inbound' AND NOT EXISTS "
                     "(SELECT 1 FROM offers o WHERE o.deal_id=m.deal_id AND o.source='change:' || m.request_id) ORDER BY m.rowid")]
        # Client changes the proposal page already turned into a structured
        # offer (or that were submitted but not yet priced): the agent still
        # has to check the terms and run the engine on them.
        unevaluated = [{"deal_id": r["deal_id"], "offer_id": r["id"], "round": r["round"],
                        "request_id": r["source"][len("change:"):],
                        "body": r["body"], "offer": db.uj(r["offer_json"])}
                       for r in conn.execute(
            "SELECT o.*, (SELECT m.body FROM messages m WHERE m.deal_id=o.deal_id"
            " AND m.direction='inbound' AND 'change:' || m.request_id=o.source) AS body "
            "FROM offers o JOIN deals d ON d.id=o.deal_id "
            "WHERE d.sample_data=1 AND o.source LIKE 'change:%' AND NOT EXISTS "
            "(SELECT 1 FROM evaluations e WHERE e.offer_id=o.id) ORDER BY o.rowid")]
        return {"inquiries": inquiries, "change_requests": changes,
                "unevaluated_offers": unevaluated}
    finally:
        conn.close()


def t_submit_change_offer(args):
    def write(conn):
        deal = _sample_deal(conn, args["deal_id"])
        version = conn.execute(
            "SELECT * FROM proposal_versions WHERE id=? AND deal_id=?",
            (args["version_id"], args["deal_id"])).fetchone()
        current = core.current_version(conn, args["deal_id"])
        if not version or not current or current["id"] != args["version_id"]:
            core.record_step(conn, args["deal_id"], None, "stale_version", "blocked", "Change submitted against stale version")
            return {"_error": "stale_version"}
        errors = core.validate_offer_request(args["offer_request"])
        if errors:
            raise ToolError("engine_validation_failed: " + str(errors))
        msg = conn.execute("SELECT * FROM messages WHERE request_id=?",
                           (args["request_id"],)).fetchone()
        if msg and (msg["deal_id"] != args["deal_id"] or msg["version_id"] != args["version_id"] or msg["direction"] != "inbound"):
            raise ToolError("request_id_conflict")
        prior_offer = conn.execute(
            "SELECT 1 FROM offers WHERE deal_id=? AND source=?",
            (args["deal_id"], "change:" + args["request_id"])).fetchone()
        if msg and prior_offer:
            raise ToolError("request_id_conflict: change already submitted")
        if not msg:
            evidence = args.get("evidence") or {}
            if not isinstance(evidence, dict) or not isinstance(evidence.get("raw_message"), str):
                raise ToolError("missing_field:evidence.raw_message")
            conn.execute(
                "INSERT INTO messages (id, deal_id, version_id, direction, body, "
                "option_ids_json, check_json, request_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (db.new_id(), args["deal_id"], args["version_id"], "inbound",
                 evidence["raw_message"], db.j([]), None, args["request_id"], db.now()))
        offer_id = core.create_change_offer(conn, args["deal_id"], args["version_id"], args["request_id"], args["offer_request"])
        return {"offer_id": offer_id}
    result = _write_operation("submit_change_offer", args, write)
    if "_error" in result:
        raise ToolError(result["_error"])
    return result

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

TOOLS += [("record_step", t_record_step, "Record an agent run step."),
          ("save_agent_output", t_save_agent_output, "Save private agent output."),
          ("get_scoping_history", t_get_scoping_history, "Read private scoping history."),
          ("list_pending_work", t_list_pending_work, "List unclaimed sample inquiries, queued changes, and client change offers not yet evaluated."),
          ("submit_change_offer", t_submit_change_offer, "Submit structured change terms.")]

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
                             "properties": _schema(name), **({"required": _required(name)} if name in ("record_step", "save_agent_output", "get_scoping_history", "list_pending_work", "submit_change_offer") else {})}}
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
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ToolError("invalid_arguments: expected object")
            if name in ("record_step", "save_agent_output", "get_scoping_history", "list_pending_work", "submit_change_offer"):
                for field in _required(name):
                    if field not in arguments:
                        raise ToolError(f"missing_field:{field}")
                for field, schema in _schema(name).items():
                    if field in arguments:
                        value = arguments[field]
                        expected = str if schema["type"] == "string" else dict
                        if not isinstance(value, expected) or (field in _required(name) and expected is str and not value.strip()):
                            raise ToolError(f"invalid_field:{field}")
            result = fn(arguments)
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
                "offer_request": {"type": "object"}, "evidence": {"type": "object"},
                "request_id": {"type": "string"}}
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
    schemas = {
        "record_step": {"deal_id": "string", "offer_id": "string", "step_key": "string", "status": "string", "private_note": "string", "trigger": "string", "request_id": "string"},
        "save_agent_output": {"deal_id": "string", "offer_id": "string", "kind": "string", "payload": "object", "request_id": "string"},
        "submit_change_offer": {"deal_id": "string", "request_id": "string", "version_id": "string", "offer_request": "object", "evidence": "object"}}
    result = {k: {"type": v} for k, v in schemas.get(name, {}).items()}
    if name == "record_step":
        result["step_key"]["enum"] = sorted(core.STEP_KEYS)
        result["status"]["enum"] = sorted(core.STEP_STATUSES)
    elif name == "save_agent_output":
        result["kind"]["enum"] = ["intake", "scoping", "negotiator", "clarification"]
    return result


def _required(name):
    return {"record_step": ["deal_id", "step_key", "status", "request_id"],
            "save_agent_output": ["deal_id", "kind", "payload", "request_id"],
            "submit_change_offer": ["deal_id", "request_id", "version_id", "offer_request"],
            "get_scoping_history": [], "list_pending_work": []}[name]


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
