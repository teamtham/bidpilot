"""BidPilot core workflow: engine calls, versions, digests, release boundary."""
import copy
import hashlib
import functools
import inspect
import importlib.util
import json
import re
import secrets
import sqlite3
import sys
from datetime import date
from pathlib import Path

import db
from db import j, uj
import capacity as cap

BASE = Path(__file__).resolve().parent
ENGINE_DIR = BASE / "bidpilot-engine"

_spec = importlib.util.spec_from_file_location("bidpilot_engine", ENGINE_DIR / "engine.py")
engine = importlib.util.module_from_spec(_spec)
sys.modules["bidpilot_engine"] = engine
_spec.loader.exec_module(engine)

ENGINE_VERSION = "bidpilot-engine demo (engine.py, unchanged, stdlib only)"
SAMPLE_BADGE = "Sample data · 23 Nov 2026 · Asia/Singapore"


def load_seed(conn, commit=True):
    """Import the five CSV tables unchanged (CSV -> JSON tables for engine + seed rows)."""
    data = engine.load_data(ENGINE_DIR / "data")
    conn.execute("DELETE FROM seed_business_rules")
    for row in data["business_rules"]:
        conn.execute("INSERT INTO seed_business_rules VALUES (?,?,?)",
                     (row["key"], float(row["value"]), row["meaning"]))
    conn.execute("DELETE FROM seed_effort_rules")
    for row in data["effort_rules"]:
        conn.execute("INSERT INTO seed_effort_rules VALUES (?,?,?)",
                     (row["rule"], row["hours"], row["applies"]))
    conn.execute("DELETE FROM seed_rate_card")
    for row in data["rate_card"]:
        conn.execute("INSERT INTO seed_rate_card VALUES (?,?,?)",
                     (row["item"], float(row["price_sgd"]), row["unit"]))
    conn.execute("DELETE FROM seed_past_projects")
    for row in data["past_projects"]:
        conn.execute("INSERT INTO seed_past_projects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            row["project_id"], row["client"], row["format"], row["production_mode"],
            int(row["shoot_days"]), int(row["locations"]), row["scripting"],
            int(row["deliverables"]), row["editing_level"], row["graphics"],
            int(row["revision_rounds"]), int(row["caption_versions"]),
            float(row["est_hours"]), float(row["actual_hours"]), float(row["price_sgd"]),
            row["start"], row["end"]))
    conn.execute("DELETE FROM seed_calendar_weeks")
    for row in data["calendar_weeks"]:
        conn.execute("INSERT INTO seed_calendar_weeks VALUES (?,?,?,?,?)", (
            row["week_ending"], float(row["working_hours"]),
            float(row["calendar_busy_hours"]), float(row["unscheduled_owed_hours"]), row["note"]))
    if commit:
        conn.commit()
    return data


def seed_data():
    """Authoritative sample data: the supplied CSVs, loaded read-only."""
    return engine.load_data(ENGINE_DIR / "data")


def seed_hash(data):
    """Stable hash of the authoritative seed snapshot (CSVs immutable)."""
    return hashlib.sha256(json.dumps(
        {name: data[name] for name in sorted(data)}, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def run_engine_evaluate(request):
    """Execute the supplied Python engine. Source of truth for all option terms."""
    return engine.evaluate(request, engine.load_data(ENGINE_DIR / "data"))


def run_outbound_check(payload):
    return engine.outbound_check(payload["draft"], payload["approved_options"],
                                 payload["private_values"], payload["other_client_names"])


def other_client_names(conn, exclude_client):
    rows = conn.execute("SELECT DISTINCT client FROM seed_past_projects").fetchall()
    return [r["client"] for r in rows if r["client"] != exclude_client]


def _write_locked(fn):
    """Lock before reads and release an owned transaction on every exit."""
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapped(conn, *args, **kwargs):
        owned = not conn.in_transaction
        if owned:
            conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn, *args, **kwargs)
            bound = signature.bind(conn, *args, **kwargs)
            should_commit = bound.arguments.get("commit", True)
            if owned and should_commit and conn.in_transaction:
                conn.commit()
            return result
        except Exception:
            if owned and conn.in_transaction:
                conn.rollback()
            raise
    return wrapped


# ---- deal intake ----------------------------------------------------------

def validate_offer_request(offer_request):
    """Validate intake through the engine; missing/invalid values are never
    fabricated — they are reported so the intake form can be completed."""
    if not isinstance(offer_request, dict):
        return [("INVALID_FIELD", "offer_request")]
    return [tuple(e) for e in engine.validation_errors(offer_request)]


def _insert_intake_offer(conn, deal_id, source, source_id, offer_request, now):
    """Insert the first intake offer for a deal in the caller's transaction."""
    offer_id = db.new_id()
    round_no = 1 + (conn.execute(
        "SELECT COALESCE(MAX(round),0) AS r FROM offers WHERE deal_id=?", (deal_id,)
    ).fetchone()["r"])
    conn.execute(
        "INSERT INTO offers (id, deal_id, source, source_id, from_version_id, round, as_of,"
        " offer_json, evidence_json, comparable_ids_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (offer_id, deal_id, source, source_id, None, round_no,
         offer_request.get("as_of"), j(offer_request.get("offer")),
         j(offer_request), j(offer_request.get("comparable_ids", [])), now))
    return offer_id


