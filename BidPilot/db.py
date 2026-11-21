"""BidPilot demo storage. SQLite, schema per BUILD_GUIDE section 5."""
import json
import sqlite3
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS seed_business_rules (key TEXT PRIMARY KEY, value REAL, meaning TEXT);
CREATE TABLE IF NOT EXISTS seed_effort_rules (rule TEXT PRIMARY KEY, hours TEXT, applies TEXT);
CREATE TABLE IF NOT EXISTS seed_rate_card (item TEXT PRIMARY KEY, price_sgd REAL, unit TEXT);
CREATE TABLE IF NOT EXISTS seed_past_projects (project_id TEXT PRIMARY KEY, client TEXT, format TEXT,
  production_mode TEXT, shoot_days INTEGER, locations INTEGER, scripting TEXT, deliverables INTEGER,
  editing_level TEXT, graphics TEXT, revision_rounds INTEGER, caption_versions INTEGER,
  est_hours REAL, actual_hours REAL, price_sgd REAL, start TEXT, end TEXT);
CREATE TABLE IF NOT EXISTS seed_calendar_weeks (week_ending TEXT PRIMARY KEY, working_hours REAL,
  calendar_busy_hours REAL, unscheduled_owed_hours REAL, note TEXT);

CREATE TABLE IF NOT EXISTS deals (id TEXT PRIMARY KEY, source TEXT, source_id TEXT, client TEXT,
  raw_inquiry TEXT, status TEXT, current_version_id TEXT, sample_data INTEGER DEFAULT 1,
  created_at REAL, UNIQUE(source, source_id));
CREATE TABLE IF NOT EXISTS offers (id TEXT PRIMARY KEY, deal_id TEXT, source TEXT, source_id TEXT,
  from_version_id TEXT, round INTEGER, as_of TEXT, offer_json TEXT, evidence_json TEXT,
  comparable_ids_json TEXT, created_at REAL, UNIQUE(source, source_id));
CREATE TABLE IF NOT EXISTS evaluations (id TEXT PRIMARY KEY, offer_id TEXT UNIQUE, verdict TEXT,
  reason_codes_json TEXT, result_json TEXT, engine_version TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS options (id TEXT PRIMARY KEY, evaluation_id TEXT, option_key TEXT,
  kind TEXT, public_terms_json TEXT, created_at REAL, UNIQUE(evaluation_id, option_key));
CREATE TABLE IF NOT EXISTS proposal_versions (id TEXT PRIMARY KEY, deal_id TEXT, offer_id TEXT,
  previous_version_id TEXT, version_no INTEGER, status TEXT, draft_body TEXT, outbound_status TEXT,
  checked_digest TEXT, public_option_ids_json TEXT, token_hash TEXT UNIQUE, selected_option_id TEXT,
  published_at REAL, accepted_at REAL, created_at REAL, UNIQUE(deal_id, version_no));
CREATE TABLE IF NOT EXISTS run_steps (id TEXT PRIMARY KEY, deal_id TEXT, offer_id TEXT,
  step_key TEXT, status TEXT, private_note TEXT, started_at REAL, finished_at REAL);
CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, deal_id TEXT, version_id TEXT,
  direction TEXT, body TEXT, option_ids_json TEXT, check_json TEXT, request_id TEXT UNIQUE,
  created_at REAL);
CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, version_id TEXT, actor_id TEXT,
  action TEXT, reviewed_digest TEXT, note TEXT, decided_at REAL);
CREATE TABLE IF NOT EXISTS confirmations (id TEXT PRIMARY KEY, deal_id TEXT, version_id TEXT,
  option_id TEXT, actor_id TEXT, decided_at REAL);