def claim_or_resume(conn, source, source_id, client, raw_inquiry, offer_request,
                    require_sample=False, commit=True):
    """Unique (source, source_id): a duplicate import resumes the same deal.

    An incomplete intake (engine reports only MISSING_FIELD) is never rejected
    and never fabricated: it persists as a deal in status "needs_information"
    that records the missing field paths, with no offer. A later claim with the
    same (source, source_id) resumes that deal and, once the missing values are
    supplied, creates the offer and advances to "new". Invalid values
    (INVALID_FIELD) are still rejected. Complete-intake behavior is unchanged.

    With require_sample=True (MCP bridge), a write transaction is opened
    BEFORE any read that decides writes, and a colliding non-sample deal is
    rejected inside that same transaction — before any resume log, offer or
    deal row is written, whether or not the colliding deal already has an
    offer.
    """
    if require_sample and not conn.in_transaction:
        # Callers may already hold the write transaction for related writes.
        conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT * FROM deals WHERE source=? AND source_id=?",
                       (source, source_id)).fetchone()
    now = db.now()
    errors = validate_offer_request(offer_request)
    incomplete = bool(errors) and all(code == "MISSING_FIELD" for code, _ in errors)
    missing_fields = [field for _code, field in errors]
    if row:
        if require_sample and not row["sample_data"]:
            conn.rollback()
            raise ValueError("non_sample_record_rejected: MCP tools accept sample data only")
        deal_id = row["id"]
        has_offer = conn.execute("SELECT 1 FROM offers WHERE source=? AND source_id=?",
                                 (source, source_id)).fetchone() is not None
        if row["status"] == "needs_information" and not has_offer:
            # Resuming an incomplete intake; the missing info may now be supplied.
            if incomplete:
                _step(conn, deal_id, "needs_information", "queued",
                      j({"missing_fields": missing_fields}))
                if commit:
                    conn.commit()
                return deal_id, None, False
            if errors:
                conn.rollback()
                raise ValueError("engine_validation_failed: " + "; ".join(
                    f"{code}:{field}" for code, field in errors))
            _step(conn, deal_id, "intake", "ok",
                  "Missing information supplied; intake complete")
            conn.execute("UPDATE deals SET status='new' WHERE id=?"
                         " AND status='needs_information'", (deal_id,))
            offer_id = _insert_intake_offer(conn, deal_id, source, source_id,
                                            offer_request, now)
            if commit:
                conn.commit()
            return deal_id, offer_id, False
        # Duplicate intake resumes the deal; history (raw_inquiry, client) is
        # never overwritten by a later copy of the same inbound message.
        _step(conn, deal_id, "resume", "ok", f"Duplicate inbound {source}/{source_id} resumed deal")
    else:
        if errors and not incomplete:
            conn.rollback()
            raise ValueError("engine_validation_failed: " + "; ".join(
                f"{code}:{field}" for code, field in errors))
        deal_id = db.new_id()
        conn.execute(
            "INSERT INTO deals (id, source, source_id, client, raw_inquiry, status,"
            " current_version_id, sample_data, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (deal_id, source, source_id, client, raw_inquiry,
             "needs_information" if incomplete else "new", None, 1, now))
        if incomplete:
            _step(conn, deal_id, "needs_information", "queued",
                  j({"missing_fields": missing_fields}))
            if commit:
                conn.commit()
            return deal_id, None, True
        _step(conn, deal_id, "intake", "ok", "Inbound message claimed")
    if commit:
        conn.commit()
    # duplicate intake reuses the deal's first offer: offers are saved with the
    # deal's stable (source, source_id) keys, so the same lookup that inserted
    # finds them again — no transformed keys, no second offer on retry.
    offer_row = conn.execute("SELECT * FROM offers WHERE source=? AND source_id=?",
                             (source, source_id)).fetchone()
    if offer_row:
        return deal_id, offer_row["id"], False
    offer_id = _insert_intake_offer(conn, deal_id, source, source_id, offer_request, now)
    if commit:
        conn.commit()
    return deal_id, offer_id, True


# ---- client change parsing ------------------------------------------------

WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
CHANGE_COUNT = re.compile(
    r"\b(\d+|" + "|".join(WORD_NUMBERS) + r")\s+videos?\b", re.IGNORECASE)
CHANGE_CAPTIONS = re.compile(
    r"\b(\d+|" + "|".join(WORD_NUMBERS) + r")\s+caption(?:ed)?\s+versions?\b", re.IGNORECASE)
CHANGE_BUDGET = re.compile(
    r"(?:s\$|sgd\s*|\$)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE)
CHANGE_ISO = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
CHANGE_MONTHS = ("January February March April May June July August September October"
                 " November December").split()
CHANGE_MONTH = "|".join(m[:3] for m in CHANGE_MONTHS)
CHANGE_DATE_TEXT = re.compile(
    rf"\b(?:(\d{{1,2}})\s+({CHANGE_MONTH})[a-z]*|({CHANGE_MONTH})[a-z]*\s+(\d{{1,2}}))"
    rf"(?:,?\s+(\d{{4}}))?\b", re.IGNORECASE)


def _word_number(token):
    token = token.lower()
    return int(token) if token.isdigit() else WORD_NUMBERS[token.lower()]


def _month_index(name):
    name = name.lower()[:3]
    return next(i for i, m in enumerate(CHANGE_MONTHS, 1) if m.lower().startswith(name))


def parse_change_body(body, prior_request):
    """Extract explicit count, captions, deadline and budget from change text.

    Returns a dict of parsed values (None when not stated). Pure parsing: it
    never invents terms and never falls back to a fixed counter fixture.
    """
    text = " " + " ".join((body or "").split()) + " "
    parsed = {"count": None, "captions": None, "deadline": None, "budget": None}

    m = CHANGE_COUNT.search(text)
    if m:
        parsed["count"] = _word_number(m[1])
    m = CHANGE_CAPTIONS.search(text)
    if m:
        parsed["captions"] = _word_number(m[1])
    m = CHANGE_BUDGET.search(text)
    if m:
        parsed["budget"] = round(float(m[1].replace(",", "")), 2)

    m = CHANGE_ISO.search(body or "")
    if m:
        parsed["deadline"] = m[1]
    else:
        m = CHANGE_DATE_TEXT.search(text)
        if m:
            day, month = (m[1], m[2]) if m[1] else (m[4], m[3])
            year = int(m[5]) if m[5] else date.fromisoformat(
                prior_request["as_of"]).year
            candidate = date(year, _month_index(month), int(day))
            if not m[5] and candidate < date.fromisoformat(prior_request["as_of"]):
                candidate = date(year + 1, candidate.month, candidate.day)
            parsed["deadline"] = candidate.isoformat()
    return parsed


@_write_locked
def client_change(conn, deal_id, body, request_id, terms=None, expected_version_id=None,
                  expected_token=None):
    """A client change creates a new offer and proposal version; never overwrites history.

    The full prior request is decoded once; only explicitly stated terms
    (count, captions, deadline, budget) are applied. Unparseable free text is
    queued for founder clarification instead of being turned into an offer.
    ``terms`` carries explicit structured fields from the client form; they
    override anything parsed from the free-text body, so an adversarial
    message keeps its exact wording while the counters state the real terms.
    """
    deal = conn.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
    version = current_version(conn, deal_id)
    if expected_version_id is not None and (version is None or version["id"] != expected_version_id):
        raise ValueError("stale_version")
    if expected_token is not None and (version is None or version["token_hash"] !=
            hashlib.sha256(expected_token.encode()).hexdigest()):
        raise ValueError("stale_version")
    if version is None:
        raise ValueError("No published version to change against")
    if version["status"] != "published":
        raise ValueError("Current version is not published")
    existing_msg = conn.execute("SELECT 1 FROM messages WHERE request_id=? AND deal_id=?",
                                (request_id, deal_id)).fetchone()
    if existing_msg:
        # Retry of the same payload (e.g. after an ambiguous transport
        # outcome): never create a second round — return the original result.
        offer_row = conn.execute("SELECT id FROM offers WHERE deal_id=? AND source=?",
                                 (deal_id, f"change:{request_id}")).fetchone()
        return offer_row["id"] if offer_row else None
    now = db.now()
    conn.execute(
        "INSERT INTO messages (id, deal_id, version_id, direction, body, option_ids_json,"
        " check_json, request_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (db.new_id(), deal_id, version["id"], "inbound", body, j([]), None, request_id, now))

    prior = uj(conn.execute("SELECT evidence_json FROM offers WHERE id=?",
                            (version["offer_id"],)).fetchone()["evidence_json"])
    parsed = parse_change_body(body, prior)
    for key in ("count", "captions", "deadline", "budget"):
        value = (terms or {}).get(key)
        if value is not None:
            if key == "budget":
                value = float(value)
            elif key in ("count", "captions"):
                value = int(value)
            parsed[key] = value
    if not (parsed["count"] and parsed["deadline"] and parsed["budget"] is not None):
        missing = [k for k in ("count", "deadline", "budget") if parsed[k] is None]
        _step(conn, deal_id, "client_change", "queued",
              f"Change request {request_id} queued for clarification; unstated: "
              f"{', '.join(missing)}")
        conn.commit()
        return None
    request = copy.deepcopy(prior)
    offer = request["offer"]
    offer["deliverables"] = parsed["count"]
    offer["caption_versions"] = (parsed["captions"] if parsed["captions"] is not None
                                 else min(offer["caption_versions"], parsed["count"]))
    offer["deadline"] = parsed["deadline"]
    offer["budget_sgd"] = parsed["budget"]
    if engine.validation_errors(request):
        _step(conn, deal_id, "client_change", "queued",
              f"Change request {request_id} queued: parsed terms are not engine-valid")
        conn.commit()
        return None
    offer_id = create_change_offer(conn, deal_id, version["id"], request_id, request)
    conn.commit()
    return offer_id


def create_change_offer(conn, deal_id, version_id, request_id, request):
    """Insert a validated structured change in the caller's transaction."""
    existing = conn.execute("SELECT id FROM offers WHERE deal_id=? AND source=?",
                            (deal_id, f"change:{request_id}")).fetchone()
    if existing:
        return existing["id"]
    offer_id = db.new_id()
    round_no = conn.execute("SELECT COALESCE(MAX(round),0)+1 AS r FROM offers WHERE deal_id=?",
                            (deal_id,)).fetchone()["r"]
    conn.execute(
        "INSERT INTO offers (id, deal_id, source, source_id, from_version_id, round, as_of,"
        " offer_json, evidence_json, comparable_ids_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (offer_id, deal_id, f"change:{request_id}", f"r{round_no}", version_id, round_no,
         request.get("as_of"), j(request["offer"]), j(request),
         j(request.get("comparable_ids", [])), db.now()))
    conn.execute("UPDATE deals SET status='change_requested', current_version_id=NULL WHERE id=?",
                 (deal_id,))
    _step(conn, deal_id, "client_change", "ok", f"Change request {request_id} queued", offer_id)
    return offer_id


STEP_KEYS = frozenset(("claimed", "intake", "scoping", "engine", "draft", "outbound",
                       "review", "needs_information", "stale_version", "blocked", "error"))
STEP_STATUSES = frozenset(("running", "ok", "blocked", "error", "skipped"))


def record_step(conn, deal_id, offer_id, step_key, status, private_note=None,
                trigger=None):
    """Append an agent step in the caller's transaction, closing an open run.

    trigger is optional and backward compatible: None (unrecorded), "manual"
    or "scheduled". Older rows and callers keep working unchanged.
    """
    if step_key not in STEP_KEYS or status not in STEP_STATUSES:
        raise ValueError("invalid_step_key_or_status")
    if trigger is not None and trigger not in ("manual", "scheduled"):
        raise ValueError("invalid_trigger")
    if offer_id and not conn.execute("SELECT 1 FROM offers WHERE id=? AND deal_id=?",
                                     (offer_id, deal_id)).fetchone():
        raise ValueError("offer_ownership_mismatch")
    now = db.now()
    if status != "running":
        conn.execute("UPDATE run_steps SET finished_at=? WHERE id=(SELECT id FROM run_steps "
                     "WHERE deal_id=? AND offer_id IS ? AND step_key=? AND status='running' "
                     "AND finished_at IS NULL ORDER BY rowid DESC LIMIT 1)",
                     (now, deal_id, offer_id, step_key))
    step_id = db.new_id()
    conn.execute("INSERT INTO run_steps (id, deal_id, offer_id, step_key, status,"
                 " private_note, started_at, finished_at, trigger) VALUES (?,?,?,?,?,?,?,?,?)",
                 (step_id, deal_id, offer_id, step_key, status, private_note, now,
                  None if status == "running" else now, trigger))
    return step_id


# ---- evaluation -----------------------------------------------------------

@_write_locked
def evaluate(conn, deal_id, offer_id, commit=True):
    """Run the real engine once per offer; retries are idempotent.

    If the offer was already evaluated, the stored evaluation and its
    proposal version are returned without rerunning the engine or creating
    another version.
    """
    offer_row = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    if offer_row is None or offer_row["deal_id"] != deal_id:
        raise ValueError("offer_not_found_or_ownership_mismatch")
    existing = conn.execute("SELECT * FROM evaluations WHERE offer_id=?",
                            (offer_id,)).fetchone()
    if existing:
        version = conn.execute(
            "SELECT id FROM proposal_versions WHERE offer_id=?"
            " ORDER BY version_no DESC LIMIT 1", (offer_id,)).fetchone()
        return existing["id"], (version["id"] if version else None)
    request = uj(offer_row["evidence_json"])
    now = db.now()
    _step(conn, deal_id, "engine_evaluate", "running", "Engine evaluation started", offer_id)
    # The unchanged engine sees residual capacity: confirmed reservations are
    # folded into a copied calendar table via the conservative adapter. The
    # CSVs and the SQLite seed mirror stay untouched.
    ledger_weeks = cap.ledger(conn)
    data = cap.adapted_data(seed_data(), ledger_weeks)
    result = engine.evaluate(request, data)
    snapshot = {"as_of": request.get("as_of"), "revision": cap.ledger_revision(conn),
                "weeks": [{"week": w, **snap} for w, snap in sorted(ledger_weeks.items())]}
    data_hash = seed_hash(seed_data())
    eval_id = db.new_id()
    conn.execute(
        "INSERT INTO evaluations (id, offer_id, verdict, reason_codes_json, result_json,"
        " engine_version, seed_hash, capacity_revision, capacity_snapshot_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (eval_id, offer_id, result["verdict"], j(result["reason_codes"]), j(result),
         ENGINE_VERSION, data_hash, snapshot["revision"], j(snapshot), now))
    for opt in result["options"]:
        conn.execute(
            "INSERT OR IGNORE INTO options (id, evaluation_id, option_key, kind,"
            " public_terms_json, created_at) VALUES (?,?,?,?,?,?)",
            (db.new_id(), eval_id, opt["option_id"], opt["kind"], j(opt), now))
    # create a proposal version for this offer
    prior = conn.execute(
        "SELECT MAX(version_no) AS v FROM proposal_versions WHERE deal_id=?",
        (deal_id,)).fetchone()["v"] or 0
    draft = draft_for(result, request["offer"]["client"])
    version_id = db.new_id()
    prev = conn.execute("SELECT id FROM proposal_versions WHERE deal_id=? AND version_no=?",
                        (deal_id, prior)).fetchone()
    option_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM options WHERE evaluation_id=? ORDER BY rowid", (eval_id,))]
    conn.execute(
        "INSERT INTO proposal_versions (id, deal_id, offer_id, previous_version_id, version_no,"
        " status, draft_body, outbound_status, checked_digest, public_option_ids_json,"
        " token_hash, selected_option_id, published_at, accepted_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (version_id, deal_id, offer_id, prev["id"] if prev else None, prior + 1, "draft",
         draft, "unchecked", None, j(option_ids), None, None, None, None, now))
    conn.execute("UPDATE deals SET status='evaluated' WHERE id=? AND status IN ('new','change_requested')",
                 (deal_id,))
    _step(conn, deal_id, "engine_evaluate", "ok",
          f"verdict={result['verdict']} reasons={result['reason_codes']}", offer_id)
    if commit:
        conn.commit()
    return eval_id, version_id