CREATE TABLE IF NOT EXISTS inbox (id TEXT PRIMARY KEY, source TEXT DEFAULT 'site',
  source_id TEXT UNIQUE, fields_json TEXT, raw_message TEXT, received_at REAL,
  sample_data INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS agent_outputs (id TEXT PRIMARY KEY, deal_id TEXT, offer_id TEXT,
  kind TEXT, payload_json TEXT, request_id TEXT UNIQUE, created_at REAL);
CREATE TABLE IF NOT EXISTS learnings (id TEXT PRIMARY KEY, deal_id TEXT, source TEXT,
  body TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS mcp_operations (request_id TEXT PRIMARY KEY, tool TEXT,
  payload_hash TEXT, result_json TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, deal_id TEXT,
  version_id TEXT UNIQUE, option_id TEXT, confirmation_id TEXT UNIQUE,
  capacity_revision INTEGER, status TEXT DEFAULT 'active',
  total_planning_hundredths INTEGER, created_at REAL);
CREATE TABLE IF NOT EXISTS allocations (id TEXT PRIMARY KEY, reservation_id TEXT,
  week_ending TEXT, planning_hundredths INTEGER, milestone TEXT, created_at REAL,
  UNIQUE(reservation_id, week_ending));
CREATE TABLE IF NOT EXISTS inquiry_proposal_links (
 version_id TEXT PRIMARY KEY, released_hash TEXT NOT NULL, alias_hash TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS capacity_impacts (
 reservation_id TEXT, version_id TEXT, details TEXT NOT NULL, PRIMARY KEY(reservation_id, version_id));
CREATE TABLE IF NOT EXISTS capacity_revisions (revision INTEGER PRIMARY KEY,
  seed_hash TEXT, snapshot_json TEXT, created_at REAL);
"""


_MIGRATIONS = [
    ("proposal_versions", "page_json", "ALTER TABLE proposal_versions ADD COLUMN page_json TEXT"),
    ("proposal_versions", "import_option_keys_json",
     "ALTER TABLE proposal_versions ADD COLUMN import_option_keys_json TEXT"),
    ("mcp_operations", "deal_id",
     "ALTER TABLE mcp_operations ADD COLUMN deal_id TEXT"),
    # Phase 5: record the authoritative seed snapshot hash per evaluation.
    ("evaluations", "seed_hash", "ALTER TABLE evaluations ADD COLUMN seed_hash TEXT"),
    # Stage 11: capacity snapshot/revision the evaluation ran against.
    ("evaluations", "capacity_revision",
     "ALTER TABLE evaluations ADD COLUMN capacity_revision INTEGER"),
    ("evaluations", "capacity_snapshot_json",
     "ALTER TABLE evaluations ADD COLUMN capacity_snapshot_json TEXT"),
    # Stage 15: optional manual/scheduled trigger recorded per run step.
    ("run_steps", "trigger",
     'ALTER TABLE run_steps ADD COLUMN "trigger" TEXT'),
    # Bind inbound retries to the original change payload.
    ("messages", "payload_hash", "ALTER TABLE messages ADD COLUMN payload_hash TEXT"),
    # Conversation worker leases: one agent owns an inquiry's pending turns.
    ("inquiry_jobs", "lease_owner", "ALTER TABLE inquiry_jobs ADD COLUMN lease_owner TEXT"),
]

def _ensure_columns(conn):
    def has_column(table, column):
        return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))

    if all(has_column(table, column) for table, column, _ in _MIGRATIONS):
        return
    # The dashboard and stdio server can open the same older DB concurrently.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        for table, column, ddl in _MIGRATIONS:
            if not has_column(table, column):
                conn.execute(ddl)


def now():
    return round(time.time(), 3)


def new_id():
    return str(uuid.uuid4())


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        from studio_modules import modules
        import inquiry_quote
        conn.executescript(SCHEMA + inquiry_quote.SCHEMA + "".join(module.SCHEMA for module in modules()))
        _ensure_columns(conn)
    except Exception:
        conn.close()
        raise
    return conn


def j(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=False)


def uj(text):
    return json.loads(text)