def reevaluate(conn, offer_id, commit=True):
    """Explicit fresh evaluation path for a stale offer.

    Creates a NEW offer round carrying the identical stored evidence (terms are
    never overwritten), then evaluates it against current residual capacity:
    fresh evaluation, options, version, check and founder review. Existing
    approved terms and versions remain untouched historical evidence.
    """
    offer_row = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    if offer_row is None:
        raise ValueError("offer_not_found")
    deal_id = offer_row["deal_id"]
    deal = conn.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
    if deal["status"] in ("confirmed", "closed"):
        raise ValueError("manual_review_required: confirmed booking cannot be re-evaluated")
    now = db.now()
    request = uj(offer_row["evidence_json"])
    new_offer = db.new_id()
    round_no = conn.execute("SELECT COALESCE(MAX(round),0)+1 AS r FROM offers WHERE deal_id=?",
                            (deal_id,)).fetchone()["r"]
    conn.execute(
        "INSERT INTO offers (id, deal_id, source, source_id, from_version_id, round, as_of,"
        " offer_json, evidence_json, comparable_ids_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (new_offer, deal_id, offer_row["source"], f"reeval:{offer_id}:{round_no}",
         offer_row["from_version_id"], round_no, offer_row["as_of"],
         j(request["offer"]), j(request), j(request.get("comparable_ids", [])), now))
    _step(conn, deal_id, "reevaluate", "ok",
          f"Fresh evaluation round created from offer {offer_id}", new_offer)
    if commit:
        conn.commit()
    return new_offer


def draft_for(result, client):
    est = result.get("estimate")
    opts = result.get("options") or []
    if result["verdict"] == "accept" and opts:
        o = opts[0]
        return (f"Thanks {client} — we can deliver {o['deliverables']} videos by {o['deadline']}"
                f" for S${o['price_sgd']:,.2f}.")
    if opts:
        o = opts[-1]
        if o.get("phases"):
            p1, p2 = o["phases"]
            return (f"Thanks {client} — a phased plan works: {p1['deliverables']} videos by"
                    f" {p1['deadline']} and {p2['deliverables']} videos by {p2['deadline']},"
                    f" S${o['price_sgd']:,.2f} for the full package.")
        return (f"Thanks {client} — we can deliver {o['deliverables']} videos by {o['deadline']}"
                f" for S${o['price_sgd']:,.2f}.")
    return f"Thanks {client} — we will review and come back to you."


# ---- outbound check -------------------------------------------------------

def digest(version_row, option_rows):
    canonical = {
        "version_id": version_row["id"],
        "draft_bytes": version_row["draft_body"],
        "options": [{
            "option_id": r["id"],
            "terms": uj(r["public_terms_json"]),
        } for r in option_rows],
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        .encode("utf-8")).hexdigest()


def options_of(conn, version_row):
    ids = uj(version_row["public_option_ids_json"])
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM options WHERE id IN ({marks})", ids).fetchall()
    by_id = {row["id"]: row for row in rows}
    return [by_id[option_id] for option_id in ids if option_id in by_id]


def option_set_errors(conn, version_row):
    """A version's option set must be the exact ordered engine output.

    The expected set comes from the engine evaluation result itself, so a
    deleted option row (or an emptied id list) fails closed:
    - the complete ordered engine option key set must exist as option rows
      for the evaluation of the version's own offer (no missing, no extra);
    - the version's stored id list must equal those rows in engine order,
      with no missing and no duplicate ids;
    - every stored public terms payload must byte-match the original engine
      evaluation output (tamper detection);
    - every option must belong to the evaluation of the version's own offer
      (blocks cross-deal / cross-version options).
    """
    eval_row = conn.execute("SELECT * FROM evaluations WHERE offer_id=?",
                            (version_row["offer_id"],)).fetchone()
    if eval_row is None:
        return ["option_evaluation_missing"]
    result = uj(eval_row["result_json"])
    engine_options = result.get("options") or []
    if not engine_options:
        return ["option_engine_output_empty"]
    expected_keys = [o["option_id"] for o in engine_options]
    rows = conn.execute("SELECT * FROM options WHERE evaluation_id=? ORDER BY rowid",
                        (eval_row["id"],)).fetchall()
    row_by_key = {r["option_key"]: r for r in rows}
    errors = []
    if sorted(row_by_key) != sorted(expected_keys):
        errors.append("option_keys_not_complete")
    selected_keys = expected_keys
    if version_row["import_option_keys_json"] is not None:
        try:
            selected_keys = uj(version_row["import_option_keys_json"])
            if (not isinstance(selected_keys, list) or not selected_keys or
                    any(not isinstance(k, str) or k not in expected_keys for k in selected_keys) or
                    len(set(selected_keys)) != len(selected_keys)):
                raise ValueError("invalid subset")
        except (ValueError, TypeError):
            errors.append("option_import_subset_invalid")
            selected_keys = []
    expected_ids = [row_by_key[k]["id"] for k in selected_keys if k in row_by_key]
    stored_ids = uj(version_row["public_option_ids_json"]) or []
    if stored_ids != expected_ids:
        if (sorted(stored_ids) == sorted(expected_ids)
                and len(stored_ids) == len(expected_ids)):
            errors.append("option_ids_out_of_order")
        else:
            errors.append("option_ids_not_exact")
    for engine_opt in engine_options:
        r = row_by_key.get(engine_opt["option_id"])
        if r is None or uj(r["public_terms_json"]) != engine_opt:
            errors.append(f"option_terms_mismatch:{engine_opt['option_id']}")
            break
    return errors


def _mirror_errors(conn, data):
    """SQLite seed tables must still mirror the authoritative CSVs."""
    errors = []
    br = conn.execute("SELECT key, value, meaning FROM seed_business_rules").fetchall()
    if {(r["key"], r["value"], r["meaning"]) for r in br} != {
            (row["key"], float(row["value"]), row["meaning"])
            for row in data["business_rules"]}:
        errors.append("seed_table_mismatch:business_rules")
    er = conn.execute("SELECT rule, hours, applies FROM seed_effort_rules").fetchall()
    if {(r["rule"], r["hours"], r["applies"]) for r in er} != {
            (row["rule"], row["hours"], row["applies"]) for row in data["effort_rules"]}:
        errors.append("seed_table_mismatch:effort_rules")
    rc = conn.execute("SELECT item, price_sgd, unit FROM seed_rate_card").fetchall()
    if {(r["item"], r["price_sgd"], r["unit"]) for r in rc} != {
            (row["item"], float(row["price_sgd"]), row["unit"])
            for row in data["rate_card"]}:
        errors.append("seed_table_mismatch:rate_card")
    cw = conn.execute("SELECT week_ending, working_hours, calendar_busy_hours,"
                      " unscheduled_owed_hours, note FROM seed_calendar_weeks").fetchall()
    if {(r["week_ending"], r["working_hours"], r["calendar_busy_hours"],
         r["unscheduled_owed_hours"], r["note"]) for r in cw} != {
            (row["week_ending"], float(row["working_hours"]),
             float(row["calendar_busy_hours"]), float(row["unscheduled_owed_hours"]),
             row["note"]) for row in data["calendar_weeks"]}:
        errors.append("seed_table_mismatch:calendar_weeks")
    pp = conn.execute("SELECT * FROM seed_past_projects").fetchall()
    if len(pp) != len(data["past_projects"]):
        return errors + ["seed_table_mismatch:past_projects"]
    for row, csv_row in zip(
            sorted(pp, key=lambda r: r["project_id"]),
            sorted(data["past_projects"], key=lambda r: r["project_id"])):
        same = (row["project_id"] == csv_row["project_id"]
                and row["client"] == csv_row["client"]
                and row["format"] == csv_row["format"]
                and row["production_mode"] == csv_row["production_mode"]
                and (row["shoot_days"], row["locations"], row["deliverables"],
                     row["revision_rounds"], row["caption_versions"]) == (
                    int(csv_row["shoot_days"]), int(csv_row["locations"]),
                    int(csv_row["deliverables"]), int(csv_row["revision_rounds"]),
                    int(csv_row["caption_versions"]))
                and (row["scripting"], row["editing_level"], row["graphics"],
                     row["start"], row["end"]) == (
                    csv_row["scripting"], csv_row["editing_level"], csv_row["graphics"],
                    csv_row["start"], csv_row["end"])
                and (row["est_hours"], row["actual_hours"], row["price_sgd"]) == (
                    float(csv_row["est_hours"]), float(csv_row["actual_hours"]),
                    float(csv_row["price_sgd"])))
        if not same:
            errors.append("seed_table_mismatch:past_projects")
            break
    return errors


def seed_errors(conn, version_row):
    """The authoritative seed snapshot must be unchanged since evaluation.

    The supplied CSVs are immutable sample data; each evaluation records the
    hash of the snapshot it ran against. If the CSVs changed, or the SQLite
    seed tables no longer mirror them, the evaluation is stale and cannot be
    checked, released or confirmed until re-evaluated.
    """
    eval_row = conn.execute("SELECT seed_hash FROM evaluations WHERE offer_id=?",
                            (version_row["offer_id"],)).fetchone()
    if eval_row is None:
        return ["seed_evaluation_missing"]
    data = seed_data()
    if eval_row["seed_hash"] != seed_hash(data):
        return ["seed_data_stale"]
    return _mirror_errors(conn, data)


@_write_locked
def check_draft(conn, version_id, commit=True):
    version = conn.execute("SELECT * FROM proposal_versions WHERE id=?", (version_id,)).fetchone()
    deal = conn.execute("SELECT * FROM deals WHERE id=?", (version["deal_id"],)).fetchone()
    eval_row = conn.execute("SELECT * FROM evaluations WHERE offer_id=?",
                            (version["offer_id"],)).fetchone()
    now = db.now()
    option_rows = options_of(conn, version)
    d = digest(version, option_rows)
    set_errors = option_set_errors(conn, version) + seed_errors(conn, version)
    if set_errors:
        # Terms no longer match the original engine output: refuse before the
        # outbound check and mark the version blocked.
        outcome = {"passed": False, "violations": [{"code": "OPTION_SET_INVALID",
                                                    "text": e} for e in set_errors]}
        conn.execute("UPDATE proposal_versions SET outbound_status='blocked' WHERE id=?",
                     (version_id,))
        _step(conn, deal["id"], "outbound_check", "blocked", j(outcome["violations"]),
              version["offer_id"])
        if commit:
            conn.commit()
        return outcome
    result = uj(eval_row["result_json"])
    est = result.get("estimate")
    option_rows = options_of(conn, version)
    approved = [uj(r["public_terms_json"]) for r in option_rows]
    privates = {}
    if est:
        privates = {"floor_sgd": est["floor_sgd"], "min_hourly_rate_sgd": 70,
                    "capacity_hours": est["capacity_hours"]}
        for key, value in (result.get("option_metrics") or {}).items():
            if value:
                privates[f"opt_{key}_floor"] = value["floor_sgd"]
    payload = {"draft": version["draft_body"], "approved_options": approved,
               "private_values": privates,
               "other_client_names": other_client_names(conn, deal["client"])}
    outcome = run_outbound_check(payload)  # real engine call
    status = "passed" if outcome["passed"] else "blocked"
    conn.execute("UPDATE proposal_versions SET outbound_status=?, checked_digest=? WHERE id=?",
                 (status, d, version_id))
    conn.execute(
        "INSERT INTO messages (id, deal_id, version_id, direction, body, option_ids_json,"
        " check_json, request_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (db.new_id(), deal["id"], version_id, "outbound_check", version["draft_body"],
         j([r["id"] for r in option_rows]), j(outcome), None, now))
    _step(conn, deal["id"], "outbound_check", status, j(outcome["violations"]),
          version["offer_id"])
    if version["status"] == "checked" and not outcome["passed"]:
        conn.execute("UPDATE proposal_versions SET status='blocked' WHERE id=?", (version_id,))
    elif version["status"] in ("draft", "blocked") and outcome["passed"]:
        conn.execute("UPDATE proposal_versions SET status='checked' WHERE id=?", (version_id,))
    if commit:
        conn.commit()
    return outcome


@_write_locked
def save_draft(conn, version_id, body, commit=True):
    """Editing text creates a fresh version; old check/approval become ineligible.

    commit=False keeps the transaction open so a caller can append further
    writes (e.g. a durable idempotency record) atomically before committing.
    """
    version = conn.execute("SELECT * FROM proposal_versions WHERE id=?", (version_id,)).fetchone()
    deal_id = version["deal_id"]
    now = db.now()
    prior = conn.execute("SELECT MAX(version_no) AS v FROM proposal_versions WHERE deal_id=?",
                         (deal_id,)).fetchone()["v"]
    option_ids = uj(version["public_option_ids_json"])
    new_version = db.new_id()
    conn.execute(
        "INSERT INTO proposal_versions (id, deal_id, offer_id, previous_version_id, version_no,"
        " status, draft_body, outbound_status, checked_digest, public_option_ids_json,"
        " token_hash, selected_option_id, published_at, accepted_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_version, deal_id, version["offer_id"], version["id"], prior + 1, "draft", body,
         "unchecked", None, j(option_ids), None, None, None, None, now))
    conn.execute("UPDATE proposal_versions SET import_option_keys_json=? WHERE id=?",
                 (version["import_option_keys_json"], new_version))
    _step(conn, deal_id, "edit_draft", "ok", "New version created; fresh check required",
          version["offer_id"])
    if commit:
        conn.commit()
    return new_version


# ---- release boundary -----------------------------------------------------

def approve(conn, version_id, selected_option_id=None, actor="alex"):
    """Validate and release under a write transaction.

    A BEGIN IMMEDIATE write lock is acquired BEFORE rereading the latest
    version, draft, checked digest and option evidence; validation and the
    approve+release writes then happen atomically inside that transaction.
    """
    now = db.now()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return {"ok": False, "reasons": ["busy"]}
    try:
        version = conn.execute("SELECT * FROM proposal_versions WHERE id=?",
                               (version_id,)).fetchone()
        if version is None:
            conn.rollback()
            return {"ok": False, "reasons": ["version_not_found"]}
        deal = conn.execute("SELECT * FROM deals WHERE id=?",
                            (version["deal_id"],)).fetchone()
        reasons = []
        latest = conn.execute(
            "SELECT id, version_no FROM proposal_versions WHERE deal_id=?"
            " ORDER BY version_no DESC LIMIT 1", (deal["id"],)).fetchone()
        if latest["id"] != version_id:
            reasons.append("not_latest_version")
        if version["outbound_status"] != "passed":
            reasons.append("outbound_not_passed")
        if version["status"] not in ("checked", "draft"):
            reasons.append(f"status={version['status']}")
        option_rows = options_of(conn, version)
        reasons.extend(option_set_errors(conn, version))
        reasons.extend(seed_errors(conn, version))
        if deal["status"] in ("confirmed", "closed"):
            reasons.append("manual_review_required: confirmed booking cannot be re-released")
        # Stage 11: revalidate every option offered in the exact public set
        # against current residual capacity under this lock. Terms are never
        # changed; an unsafe set blocks release and needs a fresh evaluation.
        eval_row = conn.execute("SELECT * FROM evaluations WHERE offer_id=?",
                                (version["offer_id"],)).fetchone()
        snapshot_validated = None
        if eval_row is not None and not reasons:
            result = uj(eval_row["result_json"])
            as_of_row = conn.execute("SELECT as_of FROM offers WHERE id=?",
                                     (version["offer_id"],)).fetchone()
            as_of = as_of_row["as_of"] if as_of_row else None
            terms_list = [uj(r["public_terms_json"]) for r in option_rows]
            widest = max((t.get("phases") or [{}])[-1].get("deadline", t["deadline"])
                         for t in terms_list)
            led = cap.ledger(conn, as_of=as_of, deadline=widest)
            try:
                unsafe = [t["option_id"] for t in terms_list
                          if cap.plan(led, cap.option_demands(result, t)) is None]
            except (KeyError, TypeError, ValueError):
                unsafe = [t["option_id"] for t in terms_list]
            if unsafe:
                reasons.append("capacity_changed")
                reasons.append("review_required")
                reasons.append("unsafe_options:" + ",".join(unsafe))
            else:
                snapshot_validated = j({"as_of": as_of, "revision": cap.ledger_revision(conn),
                                        "weeks": [{"week": w, **snap}
                                                  for w, snap in sorted(led.items())]})
        if digest(version, option_rows) != version["checked_digest"]:
            reasons.append("digest_mismatch")
        if selected_option_id and selected_option_id not in uj(
                version["public_option_ids_json"]):
            reasons.append("option_not_in_version")
        if reasons:
            conn.execute(
                "INSERT INTO approvals (id, version_id, actor_id, action, reviewed_digest,"
                " note, decided_at) VALUES (?,?,?,?,?,?,?)",
                (db.new_id(), version_id, actor, "approve_rejected",
                 version["checked_digest"], "; ".join(reasons), now))
            _step(conn, deal["id"], "approve", "error", "; ".join(reasons),
                  version["offer_id"], trigger="manual")
            conn.commit()
            return {"ok": False, "reasons": reasons}

        token = secrets.token_urlsafe(24)  # 192 bits
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        conn.execute(
            "UPDATE proposal_versions SET status='published', token_hash=?,"
            " selected_option_id=?, published_at=? WHERE id=?",
            (token_hash, selected_option_id, now, version_id))
        conn.execute("UPDATE proposal_versions SET status='superseded', published_at=NULL"
                     " WHERE deal_id=? AND status='published' AND id<>?",
                     (deal["id"], version_id))
        conn.execute(
            "INSERT INTO approvals (id, version_id, actor_id, action, reviewed_digest, note,"
            " decided_at) VALUES (?,?,?,?,?,?,?)",
            (db.new_id(), version_id, actor, "approve", digest(version, option_rows),
             "Released to client", now))
        conn.execute("UPDATE deals SET status='published', current_version_id=? WHERE id=?",
                     (version_id, deal["id"]))
        _step(conn, deal["id"], "approve", "ok", "Published version released",
              version["offer_id"], trigger="manual")
        if snapshot_validated:
            _step(conn, deal["id"], "capacity_revalidate", "ok",
                  f"Options revalidated safe against current capacity; snapshot="
                  f"{snapshot_validated}", version["offer_id"], trigger="manual")
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        return {"ok": False, "reasons": ["atomic_failure"]}
    return {"ok": True, "token": token, "url": f"/p/{token}"}


def reject(conn, version_id, note="", actor="alex"):
    version = conn.execute("SELECT * FROM proposal_versions WHERE id=?", (version_id,)).fetchone()
    now = db.now()
    conn.execute(
        "INSERT INTO approvals (id, version_id, actor_id, action, reviewed_digest, note,"
        " decided_at) VALUES (?,?,?,?,?,?,?)",
        (db.new_id(), version_id, actor, "reject", version["checked_digest"], note, now))
    conn.execute("UPDATE proposal_versions SET status='rejected' WHERE id=?", (version_id,))
    _step(conn, version["deal_id"], "reject", "ok", "Rejected; nothing released",
          version["offer_id"], trigger="manual")
    conn.commit()
    return {"ok": True}


# ---- client side ----------------------------------------------------------

def published_by_token(conn, token):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return conn.execute("SELECT * FROM proposal_versions WHERE token_hash=? AND status='published'",
                        (token_hash,)).fetchone()


@_write_locked
def client_accept(conn, token, option_id):
    version = published_by_token(conn, token)
    if not version:
        return {"ok": False, "http": 404}
    if not option_id:
        return {"ok": False, "error": "Missing option_id"}
    if option_id not in uj(version["public_option_ids_json"]):
        return {"ok": False, "error": "Option does not belong to this version"}
    # Cross-deal / cross-version option: the option row must come from the
    # evaluation of this version's own offer.
    eval_row = conn.execute("SELECT id FROM evaluations WHERE offer_id=?",
                            (version["offer_id"],)).fetchone()
    opt_row = conn.execute("SELECT evaluation_id FROM options WHERE id=?", (option_id,)).fetchone()
    if not eval_row or not opt_row or opt_row["evaluation_id"] != eval_row["id"]:
        return {"ok": False, "error": "Option does not belong to this version"}
    # A newer published version supersedes acceptance on an older one.
    newer = conn.execute(
        "SELECT id FROM proposal_versions WHERE deal_id=? AND version_no>?"
        " AND status IN ('published','superseded') ORDER BY version_no DESC LIMIT 1",
        (version["deal_id"], version["version_no"])).fetchone()
    if newer:
        return {"ok": False, "error": "Stale version; a newer proposal exists"}
    existing = conn.execute(
        "SELECT 1 FROM messages WHERE version_id=? AND direction='acceptance'",
        (version["id"],)).fetchone()
    if existing:
        return {"ok": False, "error": "Already accepted; pending founder confirmation"}
    now = db.now()
    conn.execute(
        "INSERT INTO messages (id, deal_id, version_id, direction, body, option_ids_json,"
        " check_json, request_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (db.new_id(), version["deal_id"], version["id"], "acceptance",
         "Client accepted option (pending founder confirmation)", j([option_id]), None,
         f"accept:{version['id']}", now))
    conn.execute("UPDATE proposal_versions SET accepted_at=? WHERE id=?", (now, version["id"]))
    _step(conn, version["deal_id"], "client_accept", "ok", "Pending founder confirmation",
          version["offer_id"])
    conn.commit()
    return {"ok": True, "pending": True}


def current_version(conn, deal_id):
    return conn.execute(
        "SELECT * FROM proposal_versions WHERE deal_id=? AND status='published'"
        " ORDER BY version_no DESC LIMIT 1", (deal_id,)).fetchone()


# ---- token reissue & booking confirmation ---------------------------------

def rotate_token(conn, version_id, actor="alex"):
    """Owner-only reissue: atomically invalidates the old client token.

    Only the token hash lives in durable storage; if the browser lost the
    raw token, the founder can reissue here. The old /p/ URL stops resolving
    immediately and the action is audited. The raw token is never logged.
    """
    now = db.now()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return {"ok": False, "reasons": ["busy"]}
    try:
        version = conn.execute("SELECT * FROM proposal_versions WHERE id=?",
                               (version_id,)).fetchone()
        if version is None:
            conn.rollback()
            return {"ok": False, "reasons": ["version_not_found"]}
        if version["status"] not in ("published", "confirmed"):
            conn.rollback()
            return {"ok": False, "reasons": [f"status={version['status']}"]}
        token = secrets.token_urlsafe(24)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        conn.execute("UPDATE proposal_versions SET token_hash=? WHERE id=?",
                     (token_hash, version_id))
        conn.execute(
            "INSERT INTO approvals (id, version_id, actor_id, action, reviewed_digest,"
            " note, decided_at) VALUES (?,?,?,?,?,?,?)",
            (db.new_id(), version_id, actor, "rotate_token", version["checked_digest"],
             "Client token reissued; previous token invalidated", now))
        _step(conn, version["deal_id"], "rotate_token", "ok",
              "Client access token reissued (old token invalidated)", version["offer_id"],
              trigger="manual")
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        return {"ok": False, "reasons": ["atomic_failure"]}
    return {"ok": True, "token": token, "url": f"/p/{token}"}


def _newer_version(conn, deal_id, version_no):
    return conn.execute(
        "SELECT id FROM proposal_versions WHERE deal_id=? AND version_no>?"
        " AND status IN ('published','superseded','confirmed') ORDER BY version_no"
        " DESC LIMIT 1", (deal_id, version_no)).fetchone()


def confirm_booking(conn, version_id, actor="alex"):
    """Founder-only booking confirmation on a pending client acceptance.

    Atomic and idempotent: requires a currently published version with a
    recorded acceptance, no newer version, and fresh seed data. Under one
    BEGIN IMMEDIATE lock the accepted version/option and the current ledger
    are reread, the original safeguards validated, exact-cent weekly
    allocations built from residual capacity, and reservation + allocations +
    confirmation + status + audit inserted atomically. A competing or unsafe
    confirmation fails with capacity_changed/review_required and leaves the
    acceptance pending — no partial allocations, no confirmed flag.
    """
    now = db.now()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return {"ok": False, "reasons": ["busy"]}
    try:
        version = conn.execute("SELECT * FROM proposal_versions WHERE id=?",
                               (version_id,)).fetchone()
        if version is None:
            conn.rollback()
            return {"ok": False, "reasons": ["version_not_found"]}
        deal = conn.execute("SELECT * FROM deals WHERE id=?",
                            (version["deal_id"],)).fetchone()
        if version["status"] == "confirmed":
            prior = conn.execute(
                "SELECT option_id, id FROM confirmations WHERE version_id=?",
                (version_id,)).fetchone()
            reservation = conn.execute(
                "SELECT * FROM reservations WHERE version_id=?", (version_id,)).fetchone()
            conn.rollback()
            return {"ok": True, "already": True,
                    "option_id": prior["option_id"] if prior else None,
                    "reservation_id": reservation["id"] if reservation else None}
        reasons = []
        if version["status"] != "published":
            reasons.append(f"status={version['status']}")
        if not version["accepted_at"]:
            reasons.append("no_pending_acceptance")
        if _newer_version(conn, deal["id"], version["version_no"]):
            reasons.append("stale_version")
        reasons.extend(seed_errors(conn, version))
        reasons.extend(option_set_errors(conn, version))
        accept_row = conn.execute(
            "SELECT option_ids_json FROM messages WHERE version_id=?"
            " AND direction='acceptance'", (version_id,)).fetchone()
        accepted_option = (uj(accept_row["option_ids_json"])[0]
                           if accept_row and uj(accept_row["option_ids_json"]) else None)
        if accepted_option is None or accepted_option not in (
                uj(version["public_option_ids_json"]) or []):
            reasons.append("acceptance_option_missing")
        if reasons:
            conn.rollback()
            return {"ok": False, "reasons": reasons}

        # ---- capacity reservation under the same lock ---------------------
        opt_row = conn.execute("SELECT * FROM options WHERE id=?",
                               (accepted_option,)).fetchone()
        eval_row = conn.execute("SELECT * FROM evaluations WHERE offer_id=?",
                                (version["offer_id"],)).fetchone()
        as_of_row = conn.execute("SELECT as_of FROM offers WHERE id=?",
                                 (version["offer_id"],)).fetchone()
        if opt_row is None or eval_row is None or as_of_row is None:
            conn.rollback()
            return {"ok": False, "reasons": ["acceptance_option_missing"]}
        terms = uj(opt_row["public_terms_json"])
        result = uj(eval_row["result_json"])
        as_of = as_of_row["as_of"]
        try:
            demands = cap.option_demands(result, terms)
            total_cents = sum(need for need, _deadline in demands)
            led = cap.ledger(conn, as_of=as_of, deadline=terms["deadline"])
            allocation = cap.plan(led, demands)
        except (KeyError, TypeError, ValueError):
            # Unsupported option shape or metric/date mismatch: fail closed.
            conn.rollback()
            return {"ok": False, "reasons": ["option_metric_mismatch"]}
        if allocation is None:
            # Infeasible against current residual capacity (another deal
            # confirmed first, or the calendar changed): fail safely, keep
            # the acceptance pending, and point at the next action.
            _step(conn, deal["id"], "confirm_booking", "capacity_changed",
                  "Unsafe against current capacity: re-evaluate the offer before"
                  " releasing or confirming again", version["offer_id"], trigger="manual")
            conn.commit()
            return {"ok": False, "reasons": ["capacity_changed", "review_required"],
                    "next_action": "Re-evaluate this offer against current capacity,"
                                   " then check, review and release a fresh version."}

        # ---- atomic durable writes ----------------------------------------
        revision = cap.ledger_revision(conn) + 1
        after = {w: dict(s) for w, s in led.items()}
        for week, cents, _milestone in allocation:
            after[week]["allocated"] += cents
            after[week]["residual"] -= cents
        snapshot = j({"as_of": as_of, "revision": revision,
                      "weeks": [{"week": w, **s} for w, s in sorted(after.items())]})
        confirmation_id = db.new_id()
        reservation_id = db.new_id()
        conn.execute(
            "INSERT INTO capacity_revisions (revision, seed_hash, snapshot_json,"
            " created_at) VALUES (?,?,?,?)",
            (revision, seed_hash(seed_data()), snapshot, now))
        conn.execute(
            "INSERT INTO reservations (id, deal_id, version_id, option_id,"
            " confirmation_id, capacity_revision, status, total_planning_hundredths,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (reservation_id, deal["id"], version_id, accepted_option, confirmation_id,
             revision, "active", total_cents, now))
        for week, cents, milestone in allocation:
            conn.execute(
                "INSERT INTO allocations (id, reservation_id, week_ending,"
                " planning_hundredths, milestone, created_at) VALUES (?,?,?,?,?,?)",
                (db.new_id(), reservation_id, week, cents, milestone, now))
        conn.execute("UPDATE proposal_versions SET status='confirmed' WHERE id=?",
                     (version_id,))
        conn.execute("UPDATE deals SET status='confirmed' WHERE id=?", (deal["id"],))
        conn.execute("INSERT INTO confirmations (id, deal_id, version_id, option_id,"
                     " actor_id, decided_at) VALUES (?,?,?,?,?,?)",
                     (confirmation_id, deal["id"], version_id, accepted_option, actor, now))
        conn.execute(
            "INSERT INTO approvals (id, version_id, actor_id, action, reviewed_digest,"
            " note, decided_at) VALUES (?,?,?,?,?,?,?)",
            (db.new_id(), version_id, actor, "confirm_booking",
             version["checked_digest"],
             f"Sample booking confirmed (option {accepted_option}); reserved"
             f" {total_cents / 100:.2f} planning hours over {len(allocation)} weeks"
             f" at capacity revision {revision}", now))
        _step(conn, deal["id"], "confirm_booking", "ok",
              f"Booking confirmed on option {accepted_option}; reserved"
              f" {total_cents / 100:.2f} h", version["offer_id"], trigger="manual")
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        return {"ok": False, "reasons": ["atomic_failure"]}
    return {"ok": True, "option_id": accepted_option,
            "reservation_id": reservation_id,
            "allocation": [{"week": w, "hours": c / 100} for w, c, _m in allocation],
            "capacity_revision": revision}


def close_deal(conn, deal_id, actor="alex"):
    """Move a confirmed deal to closed. Sample booking only: nothing is sent."""
    deal = conn.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
    if deal is None:
        return {"ok": False, "reasons": ["deal_not_found"]}
    if deal["status"] == "closed":
        return {"ok": True, "already": True}
    if deal["status"] != "confirmed":
        return {"ok": False, "reasons": [f"status={deal['status']}"]}
    now = db.now()
    conn.execute("UPDATE deals SET status='closed' WHERE id=?", (deal_id,))
    version = conn.execute(
        "SELECT * FROM proposal_versions WHERE deal_id=? AND status='confirmed'"
        " ORDER BY version_no DESC LIMIT 1", (deal_id,)).fetchone()
    conn.execute(
        "INSERT INTO approvals (id, version_id, actor_id, action, reviewed_digest,"
        " note, decided_at) VALUES (?,?,?,?,?,?,?)",
        (db.new_id(), version["id"] if version else None, actor, "close_deal",
         version["checked_digest"] if version else None,
         "Deal closed (sample booking only; nothing sent)", now))
    _step(conn, deal_id, "close_deal", "ok", "Deal closed", None, trigger="manual")
    conn.commit()
    return {"ok": True}


def _step(conn, deal_id, key, status, note, offer_id=None, trigger=None):
    conn.execute(
        "INSERT INTO run_steps (id, deal_id, offer_id, step_key, status, private_note,"
        ' started_at, finished_at, "trigger") VALUES (?,?,?,?,?,?,?,?,?)',
        (db.new_id(), deal_id, offer_id, key, status, note, db.now(), db.now(), trigger))
