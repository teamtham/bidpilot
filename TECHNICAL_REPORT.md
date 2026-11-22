# BidPilot Technical Report

Developer handover for `/Users/teamtham/hackathon`. This is the only deliverable created by this audit; application code, configuration, existing artifacts and dirty-worktree changes were preserved. No publication was requested or performed.

**Evidence conventions.** Repository-relative `file:line` citations identify source evidence. **Audit execution evidence** identifies supplied fresh verification results or the authorized engine/data auditor's saved results; execution results are not assigned fictitious source lines. **Inference** identifies consequences deduced from implementation. Historical handoff/evidence reports are not current runtime verification. Tokens, complete private capability URLs, credentials, runtime logs and database contents are omitted.

**Coverage limits.** Primary interface/operations sources, all repository test source files, six current installed BidPilot skill instructions, repository guides/docs, connector JSON and stored JSON reports were read across the audit. The engine/data auditor read both repository engine copies and application integration sources. Some single-line HTML/JavaScript, MCP schema descriptions and fixture JSON exceeded the file reader's 2,000-character line limit; those exact long lines were not completely visible. Binary screenshots were inventoried, not read as code or visually certified. `ffprobe` was absent from PATH, so video metadata was not inspected. Installed `.SKILL.md.bak` files and installed engine companion-source/data parity were not certified. No runtime log, credential file, live DB or private media cache was read. The authorized saved engine audit was read through EOF; it is evidence, not executable instructions.

## 1. Overview, Stack And Quick Start

BidPilot prepares sample video-production proposals through QwenWork intake/scoping/negotiation and deterministic Python tools. (`README.md:3-10,49-61`)
A buyer submits a structured quote or private inquiry, optionally with reference video. (`BidPilot/server.py:191-192,346-411`; `BidPilot/conversation.py:445-485`)
Python stores scope/evaluation/version evidence in SQLite and produces public option terms. (`BidPilot/core.py:478-529`; `BidPilot/db.py:18-45`)
An authenticated founder releases the checked exact version; buyer selection remains pending until founder confirmation. (`BidPilot/core.py:840-946,1033-1076,1134-1285`)
This is a loopback, synthetic-data demo, not a production-hardened multi-tenant deployment. (`README.md:108-129`; `docs/OPERATIONS.md:152-158`)

| Layer | Stack / runtime | Source |
|---|---|---|
| Web | Python `ThreadingHTTPServer`, `BaseHTTPRequestHandler`, embedded HTML/CSS/JS | `BidPilot/server.py:19,99,2044-2101` |
| Persistence | SQLite; JSON serialized in TEXT; UUID IDs and epoch timestamps | `BidPilot/db.py:102-129` |
| Agent transport | Custom newline-delimited stdio JSON-RPC MCP, protocol 2024-11-05 | `BidPilot/mcp_server.py:13,36-39,697-818` |
| Project connector | Dependency-free Node stdio MCP, protocol 2025-03-26; git/rg/npm subprocesses | `tools/qwenwork-mcp-bridge.js:10-19,162-223,229-249` |
| Media | FFmpeg/PATH/imageio-ffmpeg; Tesseract or macOS Swift/Vision; optional whisper.cpp | `BidPilot/conversation.py:284-292,317-385`; `BidPilot/media_probe.swift:1-4` |
| Tests | unittest; optional Node syntax checks, Playwright Chromium | `.github/workflows/tests.yml:13-22`; `BidPilot/test_stage7.py:137-155`; `BidPilot/test_stage9.py:23-28,63-77` |

Documented requirement is Python 3.11+; CI selects 3.11. Fresh supplied execution evidence found PATH `python3` 3.9.6 and `.venv/bin/python` 3.13.7. Use a verified >=3.11 interpreter to create the environment rather than assuming PATH satisfies the documentation. (`docs/RUNNING.md:3-8`; `.github/workflows/tests.yml:10-12`; **Audit execution evidence**)

Run from repository root; installation commands are documentation, not actions performed by this report:

```sh
python3 --version
# Only if that interpreter is Python 3.11 or later:
python3 -m venv .venv
.venv/bin/python -m pip install -r BidPilot/requirements-media.txt
.venv/bin/python -B BidPilot/server.py
# Optional browser tests:
.venv/bin/python -m pip install playwright
.venv/bin/python -m playwright install chromium
```

Sources: `docs/RUNNING.md:3-8,48-53`. Requirement file pins `imageio-ffmpeg==0.8.1`; Playwright is separately installed, unpinned in CI. (`BidPilot/requirements-media.txt:1`; `.github/workflows/tests.yml:15`)

Server output is `BidPilot founder dashboard: http://127.0.0.1:<port>/?owner=<REDACTED>`. This is a format, not an actual capability. Default port 8765/database `BidPilot/bidpilot.db`; public entries are `http://127.0.0.1:8765/inquire` and `/quote`. (`BidPilot/server.py:32,2091-2101`; `docs/RUNNING.md:11-13`)

```sh
.venv/bin/python -B BidPilot/server.py --port 8765 --db /absolute/private/app.db
# Behind a configured TLS proxy; example origins, not deployed resources:
.venv/bin/python -B BidPilot/server.py --db /absolute/private/app.db \
  --public-origin https://brief.example.com --embed-origin https://studio.example.com
# Product MCP, with explicit interpreter and the same DB:
BIDPILOT_MCP_DB=/absolute/private/app.db .venv/bin/python -B BidPilot/mcp_server.py
# Project connector:
QWENWORK_PROJECT_ROOT=/Users/teamtham/hackathon node tools/qwenwork-mcp-bridge.js
```

Server CLI/source: `BidPilot/server.py:2065-2101`; MCP path precedence: `BidPilot/mcp_server.py:38-39`; project root: `tools/qwenwork-mcp-bridge.js:17`; proxy requirements: `docs/RUNNING.md:42`. Environment names and defaults are cataloged in section 10; no secret environment values were inspected.

## 2. Exact File Inventory And Purpose

Inventory provenance: audit `git ls-files --cached --others --exclude-standard` plus visible directory listings. Inventory is a point-in-time enumeration, not a git-clean claim. Compressed braces below expand to exact filenames; no wildcard silently represents unknown files. The new `TECHNICAL_REPORT.md` is this handover. `.git`, `.venv`, Python caches, logs, live DBs and private runtime media are intentionally outside content inventory.

### 2.1 Application And Scripts

| Exact path | Purpose / source |
|---|---|
| `BidPilot/server.py` | HTTP/auth/dashboard/client/forms/media host (`:99-246,2044-2101`) |
| `BidPilot/mcp_server.py` | MCP registration/ownership/pending discovery (`:590-691,697-822`) |
| `BidPilot/conversation.py` | Inquiry/turn/fact/reference jobs and endpoints (`:22-33,445-513`) |
| `BidPilot/core.py` | Intake, evaluation, version/check/release/booking/closure (`:153-243,471-571,749-1318`) |
| `BidPilot/db.py` | Base schema/bootstrap/migrations/serialization (`:7-129`) |
| `BidPilot/capacity.py` | Reservation ledger/adapted calendar/booking demands (`:136-179`; `docs/REFERENCE_TO_QUOTE_PLAN.md:138`) |
| `BidPilot/dashboard_ops.py` | Owner transactional run import/sample reset (`:15-213`) |
| `BidPilot/commerce.py` | Proposal composition/additive amendments/closeout (`:1-4,743-785`) |
| `BidPilot/estimating.py` | Historical/detailed estimates/resource scheduling/alternatives (`:133-200,285-387,447-610,1179-1205`) |
| `BidPilot/inquiry_quote.py` | Inquiry revision-to-priced offer binding (`:14-100`) |
| `BidPilot/portfolio.py` | Shared portfolio/search/frame/media interfaces (`:17-26,185-282`) |
| `BidPilot/studio_modules.py` | Shared module order: conversation, estimating, commerce, portfolio (`:4-9`) |
| `BidPilot/media_probe.swift` | macOS frame/OCR/reference decoder (`:1-99`) |
| `BidPilot/requirements-media.txt` | Pinned media dependency (`:1`) |
| `BidPilot/scripts/backtest_estimates.py` | Held-out supplied-prediction/database replay scorer (`:18-124`) |
| `BidPilot/scripts/synthetic_calibration.py` | Synthetic rolling-history calibration/report CLI (`:43-178`) |
| `BidPilot/scripts/demo_smoke.py` | Temporary HTTP/MCP fixture lifecycle replay (`:72-217`) |
| `BidPilot/scripts/operations_worker.py` | Recovery-scan/dispatch CLI (`:20-70`) |
| `BidPilot/scripts/qwenwork_adapter.py` | Read-only scheduled-consumer completion verification (`:31-96`) |
| `BidPilot/operations.py` | Studio isolation, leased queue, metrics, snapshots/retention/revocation (`:205-368,377-1171`) |
| `tools/qwenwork-mcp-bridge.js` | Project read/search/check/handoff connector (`:102-223`) |
| `bidpilot-embed/index.html` | Iframe shell using fixed temporary buyer-service origin, redacted here (`:19-24`) |
| `operations_worker.retry-backup.py` | Older worker snapshot, no current completion verifier (`:34-53`) |
| `operations-retry-backup.8B0gAuod/operations.py` | Older operations snapshot, different dispatch/security behavior (`:370-453,700-769`) |
| `operations-retry-backup.8B0gAuod/operations_worker.py` | Older worker snapshot (`:34-53`) |
| `operations-retry-backup.8B0gAuod/test_operations.py` | Older operations tests, outside normal app discovery (`:30-268`; `.github/workflows/tests.yml:20-22`) |

### 2.2 Tests

Exact files under `BidPilot/`:

```text
test_alternatives.py             Scope transformations/pricing
test_calibration.py              Synthetic scoring/determinism
test_commerce.py                 Proposal/amendment/closeout/portfolio
test_conversation.py             Chat/media/evidence/retries/leases
test_demo_smoke.py               Replay subprocess
test_engine_sync.py              Repository engine byte parity
test_estimating.py               Detailed/history/schedules/overrides
test_expanded_lifecycle.py       Cross-module lifecycle seams
test_http.py                     Real HTTP/auth/privacy/change/embedding
test_mcp.py                      Real stdio tool/workflow/retry guards
test_operations.py               Isolation/queue/verifier/backup/retention
test_overnight_regressions.py     HTTP hardening regressions
test_pending_offers.py           Structured-change pending discovery
test_phase5.py                   Full HTTP sample lifecycle
test_qwenwork_configs.py         Config paths/commands/catalog startup
test_regressions.py              Resumption/options/release concurrency
test_stage11.py                  Capacity ledger/booking concurrency
test_stage12_mcp_agents.py       Agent writes/replay/reset/change
test_stage13_intake_form.py      Public quote/receipt/resume proof
test_stage14_dashboard_ops.py    Run import/reset/live updates
test_stage7.py                   Generated JS/explicit change controls
test_stage9.py                   Playwright rendered submissions
test_studio_journey.py           Socket-free inquiry/detailed booking
```

Definition citations and precise coverage are in section 9.

### 2.3 Config, Documentation, Reports And Skills

| Exact path | Purpose / source |
|---|---|
| `.github/workflows/tests.yml` | Ubuntu Python test CI (`:1-22`) |
| `.gitignore` | Runtime/credential/cache exclusions (`:1-18`) |
| `AGENTS.md` | Contributor role/layout/rules (`:3-22`) |
| `CLAUDE.md` | Contributor/build-guide pointer (`:1`) |
| `README.md` | Product/demo reality/setup/deployment/layout (`:1-143`) |
| `QWENWORK_MCP.md` | Connector setup/tool authority (`:1-51`) |
| `QWENWORK_HANDOFF.md` | Historical agent rehearsal/error journal; sensitive capabilities omitted (`:2-160`) |
| `qwenwork-bidpilot-mcp.json` | Product stdio config (`:1-15`) |
| `qwenwork-mcp.json` | Project stdio config (`:1-15`) |
| `docs/DETAILED_ESTIMATE.md` | Detailed input/scheduling/alternatives/history contract (`:1-48`) |
| `docs/OPERATIONS.md` | Pilot isolation/dispatch/retention guide (`:1-165`) |
| `docs/QWENWORK_PROMPT.md` | Historical staged agent/demo setup prompt (`:1-196`) |
| `docs/REFERENCE_TO_QUOTE_PLAN.md` | Proposed full design/acceptance milestones (`:1-140`) |
| `docs/RUNNING.md` | Local/hosted setup and consumer instructions (`:1-56`) |
| `docs/SUBMISSION.md` | Submission/recording plan/historical observation (`:1-46`) |
| `bidpilot-build-guide/BUILD_GUIDE.md` | Engine/build/platform/demo spec (`:1-189`) |
| `bidpilot-build-guide/BIDPILOT_QWENWORK_BUILD_PROMPT.txt` | Private staged build prompt (`:1-21`) |
| `alexstudio-q2-evidence/check-report.json` | Stored hosted checks/form controls (`:1-383`) |
| `synthetic-calibration-report.json` | Stored synthetic aggregate scores (`:1-105`) |
| `TECHNICAL_REPORT.md` | This consolidated audit handover |

Both exact directories `BidPilot/bidpilot-engine/` and `bidpilot-build-guide/bidpilot-engine/` contain the following exact relative files: `SKILL.md`, `engine.py`, `test_engine.py`, `test_cases.json`, `offer-input.schema.json`, `evaluation-output.schema.json`, `outbound-output.schema.json`, `data/business_rules.csv`, `data/calendar_weeks.csv`, `data/effort_rules.csv`, `data/past_projects.csv`, `data/rate_card.csv`. Purposes respectively: execution instruction; pure evaluator/checker; engine tests; fixtures; input/evaluation/checker contracts; thresholds/calendar/effort/history/prices. (`bidpilot-build-guide/BUILD_GUIDE.md:7,23,37,47,60`; `BidPilot/test_engine_sync.py:7-22`)

Other exact repo skill files are `bidpilot-build-guide/bidpilot-intake/SKILL.md`, `bidpilot-build-guide/bidpilot-scoping/SKILL.md`, `bidpilot-build-guide/bidpilot-negotiator/SKILL.md`, `bidpilot-build-guide/bidpilot-run/SKILL.md`, `bidpilot-build-guide/bidpilot-runtime/SKILL.md`; roles are cataloged in section 6. (`README.md:57-64`)

Installed inventory outside repo: `/Users/teamtham/.qwenwork/skills/bidpilot-{engine,intake,scoping,negotiator,run,runtime}/SKILL.md`; installed engine has the eleven companion files listed above excluding SKILL.md. `.SKILL.md.bak` exists under engine/intake/scoping/negotiator/run. Current instructions were read, backups/installed companion parity not certified. Discovery evidence is the installed-skill glob; source roles are the corresponding installed `SKILL.md:10-19` (runtime `:8-53`).

### 2.4 Binary Artifacts

These are exact names, not pixel-content certifications. The repository has videos `BidPilot/sample-portfolio/{bakery,cafe,motion}.mp4`; portfolio maps them to illustrative bakery/cafe/motion examples and seed projects. (`BidPilot/portfolio.py:28-46`)

```text
docs/images/client-page.png
docs/images/deal-timeline.png
docs/images/founder-dashboard.png
docs/images/quote-form.png
```

README illustration roles: `README.md:73,77`.

Exact screenshot naming expansion and filename-designated purposes:

```text
alexstudio-q2-evidence/
  a-cafe-launch-{desktop,phone}.png       Cafe case-study captures
  about-{desktop,phone}.png              About page captures
  case-studies-{desktop,phone}.png       Case-studies captures
  case-studies-list-with-link.png        Case-study listing/link capture
  home-{desktop,phone}.png               Home captures
  portfolio-{desktop,phone}.png          Portfolio captures
  request-a-quote-{desktop,phone}.png     Quote-page captures
  services-{desktop,phone}.png           Services captures
  test-lead-receipt.png                  Receipt capture
c2-screenshots/
  after-client-accepted-desktop.png
  after-client-{desktop,phone}.png
  after-dashboard-{desktop,phone}.png
  after-quote-{desktop,phone}.png
  before-client-accepted-desktop.png
  before-client-{desktop,phone}.png
  before-dashboard-{desktop,phone}.png
  before-quote-{desktop,phone}.png        Before/after UI captures
c3-screenshots/
  timeline-card.png
  timeline-{desktop,phone}.png           Timeline captures
c5-screenshots/
  dashboard-{desktop,phone}.png
  needs-info-desktop.png
  what-changed-{desktop,phone}.png       Needs-information/comparison captures
embed-evidence/
  embed-desktop.png
  embed-mobile.png
  embed-thread-started.png               Embed/thread captures
```

Stored report references temporary screenshot paths, not the repository copies, and records no viewport dimensions/timestamp/runner version. It is not current browser or CRM verification. (`alexstudio-q2-evidence/check-report.json:1-383`, example `:7,18,134,146`)

## 3. Architecture And Authority

```text
Hosted Hashmeta form / local /quote           Private /inquire -> /i/<capability>
            |                                          |
            v                                          v
      durable inbox                              turns + facts + reference assets
            |                                          |
     external scheduled QwenWork <--- list_pending_work + reference notifications
            |                         ^                |
       intake + scoping               |          bounded media worker
            |                         |          frames / OCR / speech
      claim_or_resume / price_inquiry_scope
            |
      stored offer/evidence -> capacity ledger -> adapted seed calendar
            |
      estimating.evaluate
      +-- legacy engine (optional historical fee calibration)
      +-- detailed lines + catalog rates + resource scheduler + alternatives
            |
      evaluation + immutable public options + draft version
            |
      negotiator / compose_proposal_page -> check_draft -> checked digest
            |
      HUMAN founder approve (auth + current version + integrity + capacity)
            |
      /p/<capability> approved website -> buyer selects option
            |
      HUMAN confirm_booking -> weekly + resource reservations
            |
      additive amendment review -> buyer accept -> HUMAN confirm
            |
      producer closeout actuals -> eligible history for future estimates

Operations recovery scan -> leased event -> configured outbound adapter
                                           shipped adapter ONLY verifies completion
Project MCP bridge is separate and has project reads + checks + handoff writes.
```

Source mapping: form/inbox `BidPilot/server.py:346-411`; conversation `BidPilot/conversation.py:56-110,295-414`; discovery `BidPilot/mcp_server.py:492-547`; inquiry pricing `BidPilot/inquiry_quote.py:31-84`; evaluation `BidPilot/core.py:478-529`; estimator branch `BidPilot/estimating.py:133-144`; release/confirmation `BidPilot/core.py:840-946,1134-1285`; commerce `BidPilot/commerce.py:398-468,548-706`; dispatch/verifier `BidPilot/operations.py:392-480,715-759,830-914`; project tools `tools/qwenwork-mcp-bridge.js:102-223`.

Core dynamically imports the primary engine directory, not the build-guide copy. Application evaluation can differ from direct pure-engine evaluation because historical pricing defaults on and `detailed_spec` chooses another estimator. (`BidPilot/core.py:21-27,69-83,478-529`; `BidPilot/estimating.py:141-144,167-199`)

Authority is procedural: local MCP is trusted preparation service; owner HTTP releases/bookings; buyer capabilities access their version/inquiry. There is no general remotely authenticated multi-tenant MCP boundary in this custom stdio transport. (`BidPilot/mcp_server.py:9-16,719-723`; `BidPilot/server.py:155-179,213-230`; `docs/OPERATIONS.md:152-158`)

## 4. Complete Persistence And Data Contracts

Notation: `T=TEXT`, `I=INTEGER`, `R=REAL`, `B=BLOB`; `PK`, `U`, `NN` mean primary key, unique, not-null. Unmarked columns have no explicit NN constraint. JSON columns are serialized TEXT, not SQL-enforced JSON. Application bootstrap concatenates base/inquiry/module schemas and checks nine column additions; no versioned migration-history table is declared. (`BidPilot/db.py:66-99,110-129`)

### 4.1 Base Tables (Including Migrated Columns)

| Table | Every column/type and declared constraints | Source |
|---|---|---|
| `seed_business_rules` | key T PK; value R; meaning T | `BidPilot/db.py:8` |
| `seed_effort_rules` | rule T PK; hours T; applies T | `BidPilot/db.py:9` |
| `seed_rate_card` | item T PK; price_sgd R; unit T | `BidPilot/db.py:10` |
| `seed_past_projects` | project_id T PK; client T; format T; production_mode T; shoot_days I; locations I; scripting T; deliverables I; editing_level T; graphics T; revision_rounds I; caption_versions I; est_hours R; actual_hours R; price_sgd R; start T; end T | `BidPilot/db.py:11-14` |
| `seed_calendar_weeks` | week_ending T PK; working_hours R; calendar_busy_hours R; unscheduled_owed_hours R; note T | `BidPilot/db.py:15-16` |
| `deals` | id T PK; source T; source_id T; client T; raw_inquiry T; status T; current_version_id T; sample_data I default 1; created_at R; U(source,source_id) | `BidPilot/db.py:18-20` |
| `offers` | id T PK; deal_id T; source T; source_id T; from_version_id T; round I; as_of T; offer_json T; evidence_json T; comparable_ids_json T; created_at R; U(source,source_id) | `BidPilot/db.py:21-23` |
| `evaluations` | id T PK; offer_id T U; verdict T; reason_codes_json T; result_json T; engine_version T; created_at R; seed_hash T; capacity_revision I; capacity_snapshot_json T | `BidPilot/db.py:24-25,73-78` |
| `options` | id T PK; evaluation_id T; option_key T; kind T; public_terms_json T; created_at R; U(evaluation_id,option_key) | `BidPilot/db.py:26-27` |
| `proposal_versions` | id T PK; deal_id T; offer_id T; previous_version_id T; version_no I; status T; draft_body T; outbound_status T; checked_digest T; public_option_ids_json T; token_hash T U; selected_option_id T; published_at R; accepted_at R; created_at R; page_json T; import_option_keys_json T; U(deal_id,version_no) | `BidPilot/db.py:28-31,67-69` |
| `run_steps` | id T PK; deal_id T; offer_id T; step_key T; status T; private_note T; started_at R; finished_at R; trigger T | `BidPilot/db.py:32-33,79-81` |
| `messages` | id T PK; deal_id T; version_id T; direction T; body T; option_ids_json T; check_json T; request_id T U; created_at R; payload_hash T | `BidPilot/db.py:34-36,82-83` |
| `approvals` | id T PK; version_id T; actor_id T; action T; reviewed_digest T; note T; decided_at R | `BidPilot/db.py:37-38` |
| `confirmations` | id T PK; deal_id T; version_id T; option_id T; actor_id T; decided_at R | `BidPilot/db.py:39-40` |
| `inbox` | id T PK; source T default site; source_id T U; fields_json T; raw_message T; received_at R; sample_data I default 1 | `BidPilot/db.py:41-43` |
| `agent_outputs` | id T PK; deal_id T; offer_id T; kind T; payload_json T; request_id T U; created_at R | `BidPilot/db.py:44-45` |
| `learnings` | id T PK; deal_id T; source T; body T; created_at R | `BidPilot/db.py:46-47` |
| `mcp_operations` | request_id T PK; tool T; payload_hash T; result_json T; created_at R; deal_id T | `BidPilot/db.py:48-49,70-71` |
| `reservations` | id T PK; deal_id T; version_id T U; option_id T; confirmation_id T U; capacity_revision I; status T default active; total_planning_hundredths I; created_at R | `BidPilot/db.py:50-53` |
| `allocations` | id T PK; reservation_id T; week_ending T; planning_hundredths I; milestone T; created_at R; U(reservation_id,week_ending) | `BidPilot/db.py:54-56` |
| `inquiry_proposal_links` | version_id T PK; released_hash T NN; alias_hash T NN U | `BidPilot/db.py:57-58` |
| `capacity_impacts` | reservation_id T; version_id T; details T NN; PK(reservation_id,version_id) | `BidPilot/db.py:59-60` |
| `capacity_revisions` | revision I PK; seed_hash T; snapshot_json T; created_at R | `BidPilot/db.py:61-62` |

### 4.2 Inquiry And Reference Tables

| Table | Every column/type and constraints | Source |
|---|---|---|
| `priced_briefs` | offer_id T PK; inquiry_id T NN; revision I NN; input_hash T NN; U(inquiry_id,revision) | `BidPilot/inquiry_quote.py:5-8` |
| `offer_briefs` | offer_id T PK; inquiry_id T NN; revision I NN | `BidPilot/inquiry_quote.py:9-10` |
| `inquiries` | id T PK; token_hash T NN U; revision I NN default 0; processed_seq I NN default 0; deal_id T; created_at R NN; revoked I NN default 0 | `BidPilot/conversation.py:23` |
| `inquiry_turns` | inquiry_id T NN; seq I NN; sender T NN; body T NN; refs_json T NN; request_id T NN; payload_hash T NN; created_at R NN; PK(inquiry_id,seq); U(inquiry_id,request_id) | `BidPilot/conversation.py:24` |
| `inquiry_facts` | id T PK; inquiry_id T NN; revision I NN; subject T NN; value_json T NN; status T NN; source_seq I NN; supersedes T; created_at R NN | `BidPilot/conversation.py:25` |
| `inquiry_jobs` | id T PK; inquiry_id T NN; seq I NN; status T NN; lease_until R; lease_owner T; U(inquiry_id,seq) | `BidPilot/conversation.py:26`; `BidPilot/db.py:84-85` |
| `reference_assets` | id T PK; inquiry_id T NN; filename T NN; mime T NN; sha256 T NN; path T NN; created_at R NN | `BidPilot/conversation.py:27` |
| `reference_runs` | id T PK; asset_id T NN; status T NN; result_json T; error T; created_at R NN; lease_until R | `BidPilot/conversation.py:28` |
| `reference_observations` | id T PK; run_id T NN; start R NN; end R NN; kind T NN; body T NN; method T NN; uncertainty T NN; frame T; supersedes T; created_at R NN | `BidPilot/conversation.py:29` |
| `media_notifications` | run_id T PK; inquiry_id T NN; status T NN default queued | `BidPilot/conversation.py:30` |
| `inquiry_commits` | inquiry_id T NN; request_id T NN; payload_hash T NN; result_json T NN; PK(inquiry_id,request_id) | `BidPilot/conversation.py:31` |
| `inquiry_receipts` | source_id T PK; inquiry_id T NN U | `BidPilot/conversation.py:32` |

### 4.3 Estimating, Commerce And Portfolio Tables

| Table | Every column/type and constraints | Source |
|---|---|---|
| `detailed_estimates` | id T PK; deal_id T NN; scope_revision_id T NN; rate_version T NN; input_json T NN; result_json T NN; created_at R NN | `BidPilot/estimating.py:18-21` |
| `founder_past_projects` | project_id T PK; project_json T NN; created_at R NN | `BidPilot/estimating.py:22-23` |
| `estimate_rate_versions` | version T PK; rates_json T NN; created_at R NN | `BidPilot/estimating.py:24-25` |
| `resource_availability` | id T PK; resource T NN; start_at T NN; end_at T NN | `BidPilot/estimating.py:26-27` |
| `resource_allocations` | id T PK; reservation_id T NN; resource T NN; start_at T NN; end_at T NN; task_id T NN; U(reservation_id,resource,start_at,end_at) | `BidPilot/estimating.py:28-31` |
| `producer_overrides` | id T PK; deal_id T NN; scope_revision_id T NN; item_id T NN; field T NN; value_json T NN; basis_date T NN; note T NN; request_id T NN U; supersedes T; created_at R NN | `BidPilot/estimating.py:32-36` |
| `change_orders` | id T PK; deal_id T NN; base_version_id T NN; base_order_id T; terms_json T NN; digest T NN; status T NN; created_at R NN; approved_at R; accepted_at R; confirmed_at R; reservation_id T | `BidPilot/commerce.py:18-22` |
| `priced_request_orders` | request_id T PK; order_id T NN; arguments_json T NN | `BidPilot/commerce.py:23-24` |
| `commerce_reference_requests` | request_id T PK; option_id T NN; reference_json T NN | `BidPilot/commerce.py:25-26` |
| `closeouts` | id T PK; deal_id T NN U; version_id T NN; scope_json T NN; actuals_json T NN; created_at R NN | `BidPilot/commerce.py:27-29` |
| `task_actuals` | closeout_id T NN; task_id T NN; hours R NN; labor_cost_cents I NN; note T NN; PK(closeout_id,task_id) | `BidPilot/commerce.py:30-33` |
| `portfolio_assets` | id T PK; project_id T NN; title T NN; description T NN; tags T NN; mime T NN; content B NN; shared I NN; digest T NN; U(project_id,digest) | `BidPilot/portfolio.py:18-21` |
| `portfolio_search_requests` | request_id T PK; version_id T NN; query T NN; results_json T; created_at R NN | `BidPilot/portfolio.py:23-25` |

Portfolio index `portfolio_share(shared,project_id)`. (`BidPilot/portfolio.py:22`)

### 4.4 Separate Operations Databases

| Table | Every column/type and constraints | Source |
|---|---|---|
| `studios` | id T PK; slug T NN U; workspace T NN U; status T NN CHECK active/revoked; retention_json T NN; created_at R NN; revoked_at R | `BidPilot/operations.py:42-51` |
| `work_events` | event_id T PK; kind T NN; payload_json T NN; payload_hash T NN; retry_key T NN U; status T NN CHECK queued/leased/completed/dead; available_at R NN; lease_owner T; lease_token T; lease_until R; attempts I NN default 0; max_attempts I NN; created_at R NN; completed_at R; last_error T; lane T | `BidPilot/operations.py:54-71,385-390` |
| `metric_events` | id T PK; name T NN; value R NN; labels_json T NN; recorded_at R NN | `BidPilot/operations.py:74-80` |

Indexes: `work_events_due(status,available_at,lease_until,created_at)` and `metric_events_time(recorded_at,name)`. Backup operations copy has these initial three table declarations but lacks current lane/verifier behavior. (`BidPilot/operations.py:72-73,81`; `operations-retry-backup.8B0gAuod/operations.py:41-81,700-769`)

**Logical relationships, not declared foreign keys:** deal -> offers -> unique evaluation -> options; deal -> proposal version -> ordered option IDs; version -> approvals/acceptance/confirmation -> reservation -> allocations; inquiry -> turns/facts/jobs/assets -> runs -> observations/notifications; inquiry revision -> priced offer; reservation -> resources; confirmed version -> amendment chain and closeout -> task actuals; portfolio -> history project; search request -> version. No application schema declaration contains REFERENCES/FOREIGN KEY, so deletions/ownership depend on service logic. (`BidPilot/db.py:7-63`; `BidPilot/conversation.py:22-33`; `BidPilot/commerce.py:17-34`; `BidPilot/estimating.py:17-37`; `BidPilot/portfolio.py:17-26`; `BidPilot/core.py:139-149,478-529,1065-1075,1233-1277`)

Amendment reservations use synthetic `change:<order_id>` version/option/confirmation keys, not real referenced rows; a future FK migration needs redesign. (`BidPilot/commerce.py:594-596`)

### 4.5 CSV Schemas And Seed Values

Both repository engine copies have identical five CSV schemas. Source references below use primary directory and correspond to reference-copy files. (**Audit execution evidence** parity; `BidPilot/test_engine_sync.py:7-22`)

| CSV | Exact header/schema | Source |
|---|---|---|
| business_rules.csv | key,value,meaning; numeric value float-parsed | `BidPilot/bidpilot-engine/data/business_rules.csv:1-10`; `engine.py:117-118` in same directory |
| effort_rules.csv | rule,hours,applies; hours numeric string or literal manual | `BidPilot/bidpilot-engine/data/effort_rules.csv:1-11`; `engine.py:117-118` |
| rate_card.csv | item,price_sgd,unit | `BidPilot/bidpilot-engine/data/rate_card.csv:1-9` |
| calendar_weeks.csv | week_ending,working_hours,calendar_busy_hours,unscheduled_owed_hours,note | `BidPilot/bidpilot-engine/data/calendar_weeks.csv:1-8` |
| past_projects.csv | project_id,client,format,production_mode,shoot_days,locations,scripting,deliverables,editing_level,graphics,revision_rounds,caption_versions,est_hours,actual_hours,price_sgd,start,end | `BidPilot/bidpilot-engine/data/past_projects.csv:1-12` |

Business keys: working_hours_per_week 30; capacity_buffer_pct 10; min_hourly_rate_sgd 70; included_revision_rounds 2; discount_limit_pct 10; comparable buffers 15/20/25 for >=3/1-2/0; offer_expiry_days 5 (reserved, not applied by evaluator). (`BidPilot/bidpilot-engine/data/business_rules.csv:2-10`; `BidPilot/bidpilot-engine/engine.py:181-187,319-325`)

Effort: first shoot/shared planning 8 h, edit-only setup 2, extra shoot 8, caption .5/version, extra revision 2, extra location 3, motion 3/video, full script 4/project, custom animation manual, fallback promo 7/video. Rates: shoot 750, edit-only setup 200, promo 600, caption 50, extra revision 200, extra location 300, motion 250, full script 400 SGD. No extra-shoot-day rate is shipped. (`BidPilot/bidpilot-engine/data/effort_rules.csv:2-11`; `BidPilot/bidpilot-engine/data/rate_card.csv:2-9`; `BidPilot/bidpilot-engine/engine.py:310-313`)

Calendar buffered week budgets: 2026-11-27 18; 12-04 10.8; 12-11 21.6; 12-18 21.6; 12-25 18; 2027-01-01 21.6; 01-08 27 hours. These are sample buckets, not shoot appointments. (`BidPilot/bidpilot-engine/data/calendar_weeks.csv:2-8`; `BidPilot/bidpilot-engine/engine.py:181-187`)

### 4.6 JSON Contracts And Important Serialized Payloads

- Legacy input: `{as_of,offer,comparable_ids}`. Offer requires client, format, production_mode, shoot_days, locations, interviewees, scripting, deliverables, editing_level, graphics, revision_rounds, caption_versions, deadline, budget_sgd, direct_costs_sgd. Format enums: product_promo/social_reel/testimonial/event_recap/explainer; production edit_only/one_shoot_day/multi_day; script none/light/full; edit simple/standard/heavy; graphics none/basic_titles/light_motion/custom_animation. Counts exact ints excluding bool, max 100, deliverables >=1; finite 0..10,000,000 money with <=2 decimals; strict ISO dates/cross-field production constraints; comparable string array. Extra fields allowed by legacy input. (`BidPilot/bidpilot-engine/engine.py:20-99`; `BidPilot/bidpilot-engine/offer-input.schema.json:125-140`)
- Legacy evaluation: verdict, ordered reason codes, missing/invalid paths, comparables used/excluded, nullable estimate, public options, private option_metrics. Public option fields: option_id/kind, deliverables, caption_versions, revision_rounds, shoot_days, locations, deadline, price_sgd, phases of incremental deliverables/deadline. (`bidpilot-build-guide/BUILD_GUIDE.md:23-35`; `BidPilot/bidpilot-engine/evaluation-output.schema.json:105-227,303-313,416-426`)
- Outbound request: draft, approved_options, private_values, other_client_names; response passed/violations with codes PRIVATE_LABEL/PRIVATE_NUMBER/OTHER_CLIENT_NAME/UNAPPROVED_AMOUNT/UNAPPROVED_DATE/UNAPPROVED_COUNT/UNRECOGNIZED_NUMBER. (`BidPilot/bidpilot-engine/SKILL.md:10,15`; `bidpilot-build-guide/BUILD_GUIDE.md:37-43`)
- Detailed spec: rate_version, buffer_pct, floor_rate_cents, items with unique id/role/basis/quantity/unit_hours/cost_rate_cents/sell_rate_cents; optional low/high/assumption/setup_id/supplied/deliverables/evidence/depends_on; expenses cost/sell cents; optional setups and tasks with id/minutes/resources/release_at/deadline/depends_on/kind/contiguous/setup_id/item_ids. Complete detailed_alternatives permit offer overrides only deliverables/captions/deadline/budget/direct costs and full spec, optional two phases/first task IDs. (`BidPilot/estimating.py:203-387,539-610`; `docs/DETAILED_ESTIMATE.md:3-40`)
- Chat fact changes: subject,value,status proposed/confirmed/excluded, source_sequence, optional supersedes; production cues: observation_id,body,uncertainty; source_sequence must equal committed latest buyer sequence. (`BidPilot/conversation.py:139-176,502-512`)
- Proposal page: headline/introduction strings, sections `{title,body}`, portfolio `{title,description,asset_id}`, bounded arrays; new version on composition. (`BidPilot/commerce.py:58-105`)
- Amendment terms: description,price_sgd,as_of,phases `{deadline,hours}`, optional calculation; closeout actuals: actual_hours,actual_cost_sgd,end,notes,reliable; optional task_actuals `{task_id,hours,labor_cost_cents,note}` and actual_labor_cost_sgd with matching totals. (`BidPilot/commerce.py:249-272,619-674`)
- Agent output kind shapes: intake offer/evidence/missing_fields/clarification; scoping scope_profile[{field,value,evidence}]/comparable_ids/differences[{project_id,fields,why,evidence}]; negotiator draft/option_ids; clarification questions. (`BidPilot/mcp_server.py:443-466`)
- Snapshot manifest: format bidpilot-snapshot-v1,studio_id,created_at,credentials_included false,files[{path,size,sha256}]; operations envelope contract bidpilot.authorized-worker.v1 and routing/digest fields; external backtest input array project_id/campaign_id?/quoted_at/actual_hours/group?/predictions and optional actual/predicted cost cents. (`BidPilot/operations.py:835-843,941-948`; `BidPilot/scripts/backtest_estimates.py:2-7,61-99`)

Strict legacy evaluation schema does NOT cover historical_pricing, detailed estimate shape/resource reasons/as_offered metrics/alternative_N. Do not silently validate extended application output against the legacy schema. (`BidPilot/bidpilot-engine/evaluation-output.schema.json:105-227,303-313,416-426`; `BidPilot/estimating.py:169,195-199,567-569,648-672`)

## 5. Engine Arithmetic And Worked Examples

### 5.1 Legacy Evaluation

Validation returns missing fields before checking supplied values. Manual estimation paths include non-product-promos, interviewee workload, custom animation and multi-day work without a supplied extra-day rate. (`BidPilot/bidpilot-engine/engine.py:49-99,310-313`)

Usable comparables match format/mode/edit/shoot days/baseline location min(request.locations,1); exclude full-script history, nonbaseline graphics, extra revisions, captions, nonpositive deliverables or actuals <= modeled fixed hours. Units are `(actual_hours - modeled_fixed_hours)/deliverables`, median full precision, otherwise 7 h/video. (`BidPilot/bidpilot-engine/engine.py:143-178,317-325`)

```text
fixed = 2 for edit-only, else 8 + 8*(shoot_days-1)
addons_hours = .5*captions + 2*max(revisions-2,0) + 3*max(locations-1,0)
             + (3*videos if light_motion else 0) + (4 if full_script else 0)
base = fixed + videos*median_unit + addons_hours
planning = round(base*(1+buffer_pct/100),2)
list = round(setup + 600*videos + applicable add-on prices,2)
floor = round(planning*70 + direct_costs_sgd,2)
effective_rate = round(budget_sgd/planning,2)
capacity = round(sum(max(0,working-busy-owed) for eligible buckets)*.9,2)
eligible bucket: as_of+4 days <= week_ending <= deadline
```

Source formulas: `BidPilot/bidpilot-engine/engine.py:121-140,181-212`. Binary float/Python round is deliberate: 30.5*1.15 rounds to fixture 35.07, not a decimal convention substituted by an agent. Direct costs increase floor, not list; effective rate is gross budget/hour, not true margin. (`bidpilot-build-guide/BUILD_GUIDE.md:14`; `BidPilot/bidpilot-engine/engine.py:201-211`)

Reasons: capacity shortage; BELOW_FLOOR when budget OR list < floor; OVER_DISCOUNT_LIMIT when budget meets floor but < rounded 90% list. If reasons, search alternatives -> counter if any, otherwise decline/NO_FEASIBLE_OPTIONS; else accept if budget>=list, accept_with_approval below list. Accepted price=min(budget,list). Comparable diagnostics do not force rejection. (`BidPilot/bidpilot-engine/engine.py:330-354`)

### 5.2 Juniper Original

Fixture: as_of 2026-11-23; one shoot/location; light script; three standard/basic-title promos, two included revisions, three captions; deadline 2026-12-11; budget 2700; direct costs 0; comparables P-031/P-036/P-041/P-044. (`BidPilot/bidpilot-engine/test_cases.json:3-6`)

| Calculation | Result | Source |
|---|---:|---|
| P-031 (21-8)/2 | 6.5 h/video | `BidPilot/bidpilot-engine/data/past_projects.csv:3`; `engine.py:175-177` |
| P-036 (29-8)/3 | 7 | same data `:5` |
| P-041 (29-8)/3 | 7 | same data `:8` |
| P-044 (38-8)/4 | 7.5 | same data `:10` |
| Median, four IDs | 7, buffer 15% | `BidPilot/bidpilot-engine/engine.py:317-321` |
| Base 8 + 3*7 + 3*.5 | 30.50 h | `BidPilot/bidpilot-engine/test_engine.py:75-87` |
| Planning round(30.5*1.15,2) | 35.07 h | same test |
| Capacity (20+12+24)*.9 | 50.40 h | same test; calendar `:2-4` |
| List 750+1800+150 | SGD 2700 | same test |
| Floor 35.07*70 | SGD 2454.90 | same test |
| Gross effective rate 2700/35.07 | SGD 76.99/h | same test |

Verdict accept, one as_offered option; human release remains required. (`BidPilot/bidpilot-engine/test_cases.json:6`; `BidPilot/core.py:840-946`)

### 5.3 Juniper Counter And Reservations

Five videos/five captions, earlier December 4 deadline, same budget -> base45.5/planning52.32/capacity28.8/list4000/floor3662.40/effective51.61; counter CAPACITY_SHORTFALL+BELOW_FLOOR. (`BidPilot/bidpilot-engine/test_engine.py:89-95`)

| Option | Videos/captions | Deadline | SGD | Planning/capacity/floor |
|---|---|---|---:|---|
| hold_date | 2/2 | 2026-12-04 | 2050 | 26.45 / 28.8 / 1851.50 |
| hold_budget | 3/3 | 2026-12-11 | 2700 | 35.07 / 50.4 / 2454.90 |
| hold_scope | 5/5 | 2026-12-18 | 4000 | 52.32 / 72 / 3662.40 |
| phased | 5/5 total; 2 then 3 | 12-04 then 12-18 | 4000 total | 52.32 / 72 / 3662.40; early26.45/28.8 |

Sources: `BidPilot/bidpilot-engine/engine.py:246-296`; `BidPilot/bidpilot-engine/test_engine.py:96-106`; **Audit execution evidence** direct engine recomputation. Original booking allocates 18/10.8/6.27 hours across Nov27/Dec4/Dec11; phased allocates 18/10.8/21.6/1.92, totaling52.32, one shared shoot. (`BidPilot/test_stage11.py:124-140,214-240`)

No-comparable variant keeps7h units but25% buffer ->38.12h/floor2668.40. Budget2550 with normal comparables gives accept_with_approval. (`BidPilot/bidpilot-engine/test_engine.py:108-112`; `BidPilot/bidpilot-engine/test_cases.json:33-36`)

### 5.4 Alternative And Detailed Paths

Legacy search descends counts and scans supplied Fridays within8 weeks; hold-date keeps original date, hold-budget maximizes count then earliest feasible Friday, hold-scope full count earliest date, phased cumulative early/full effort; cap captions to reduced count, retain other terms, dedupe equivalent public commitments ignoring kind/ID. It may propose earlier dates. (`BidPilot/bidpilot-engine/engine.py:228-296`; `test_cases.json:39-42` in same directory)

Detailed calculation uses HALF_UP cents per base line and again after buffering; planning hours still Python round. (`BidPilot/estimating.py:40-41,344-387`)

```text
line_hours = quantity*unit_hours
line_cost/sell_cents = HALF_UP(line_hours*cost/sell_rate_cents)
planning = round(sum(line_hours)*buffer_factor,2)
labor_cost/sell = sum(HALF_UP(rounded_base_line_cents*buffer_factor))
production_cost = labor_cost + expense_cost
floor = HALF_UP(planning*floor_rate_cents) + expense_cost
selling_price = labor_sell + expense_sell
```

Worked detailed fixture 27.5 base/33 planned at45cost,90sell,70floor SGD/hour plus120expense ->1605production cost/2430floor/3090selling. The first four quantities are asserted; selling follows the formula. This is NOT the legacy Juniper scope. (`BidPilot/test_estimating.py:17-29`; `BidPilot/estimating.py:374-387`)

Catalog rates match stored role rates; task dependencies/resources require simultaneous availability; shoot/contiguous one block, edit may split. Greedy lexicographic task order has no backtracking. (`BidPilot/estimating.py:390-436,447-523`)

Historical calibration combines seed/founder/reliable closeouts, requires2 exact family matches completed before quote, subtracts modeled setup/addons from fees, median per-deliverable rate, rechecks through engine and records leave-one-out prices. It is selling-price calibration, not profit inference. (`BidPilot/estimating.py:50-64,98-130,147-199`)

## 6. Complete HTTP, MCP And Skill Interfaces

Notation: `O` owner token; `S` same-origin POST; `P` proposal capability; `I` inquiry capability. Owner uses X-Owner-Token or `owner` query; configured public Host cannot authenticate owner. S compares Origin/Referer netloc to Host and http local/https configured public. Every POST passes S before dispatch. JSON/HTML no-store and noindex; HTML referrer/CSP frame-ancestors. (`BidPilot/server.py:109-134,155-179,213-230`)

### 6.1 GET Routes

| Method/path | Auth/params | Output / side effects | Source |
|---|---|---|---|
| GET `/`, `/dashboard` | O | Founder HTML | `BidPilot/server.py:193-196` |
| GET `/api/state` | O | badge/capacity/deals/offers/versions/private metrics/steps/outputs/messages/approvals/inbox; BEGIN read transaction | `BidPilot/server.py:197-201,838-941` |
| GET `/quote` | public | Form; fresh random request ID/resume proof | `BidPilot/server.py:191-192,512-544` |
| GET `/p/<token>` | P | Integrity-checked approved client HTML or404 | `BidPilot/server.py:202-204,491-502` |
| GET `/preview/<version_id>` | O | Client HTML preview or404 | `BidPilot/server.py:205-208,504-509` |
| GET `/inquire` | public | Start form | `BidPilot/conversation.py:445-449` |
| GET `/i/<token>` | I | Chat HTML | `BidPilot/conversation.py:450-455` |
| GET `/i/<token>/state` | I | transcript/facts/assets/evidence/pending/optional proposal_url | `BidPilot/conversation.py:456-463` |
| GET `/i/<token>/asset/<asset_id>` | I+asset belongs to inquiry | Original media, Range bytes=start-end,200/206/416 | `BidPilot/conversation.py:416-433,464-467` |
| GET `/estimating` | O checked centrally | Estimator HTML | `BidPilot/server.py:187-188`; `BidPilot/estimating.py:1131-1134` |
| GET `/api/estimating/history` | O centrally | ok/history | `BidPilot/estimating.py:1135-1137` |
| GET `/commerce` | O | Commerce HTML | `BidPilot/commerce.py:758-764` |
| GET `/api/commerce/versions` | O | versions[id,status,version_no,client], allowed status set | `BidPilot/commerce.py:751-757` |
| GET `/portfolio` | O | Library HTML | `BidPilot/portfolio.py:229-233` |
| GET `/portfolio/media/<id>` | public shared asset | BLOB/byte ranges; dispatch accepts prefix and final segment, not strict path shape | `BidPilot/portfolio.py:190-228` |

All GETs open application DB via schema-initializing `db.connect`; thus HTTP reads can initialize/migrate state. Unmatched GET404. Only GET/POST application method handlers are implemented; other methods fall to BaseHTTPRequestHandler unsupported-method behavior, not an application API. (`BidPilot/server.py:104-107,182-211,213`; `BidPilot/db.py:110-121`) **Inference:** ordinary unsupported methods return framework501.

### 6.2 Owner POST Routes

Each below requires O+S; JSON object body. Common malformed JSON400; several core exceptions are normalized selectively, not universally. General owner `body()` has no size cap. (`BidPilot/server.py:143-149,229-245`)

| POST path | Required fields; optional marked ? | Output/status/effects | Source |
|---|---|---|---|
| `/api/capacity-preview` | version_id,option_id | ok+fits/weeks/affected etc; read BEGIN,400 ValueError; no reservation | `BidPilot/server.py:251-257` |
| `/api/import-run` | source,source_id,offer_request,draft,option_ids,steps; request_id?,deal_id?,raw_inquiry?,evaluation?,outbound_result? | IDs/check/mismatch flags; transactional recompute/new draft/output/steps/replay;409 conflict else400 | `BidPilot/server.py:258-266`; `BidPilot/dashboard_ops.py:15-108` |
| `/api/reset-sample` | confirm exactly RESET SAMPLE DATA; request_id? | counts; sample deletion/seed reload/optional ledger revision/replay;409 conflict else400 | `BidPilot/dashboard_ops.py:111-213` |
| `/api/import` | source site/hashmeta/manual,source_id,offer_request; raw_inquiry? | ok,deal_id,offer_id,fresh,duplicate; claim/resume;400 | `BidPilot/server.py:268-283` |
| `/api/evaluate` | deal_id,offer_id | ok,eval_id,version_id; persisted engine result/options/draft;400 | `BidPilot/server.py:285-290` |
| `/api/check` | version_id | ok=passed,result; persisted gate | `BidPilot/server.py:292-294` |
| `/api/draft` | version_id,body | ok,version_id; new version | `BidPilot/server.py:296-298` |
| `/api/approve` | version_id; option_id? | core release result incl capability on success,200/409; publishes/audits | `BidPilot/server.py:300-302` |
| `/api/reject` | version_id; note? | core result200/409; rejection audit | `BidPilot/server.py:304-306` |
| `/api/rotate-token` | version_id | core result/new capability200/409; revoke old link/audit | `BidPilot/server.py:308-310` |
| `/api/reevaluate` | offer_id,deal_id | new offer/eval/version IDs;400 selected errors | `BidPilot/server.py:312-320` |
| `/api/confirm-booking` | version_id | booking result200/409; reservations/confirmation/audit | `BidPilot/server.py:322-325` |
| `/api/close-deal` | deal_id | core result200/409; administrative closure | `BidPilot/server.py:327-329` |
| `/api/client-change` | deal_id; body?,request_id?,terms? | clarify202/null offer or offered200/offer_id;409 conflict else400 | `BidPilot/server.py:331-343` |
| `/api/estimating/history` | project | project_id; insert founder history | `BidPilot/estimating.py:1143` |
| `/api/estimating/rates` | version,rates | rate_version; immutable insert | `BidPilot/estimating.py:1144` |
| `/api/estimating/preview` | spec | detailed result; calculate only | `BidPilot/estimating.py:1145` |
| `/api/estimating/estimate` | deal_id,scope_revision_id,spec | estimate_id,estimate; persist | `BidPilot/estimating.py:1146` |
| `/api/estimating/availability` | resource,start_at,end_at | availability_id; insert | `BidPilot/estimating.py:1147` |
| `/api/estimating/schedule` | tasks | feasible/allocations or blocked_task_id; read calculation | `BidPilot/estimating.py:1148` |
| `/api/estimating/backtest` | cases | overall/groups metrics; calculation | `BidPilot/estimating.py:1149` |
| `/api/estimating/merge-setups` | spec,source_setup_ids,target_setup_id | spec; pure transformation | `BidPilot/estimating.py:1150` |
| `/api/estimating/split-setup` | spec,setup_id,groups | spec; pure transformation | `BidPilot/estimating.py:1151` |
| `/api/estimating/alternatives` | spec,offer,as_of,transformations | opportunities/alternatives/rejected/base hours | `BidPilot/estimating.py:1152` |
| `/api/estimating/overrides` | deal_id,scope_revision_id,item_id,field,value,basis_date,note,request_id; supersedes? | override_id; persist/replay | `BidPilot/estimating.py:687-717,1153` |
| `/api/estimating/apply-overrides` | deal_id,scope_revision_id,spec | spec,applied; transformed copy | `BidPilot/estimating.py:1154` |
| `/api/commerce/page` | version_id,page | version_id; new page version | `BidPilot/commerce.py:770` |
| `/api/commerce/order` | version_id,terms; base_order_id? | order_id,digest; manual draft | `BidPilot/commerce.py:771` |
| `/api/commerce/priced-order` | version_id,kind,quantity,as_of,deadline; base_order_id? | order_id,digest; calculated draft; no request_id forwarded by route | `BidPilot/commerce.py:772` |
| `/api/commerce/approve` | order_id,digest | ok; approved exact order | `BidPilot/commerce.py:773` |
| `/api/commerce/confirm` | order_id | reservation_id,capacity_revision; additive booking | `BidPilot/commerce.py:774` |
| `/api/commerce/closeout` | version_id,actuals | ok; immutable actuals/selected scope | `BidPilot/commerce.py:775` |
| `/api/commerce/orders` | version_id | orders[id,status,terms,digest,base_order_id]; read | `BidPilot/commerce.py:776` |
| `/api/portfolio/upload` | title,description,tags,project_id,content base64,shared bool | ok; project/file/signature/24MiB checks; BLOB/digest insert; generic400 | `BidPilot/portfolio.py:239-282` |
| `/api/portfolio/share` | id,shared bool | ok; sharing toggle; generic400 | `BidPilot/portfolio.py:239-256,279-282` |

Estimating routes commit result and merge ok; caught ValueError/KeyError/TypeError400 rollback. Commerce same, portfolio generic validation response. (`BidPilot/estimating.py:1156-1165`; `BidPilot/commerce.py:777-785`; `BidPilot/portfolio.py:279-282`)

### 6.3 Public And Capability POST Routes

| POST path | Auth/body | Output/status/effects | Source |
|---|---|---|---|
| `/quote` | S; JSON or URLencoded <=16KiB; request_id; shoot_days,locations,interviewees,deliverables,revision_rounds,caption_versions nonnegative ints; budget_sgd finite >=0; text fields optional; caption_every_deliverable?; resume_proof?43chars | ok/source_id/inquiry_url? or HTML receipt; inbox+receipt inquiry+job+operation;400 invalid/409 conflict/429 global20-per600s limiter | `BidPilot/server.py:47-92,346-411` |
| `/inquire` | S; text; request_id?; attachment_first true permits blank | ok,inquiry_id,token; inquiry+optional buyer/job;400 | `BidPilot/conversation.py:56-69,469-485` |
| `/i/<token>/messages` | I+S; text,request_id,references? owned asset/ranges | ok,sequence; append buyer/job or replay;400/409 | `BidPilot/conversation.py:75-99,469-485` |
| `/i/<token>/upload` | I+S; filename,mime video/mp4/quicktime/x-m4v,data base64 <=30MiB decoded | ok,asset_id,analysis_id; private file0600/asset/run;400 | `BidPilot/conversation.py:236-259,469-485` |
| `/p/<token>/accept` | P+S; option_id | core ok/error,core http status; acceptance not booking | `BidPilot/server.py:457-461` |
| `/p/<token>/request-change` | P+S published only; body,request_id?,terms? count/captions/deadline/budget | offered200 or clarify202; conflict409/invalid400/stale404; message/new offer | `BidPilot/server.py:463-485` |
| `/p/<token>/portfolio-search` | P+S; request_id,query or request_id,poll true | ok,status pending/complete,assets; queues query/read poll; generic409 | `BidPilot/server.py:431-440`; `BidPilot/portfolio.py:81-107` |
| `/p/<token>/creative-change` | P+S; exact request_id,option_id,target_deliverable,asset_id,action style/swap/add | ok202 or409; message/reference row; selected confirmed option accepted by backend too | `BidPilot/server.py:430-445`; `BidPilot/commerce.py:471-512` |
| `/p/<token>/additional-work` | P+S confirmed; body,request_id,reference? action/reference/target_deliverable | ok200 or generic409; additional inbound/reference | `BidPilot/server.py:446-456`; `BidPilot/commerce.py:515-545` |
| `/p/<token>/change-order/<id>/accept` | P+S; parsed object, fields unused | ok,pending;200/409; approved->accepted/replay | `BidPilot/server.py:446-456`; `BidPilot/commerce.py:548-564` |

All /p POSTs capped16KiB; inquiry JSON cap approximately40MiB+10k regardless of message action. Unknown paths404 after their auth dispatch; no CORS OPTIONS implementation. (`BidPilot/server.py:413-428,486`; `BidPilot/conversation.py:473-475`)

### 6.4 All 36 Product MCP Tools

Argument notation: `?` optional; strings unless noted. Normal return is JSON serialized in MCP text content; get_reference_frame/get_portfolio_frame return MCP image content. ToolError -> isError/text; unexpected exception -> logged stderr traceback/internal_error; unknown tool JSON-RPC -32602; unknown method -32601. No owner release/confirm tool. (`BidPilot/mcp_server.py:697-757`)

| # Tool | Complete top-level signature | Return / side effects | Source |
|---|---|---|---|
| 1 claim_intake | source,source_id,client,raw_inquiry,offer_request object,evidence?object,request_id? | deal_id,offer_id,created,note; status/missing_fields for incomplete; claim/evidence/replay writes | `BidPilot/mcp_server.py:98-185,763-767` |
| 2 get_deal | deal_id | deal,current_offer,versions; private evidence read | `BidPilot/mcp_server.py:188-216,768-769` |
| 3 evaluate_offer | deal_id,offer_id | evaluation_id,verdict,reason_codes,estimate,options,draft_version,note; evaluation persist/replay | `BidPilot/mcp_server.py:219-239,770-771` |
| 4 save_draft | version_id,draft_body,request_id | draft_version,note; new version/replay | `BidPilot/mcp_server.py:242-315,772-774` |
| 5 check_draft | version_id | outcome,version; persisted gate | `BidPilot/mcp_server.py:354-365,775-776` |
| 6 run_trace | deal_id | deal,steps,messages,versions,agent_outputs; private read | `BidPilot/mcp_server.py:368-392,777-778` |
| 7 negotiation_payload | evaluation_id | evaluation/offer/deal IDs,verdict,options,client_message/null,note?; read | `BidPilot/mcp_server.py:318-351,779-780` |
| 8 record_step | deal_id,step_key,status,request_id,offer_id?,private_note?,trigger? | step_id; replay-safe step | `BidPilot/mcp_server.py:395-440,782-800` |
| 9 save_agent_output | deal_id,kind,payload object,request_id,offer_id? | output_id; shape-check/persist/replay | `BidPilot/mcp_server.py:443-480,783-800` |
| 10 get_scoping_history | no args | private,note,projects | `BidPilot/mcp_server.py:483-489,794-800` |
| 11 list_pending_work | no args | inquiries,change_requests,unevaluated_offers,needs_information; optional portfolio_searches,conversations,additional_work | `BidPilot/mcp_server.py:492-549` |
| 12 submit_change_offer | deal_id,request_id,version_id,offer_request object,evidence?object(raw_message conditionally required) | offer_id; stale step once/error, message/offer/replay | `BidPilot/mcp_server.py:552-588,784,799` |
| 13 price_inquiry_scope | inquiry_id,expected_revision integer,offer_request object | deal_id,offer_id,evaluation_id,version_id,scope_revision; price/bind/replay | `BidPilot/inquiry_quote.py:31-92` |
| 14 acknowledge_reference_update | inquiry_id,analysis_id | acknowledged true; consume notification | `BidPilot/conversation.py:193-197,503` |
| 15 bind_inquiry_deal | inquiry_id,deal_id | IDs; matching site identity binding | `BidPilot/conversation.py:488-500,504` |
| 16 get_conversation_context | inquiry_id,through_sequence?integer | inquiry_id,revision,processed_sequence,turns,facts,references | `BidPilot/conversation.py:101-110,505` |
| 17 commit_chat_turn | inquiry_id,through_sequence integer,expected_revision integer,reply,request_id,fact_changes?array,media_analysis_id?,production_cues?array | sequence,revision; atomic reply/facts/cues/jobs/update/replay | `BidPilot/conversation.py:139-184,506` |
| 18 start_reference_analysis | asset_id | id,status; existing run/new queued | `BidPilot/conversation.py:252-259,507` |
| 19 get_reference_evidence | analysis_id | id,asset_id,status,error,result,observations | `BidPilot/conversation.py:261-266,508` |
| 20 get_reference_frame | observation_id | JPEG image content; derived-file read | `BidPilot/conversation.py:268-273,509`; `BidPilot/mcp_server.py:739-741` |
| 21 correct_reference_observation | observation_id,body,reason | observation_id; append linked correction | `BidPilot/conversation.py:275-282,510` |
| 22 claim_conversation_work | inquiry_id,worker_id,lease_seconds?number(default300) | inquiry_id,through_sequence nullable,lease_until nullable; lease or already_leased | `BidPilot/conversation.py:199-219,511` |
| 23 return_conversation_work | inquiry_id,worker_id | released count; requeue owned jobs | `BidPilot/conversation.py:221-227,512` |
| 24 estimate_scope | deal_id,scope_revision_id,spec object,apply_overrides?bool | estimate_id,estimate,applied_overrides?; persist | `BidPilot/estimating.py:402-413,1168-1182` |
| 25 check_resource_schedule | tasks array | feasible,allocations,blocked_task_id?; read scheduling | `BidPilot/estimating.py:447-523,1183-1184` |
| 26 save_producer_override | deal_id,scope_revision_id,item_id,field,value number,basis_date,note,request_id,supersedes? | override_id; dated replacement/replay | `BidPilot/estimating.py:687-717,1185-1191` |
| 27 merge_scope_setups | spec object,source_setup_ids array,target_setup_id | spec; transformation | `BidPilot/estimating.py:742-777,1192-1195` |
| 28 split_scope_setup | spec object,setup_id,groups array | spec; transformation | `BidPilot/estimating.py:780-829,1196-1199` |
| 29 propose_scope_alternatives | spec object,offer object,as_of,transformations array | opportunities,alternatives,rejected,base_planning_hours | `BidPilot/estimating.py:1052-1102,1200-1204` |
| 30 search_portfolio | query | assets incl project_id private linkage | `BidPilot/portfolio.py:142-149,185` |
| 31 get_portfolio_frame | asset_id,seconds number | image content; temp files/decoder invocation | `BidPilot/portfolio.py:152-182,186` |
| 32 commit_portfolio_search | request_id,version_id,results array[{asset_id,why}] | ok true; curate/persist | `BidPilot/portfolio.py:114-139,187` |
| 33 compose_proposal_page | version_id,page object | version_id; new sample proposal version | `BidPilot/commerce.py:714-719,744` |
| 34 price_additional_work | version_id,kind,quantity integer,as_of,deadline,base_order_id?string/null,request_id? | order_id,digest; calculated draft/replay when keyed | `BidPilot/commerce.py:317-350,729-734,745` |
| 35 price_detailed_additional_work | version_id,spec object,tasks array,description,as_of,deadline,base_order_id?string/null,request_id? | order_id,digest; detailed draft/replay | `BidPilot/commerce.py:353-384,736-741,746` |
| 36 get_change_orders | version_id | orders[{id,status,terms,digest,base_order_id}]; private read | `BidPilot/commerce.py:709-727,747` |

Original7 tools advertise properties without required arrays; later5 and extension tools advertise required. Extension wrapper validates declared top-level types/unknown keys, not full nested JSON Schema. Sample checks are explicit per-deal, but inquiry/evidence tools have no general sample classification; trusted per-studio process is required. (`BidPilot/mcp_server.py:656-685,709-738,760-800`; `BidPilot/test_stage12_mcp_agents.py:65-79`)

Separate project MCP has6 tools: project_summary() -> root/files/scripts/git status; read_project_file(path,max_lines?300) -> UTF8 text1..1000lines; search_project(query,max_results?60) -> literal rg text, nominal1..200 per-file max-count; project_diff() -> status/unstaged diff; run_project_check(check enum test/lint/build/check) -> npm declared script result; write_handoff(summary,next_steps) -> Markdown append. It is not read-only and does not sandbox declared package scripts. (`tools/qwenwork-mcp-bridge.js:102-223`)

### 6.5 Skill Contracts

| Skill | Inputs/output/order/hard rules | Source |
|---|---|---|
| bidpilot-intake | Raw/form/source/demo clock/prior offer -> as_of,offer,evidence,missing_fields,clarification; preserve ambiguity/inheritance; buyer requests not studio approval | `bidpilot-build-guide/bidpilot-intake/SKILL.md:10-19` |
| bidpilot-scoping | Complete offer/evidence/private history -> scope_profile,comparable_ids,differences; empty if none; frame cues uncertain, producer supplies effort; never pass other-client/history to negotiator | `bidpilot-build-guide/bidpilot-scoping/SKILL.md:10-27` |
| bidpilot-engine | Structured offer+5tables or outbound payload -> verbatim deterministic result; test after install, CLI, validate schema, block on failure; no model arithmetic | `BidPilot/bidpilot-engine/SKILL.md:10-19` |
| bidpilot-negotiator | Customer context+public options -> draft/option_ids; preferably number-free; supported units/exact terms, one repair/check; no send/publish/accept/book | `bidpilot-build-guide/bidpilot-negotiator/SKILL.md:10-30` |
| bidpilot-run | Stable source key/raw/workspace date/change version -> deal/status/trace/private fallback; intake->scope->engine->draft->gate->human; stale reread/no unverified external retries | `bidpilot-build-guide/bidpilot-run/SKILL.md:10-23` |
| bidpilot-runtime | Page means local MCP; legacy/date2026-11-23/direct costs explicit0; pending categories; conversation claim/context/evidence/commit/pricing/composition; amendments stop at review | `bidpilot-build-guide/bidpilot-runtime/SKILL.md:8-58` |

Installed current intake/scoping/negotiator/run/engine instructions match the roles above; installed runtime lacks the repository's explicit current-sequence/media-only examples. Do not infer whole-directory installed parity from instruction similarity. (`/Users/teamtham/.qwenwork/skills/bidpilot-runtime/SKILL.md:47-53`; repo runtime `:52-55`)

## 7. State Transitions And Enforced Human Gate

| Entity | Transitions / checks | Source |
|---|---|---|
| Intake deal | missing-only -> needs_information/no offer; completed same identity -> new/first offer; invalid rejects; duplicate preserves raw/client/first offer | `BidPilot/core.py:153-243` |
| Offer/evaluation | evaluation unique per offer; repeated evaluate returns stored result/latest version for that offer; new/change_requested deal -> evaluated | `BidPilot/core.py:481-487,523-536` |
| Draft version | edit creates new numbered version, copies options/page/import subset, resets check/digest/token/acceptance; core low-level editor lacks universal editable/latest guard | `BidPilot/core.py:808-835`; stronger caller `BidPilot/commerce.py:93-100`; `BidPilot/mcp_server.py:278-285` |
| Check | validates option set/order/subset/terms/seeds; failure blocked; pass draft/blocked -> checked; checked failed recheck -> blocked | `BidPilot/core.py:621-674,748-805` |
| Release | founder O+S; BEGIN IMMEDIATE; latest/allowed state/outbound passed/scope current/no pending buyer/seed/integrity/capacity/resources/digest; published+token hash/audit/current deal; old published superseded | `BidPilot/server.py:229-230,300-302`; `BidPilot/core.py:840-946` |
| Buyer acceptance | current released capability + owned option + no newer release + integrity; once, accepted_at/message; no reservation | `BidPilot/core.py:1033-1076` |
| Booking | founder confirms published accepted version; atomically replan/reserve resources+weekly allocations/revision/confirmation/audit; deal/version confirmed; retry prior IDs | `BidPilot/core.py:1134-1285` |
| Closure | confirmed -> closed administrative; active reservations preserved; confirmed links still resolvable | `BidPilot/core.py:1000-1009,1288-1311`; `BidPilot/test_stage11.py:314-328` |
| Amendment | draft->approved->accepted->confirmed, digest/latest confirmed base/no closeout; additive reservation only | `BidPilot/commerce.py:398-468,548-604` |
| Inquiry facts | proposed/confirmed/excluded evidence statuses, append-only supersedes; confirmed means buyer verified, not studio accepted | `BidPilot/conversation.py:160-176`; `BidPilot/mcp_server.py:644-646` |
| Inquiry jobs/media | queued->leased->done, expiry/release requeues; reference queued->running->done/failed then notification | `BidPilot/conversation.py:180-218,295-414` |
| Operations event | queued->leased->completed or retry queued/dead; attempts/lease-token/lane; dead requeue resets budget | `BidPilot/operations.py:522-632,698-712` |

Digest includes version ID, exact draft, ordered option-row IDs/public terms and optional page JSON. This binds bytes, not semantic truth or full internal resource evidence. (`BidPilot/core.py:595-608,669-674`)

Unconfirmed inquiry proposal blocks on changed fact revision or unprocessed buyer turn; confirmed bypasses scope_error. Thus post-booking live-brief changes must be routed as requested amendment, not silently adopted as committed scope. (`BidPilot/inquiry_quote.py:14-28`; `BidPilot/test_studio_journey.py:211-224`)

## 8. Security And Trust Boundaries

**Existing safeguards:** per-launch random owner token, loopback binding, public-Host owner denial, same-origin POST, capability hashes/revocation, no-store/noindex/frame ancestry, escaped proposal text, server-side release digest/options/current-version checks, parameterized identity queries, transactional idempotency, narrow product tools with no release/SQL/shell, bounded media/proposal uploads. (`BidPilot/server.py:100-179,413-428,491-502,2065-2086`; `BidPilot/commerce.py:124-139`; `BidPilot/mcp_server.py:56-83,395-425,719-723`; `BidPilot/conversation.py:236-259`)

**Not complete protection:**

- Project bridge lexical containment follows symlinks and omits .env/log/DB/.venv/media/credentials from excluded directories. Explicit reads can expose secrets, contradicting docs. **Inference: symlink escape/secret-file exposure.** (`tools/qwenwork-mcp-bridge.js:19,41-55,181-187`; `QWENWORK_MCP.md:32`)
- Handoff has complete historical proposal bearer links; do not publish handoff or copy them into this report. Current validity untested. (`QWENWORK_HANDOFF.md:99,119`)
- Origin checks compare untrusted Host rather than loopback allowlist; correct public proxy Host rewriting/route allowlist is required. **Inference: misconfiguration changes authority boundary.** (`BidPilot/server.py:155-179`; `docs/RUNNING.md:42`)
- Public inquiries/uploads lack quote limiter/storage quota; a single capability can accumulate files/jobs. Owner bodies generally unbounded. **Inference: resource exhaustion.** (`BidPilot/conversation.py:56-69,236-250,469-485`; `BidPilot/server.py:143-149,388-394`)
- Chat commitments/private labels/numbers are English lexical guards; buyer history permits numbers from any prior message, no semantic grounding. Cue/correction text bypasses buyer reply privacy checking but is public in inquiry evidence. (`BidPilot/conversation.py:112-176,275-282,456-463`)
- Leases do not authorize commit: commit has no worker/token ownership check. (**Inference: competing trusted worker can commit first.**) (`BidPilot/conversation.py:139-159,177-182,199-218`)
- Shared portfolio metadata bypasses outbound checking; only curated why text is checked. (`BidPilot/portfolio.py:49-70,124-133,258-278`)
- Python MCP assumes dict-like JSON-RPC outside general dispatch exception boundary; malformed scalar/null may terminate transport; original tools not fully type/schema validated. (`BidPilot/mcp_server.py:697-701,724-755,803-818`)
- No sample classification on all inquiry/evidence tools; no multi-studio identity on every application row. Trust explicit per-studio processes; do not expose local stdio as unauthenticated shared remote endpoint. (`BidPilot/mcp_server.py:674-679`; `BidPilot/conversation.py:40-43,252-282`; `docs/OPERATIONS.md:152-158`)
- Snapshot manifests validate hash/path syntax but not allowed root subtrees/actual credential exclusion; no ZIP decompression quota. Sequential DB/media snapshots are not globally consistent. Retention races active decoder/files before DB lock. (`BidPilot/operations.py:934-994,997-1024,1119-1132,1153-1160`)
- Configured dispatch executables are trusted arbitrary argv without shell, URL requires private token and refuses redirects. Minimal env reduces secret inheritance, but captured subprocess stdout/stderr are not memory-bounded during execution. (`BidPilot/operations.py:769-789,801-914`)
- Media subprocesses inherit environment and run trusted selected decoders against untrusted bytes; no OS sandbox is declared. Initial180s lease can expire during40*15s OCR calls before renewal; shared intermediates not lease-fenced. (`BidPilot/conversation.py:303-359`)

Prompt-injection rules explicitly treat buyer text/video OCR/files/portfolio descriptions as evidence, never instructions; fake founder agreement does not authorize release. This is agent guidance plus procedural release protection, not evidence that every model reply is safe. (`bidpilot-build-guide/bidpilot-runtime/SKILL.md:45-50`; `bidpilot-build-guide/bidpilot-intake/SKILL.md:19`; `BidPilot/core.py:840-946`)

## 9. Test Coverage And Actual Execution Evidence

**Fresh supplied verification; not rerun for this document:**

| Execution | Actual supplied result |
|---|---|
| App: `../.venv/bin/python -B -m unittest discover -p 'test_*.py'`, cwd `BidPilot` | Ran219 tests in63.755s, OK; no failures/skips summary supplied; warnings unclosed subprocess pipes/SQLite connections |
| Primary engine: `../../.venv/bin/python -B -m unittest discover -p 'test_*.py'`, cwd `BidPilot/bidpilot-engine` | Ran6 in0.005s, OK |
| External checker: `.venv/bin/python -B "/Users/teamtham/Documents/ChatGPT/hackathon idea/bidpilot-build-guide/verify_qwenwork_regressions.py" "/Users/teamtham/hackathon/BidPilot"` | PASS duplicate intake offers=1,second_fresh=False,same_offer=True; PASS deleted options original_options=1,errors=['option_keys_not_complete','option_terms_mismatch:as_offered'] |
| Engine/data auditor | Reference6 pass, byte parity pass,5 selected in-memory tests pass; pure probes reproduce mixed-option promise, double-credit task coverage, reduced estimate/unchanged schedule |

These are **Audit execution evidence**, not fictitious file-line claims. The external checker source was not audited here. Auditor saved result was the specifically authorized evidence file supplied by user; its body recorded results, not current runtime state. Runtime media/production accuracy/live scheduler/Hashmeta integration remain unverified by these results.

| Each test source | Coverage / limitation | Source |
|---|---|---|
| test_http.py | Real owner/CSRF/duplicate/figures/public privacy/timeline/comparison/gate/tamper/stale accept/change/embed | `BidPilot/test_http.py:127-518` |
| test_mcp.py | Protocol/catalog/schema/workflow/trace/sample/ownership/replay/concurrent claims/saves/public projection | `BidPilot/test_mcp.py:141-688` |
| test_conversation.py | Retry/stale/reference ownership/binding/notification/guard/cues/lease/decode; whisper conditional, sine-tone test not transcription accuracy; macOS sample conditional | `BidPilot/test_conversation.py:14-158` |
| test_operations.py | Isolation/modes/concurrent queue/dispatch minimal envelope/discovery/validation/redirect/retries/lanes/verifier/adapter/snapshot/retention | `BidPilot/test_operations.py:32-447` |
| test_qwenwork_configs.py | Config path remapping/command/catalog; Node conditional; not actual configured interpreter validation | `BidPilot/test_qwenwork_configs.py:34-121` |
| test_stage12_mcp_agents.py | Step/output replay/ownership/schema/site receipt/tampered payload/reset lock/change/privacy | `BidPilot/test_stage12_mcp_agents.py:35-233` |
| test_stage13_intake_form.py | Form/CSRF/size/types/receipt/no premature deal/claim/resume proof/same deal | `BidPilot/test_stage13_intake_form.py:55-134` |
| test_stage14_dashboard_ops.py | Recompute/import replay/conflicts/subsets/rollback/reset/non-sample/capacity/routes/polling/release tamper | `BidPilot/test_stage14_dashboard_ops.py:29-222` |
| test_commerce.py | Escape/orders/digest/closeout/task totals/rates/creative/search/revoke/resources; release guard/outbound/capacity mocked in portions | `BidPilot/test_commerce.py:18-200` |
| test_studio_journey.py | Socket-free Handler/MCP, public origin/inquiry/media/detailed facts/rates/tasks/release pending-turn/booking resource | `BidPilot/test_studio_journey.py:20-239` |
| test_expanded_lifecycle.py | Inherits Journey tests plus collision preview/creative/amendment/closeout/history/stale/privacy/JS syntax; inherited tests repeat base journeys | `BidPilot/test_expanded_lifecycle.py:19-22,80-313` |
| test_demo_smoke.py | Replay exit/banner/final booking label | `BidPilot/test_demo_smoke.py:8-15` |
| test_pending_offers.py | Structured change listed until evaluate, first/non-sample excluded | `BidPilot/test_pending_offers.py:49-78` |
| test_phase5.py | Full HTTP sample lifecycle/preview/rotate/needs-info/confirmation/closure/seed tamper | `BidPilot/test_phase5.py:131-435` |
| test_stage7.py | Node parse/fixtures/UI markers/explicit captions/adversarial exact terms/queued ambiguity | `BidPilot/test_stage7.py:145-239` |
| test_stage9.py | Conditional Playwright real clicks/duplicate disabled/network error/retry/phased accept; some fixed sleeps | `BidPilot/test_stage9.py:63-77,118-296` |
| test_stage11.py | Exact ledger/residual/concurrency/phasing/stale release/confirmation/restart/rollback/privacy; broad description assertion is stale-risk | `BidPilot/test_stage11.py:95-419` |
| test_regressions.py | Needs-info/invalid/resume/comparison/trigger migration/duplicate/options/approve-edit race; HTTP-named deletion test uses core directly | `BidPilot/test_regressions.py:53-337`, specifically `:241-258` |
| test_overnight_regressions.py | Tamper accept/confirm, names, live confirmed links, races, IDs/namespaces/malformed/source/state/caps/nonfinite/headers/seed | `BidPilot/test_overnight_regressions.py:147-485` |
| test_engine_sync.py | Exact file-set/byte parity two repo trees, not installed copy | `BidPilot/test_engine_sync.py:7-22` |
| test_estimating.py | Fixture/history/fees/rates/schedule/backtest/detailed coverage/setups/phases/overrides/evidence/persist; missing multi-item double-credit/readiness cases | `BidPilot/test_estimating.py:26-342` |
| test_alternatives.py | Derivative rewiring/shoot merge/phases/supplied removal/missing basis/pricing; tests allow prep+shoot as phase delivery | `BidPilot/test_alternatives.py:45-183` |
| test_calibration.py | Three-model/six holdout synthetic shape/determinism, not real accuracy | `BidPilot/test_calibration.py:7-20` |
| primary/reference test_engine.py |6 methods/13 fixtures: capacity/counter/all outputs/original/no-history/safety+validation | `BidPilot/bidpilot-engine/test_engine.py:67-152`; same reference path/ranges |
| backup test_operations.py | Older queue/isolation/snapshot/retention; outside BidPilot discovery, positional jobs insert predates migration | `operations-retry-backup.8B0gAuod/test_operations.py:30-268,117-118` |

Passing tests do not disprove pure-function defects absent from assertions. No measured line/branch coverage was supplied. README137/5skips, old prompt100 and submission191 are historical text, not219 result. (`README.md:98-101`; `docs/QWENWORK_PROMPT.md:9`; `docs/SUBMISSION.md:27`)

## 10. Configuration, Deployment And Operational CLI

| Config/name | Behavior/default | Source |
|---|---|---|
| BIDPILOT_MCP_DB | Product DB; first positional argument wins, then env, then sibling bidpilot.db | `BidPilot/mcp_server.py:38-39` |
| QWENWORK_PROJECT_ROOT | Project root else cwd | `tools/qwenwork-mcp-bridge.js:17` |
| BIDPILOT_FFMPEG | Explicit decoder else PATH ffmpeg else imageio_ffmpeg | `BidPilot/conversation.py:284-292` |
| BIDPILOT_TESSERACT | Explicit OCR else PATH; macOS Vision fallback | `BidPilot/conversation.py:332-349` |
| BIDPILOT_WHISPER_CLI | Explicit binary else sys.prefix/bin/whisper-cli else PATH | `BidPilot/conversation.py:354-358` |
| BIDPILOT_WHISPER_MODEL | Explicit model else sys.prefix/share/bidpilot/ggml-tiny.en.bin | `BidPilot/conversation.py:354-358` |
| Server --port/--db |8765/siblingDB | `BidPilot/server.py:2091-2092` |
| --public-origin | HTTPS strict origin, hostname/optional port, no path; used Host/auth check | `BidPilot/server.py:39-45,155-179,2093` |
| --embed-origin | Comma-separated HTTPS frame ancestors for inquire/i paths only | `BidPilot/server.py:125-129,2071-2073,2094` |
| operations --root/--studio | Required operator-resolved runtime/UUID | `BidPilot/scripts/operations_worker.py:22-23,51-52` |
| --command-json OR --url | Required exclusive target; absolute executable or HTTPS/loopback HTTP | `BidPilot/scripts/operations_worker.py:24-50`; `BidPilot/operations.py:769-789` |
| --token-file | Required private URL bearer file, no symlink/mode group-other access | `BidPilot/operations.py:801-809` |
| --timeout/--poll-seconds/--once |30s/.25s; poll .05..60; once0 attempted/2 empty, not success proof | `BidPilot/scripts/operations_worker.py:28-66` |
| adapter --app-db | Absolute existing DB, read-only completion | `BidPilot/scripts/qwenwork_adapter.py:58-92` |
| synthetic --seed/--out |20261121/optional write report | `BidPilot/scripts/synthetic_calibration.py:165-174` |
| backtest input/--database/--train-through | JSON or app DB replay, cutoff required, error2 | `BidPilot/scripts/backtest_estimates.py:102-120` |

Connector configs use /usr/bin/env python3/node, fixed checkout script paths and DB/root env; no credential fields observed. They do not select .venv/-B; PATH3.9.6 conflicts with documented>=3.11. (`qwenwork-bidpilot-mcp.json:3-12`; `qwenwork-mcp.json:3-12`; **Audit execution evidence**)

Operations creates0700 studio workspace/credentials/media and0600 initial DB/credentials; separate registry.sqlite3/application/operations DB perUUID. Retention defaults30days each, allowed1..3650. (`BidPilot/operations.py:107-118,205-265`)

Example intended worker command, not executed:

```sh
.venv/bin/python -B BidPilot/scripts/operations_worker.py \
  --root /private/bidpilot-runtime --studio STUDIO_UUID \
  --command-json '["/absolute/venv/bin/python","/checkout/BidPilot/scripts/qwenwork_adapter.py","--app-db","/private/bidpilot-runtime/studios/STUDIO_UUID/app.db"]'
```

Contract/source: `docs/OPERATIONS.md:46-74,99-112`; `BidPilot/scripts/operations_worker.py:20-66`. Shipped adapter only waits for independent scheduled consumer; no headless QwenWork API trigger is implemented.12attempts/backoff/dead letters; verifier checks durable work, never self-attestation alone in worker CLI. (`BidPilot/operations.py:715-759,859-875`; `BidPilot/scripts/qwenwork_adapter.py:4-19`)

Production recommendations in docs: TLS reverse proxy route allowlist inquire/i/p/portfolio-media/optionalquote, correct upstream Host, founder local URL; one process group/studio; stop service/workers before restore/revoke; encrypted access-controlled backups; no public caller chooses DB/root/studio. Not a supplied systemd/container/proxy configuration. (`docs/RUNNING.md:42`; `docs/OPERATIONS.md:11-28,125-165`; `README.md:115-127`)

`.gitignore` excludes server.log/all logs,db/journal/sqlite3/cache/pyc/node_modules/.env*,db.media/WAL/SHM/.venv. Ignore rules prevent commits, not bridge reads or credential redaction. (`.gitignore:1-18`; `tools/qwenwork-mcp-bridge.js:19,41-55`)

## 11. Ranked Defects And Inconsistencies

Priority is audit judgment, not a product severity taxonomy. Reproduced probes are attributed to auditor execution; other impacts are labeled inference.

| Priority | Defect / consequence / evidence |
|---|---|
| High1 | Mixed-option outbound promise passes because independent allowed amount/date/count sets lose option association. Reproduced `5 videos by 2026-12-04 for S$2,050` against counter options, no violations. Digest preserves accepted prose, does not fix semantic mismatch. (`BidPilot/bidpilot-engine/engine.py:417-458`; `BidPilot/core.py:595-608`; **Audit execution evidence**) |
| High2 | One task's full minutes credited to every linked work item. Reproduced two1h items/one60min task plus unrelated60min slack passes. (`BidPilot/estimating.py:236-251,628-633`; `BidPilot/test_estimating.py:195-208`; **Audit execution evidence**) |
| High3 | Detailed phases validate counts/dates/dependency closure but not deliverable completion; tests permit phase-one prep only, or prep+filming for two videos. **Inference: premature delivery promise.** (`BidPilot/estimating.py:570-597,986-1008`; `BidPilot/test_estimating.py:272-283`; `BidPilot/test_alternatives.py:114-124`) |
| High4 | Manual amendments permit positive hours/zero price without floor/resource checks; founder authorization is not deterministic pricing validation. **Inference: escape hatch undermines universal floor claim unless intentional.** (`BidPilot/commerce.py:249-272,387-412,442-464,577-599`) |
| High5 | Original release/confirmation use stored as_of; additional orders clamp to today. **Inference: late booking can reserve elapsed weeks.** (`BidPilot/core.py:886-892,1194-1206`; `BidPilot/commerce.py:580-583`) |
| High6 | Project bridge lexical paths follow symlinks and permit explicit .env/log/DB/.venv reads; docs claim no credentials/outside root. (`tools/qwenwork-mcp-bridge.js:19,41-55,181-187`; `QWENWORK_MCP.md:32`) |
| High7 | Public inquiry/upload accumulation lacks quotas/rate limit; cue/correction privacy guard bypass; advisory commit leases. **Inference: resource and disclosure/concurrent-agent risks.** (`BidPilot/conversation.py:56-69,139-182,199-218,236-250,275-282,456-485`) |
| High8 | Snapshot allowlist absent/credential flag trusted, sequential DB/media consistency, retention before lock/active-run checks. **Inference: unsafe archive/restore and missing media.** (`BidPilot/operations.py:934-1024,1119-1160`) |
| Medium1 | Reduced alternative effort leaves task minutes unchanged; reproduced27planned versus33scheduled; weekly/resource models disagree. (`BidPilot/estimating.py:863-885,675-684`; `BidPilot/capacity.py:136-145`; **Audit execution evidence**) |
| Medium2 | Detailed floor_rate accepts0/noninteger; items zero; catalog validates cost/sell rates, not policy floor. **Inference: no inherited70floor guarantee.** (`BidPilot/estimating.py:298-315,374-399,628-649`) |
| Medium3 | Detailed amendments omit `_validate_task_coverage` used by initial quotes; minimum calls/item coverage inconsistent. (`BidPilot/commerce.py:353-377`; `BidPilot/estimating.py:628-640`) |
| Medium4 | Reset leaves task_actuals,producer_overrides,portfolio_search_requests,inquiry_receipts; retains founder rates/history/calendar/portfolio intentionally or ambiguously. No FK cascade. (`BidPilot/dashboard_ops.py:137-160,190-204`; `BidPilot/conversation.py:521-529`; schemas section4) |
| Medium5 | Supplied-footage alternative removes shoots/tasks/expenses but retains shoot_days/location/mode terms; override allowlist excludes them. (`BidPilot/estimating.py:548,1011-1049,1069`; `BidPilot/test_alternatives.py:130-147`) |
| Medium6 | Legacy evaluation schema incompatible with extended application output; strict consumers/engine skill validation disagree. (`BidPilot/bidpilot-engine/evaluation-output.schema.json:105-227,303-313,416-426`; `BidPilot/estimating.py:169,195-199,567-569,648-672`; engine SKILL `:14`) |
| Medium7 | Inquiry revision/evidence ownership does not prove fields agree with confirmed current facts; proposed/excluded/superseded evidence accepted by ownership check. (`BidPilot/inquiry_quote.py:35-56`; `BidPilot/estimating.py:269-282`) |
| Medium8 | Greedy lexicographic scheduler has no search; failure not proof of impossibility. Item dependencies not automatically schedule dependencies; resource roles not item roles. (`BidPilot/estimating.py:340-362,476-523`; `BidPilot/test_studio_journey.py:179-195`) |
| Medium9 | Seed hash/static engine version insufficient replay provenance; founder history/calibrated rates/source code/full internal schedules not bound by public digest. (`BidPilot/core.py:29,494-507,595-608,669-674`; `BidPilot/estimating.py:147-199,667-671`) |
| Medium10 | JSON-RPC top-level malformed values may crash; required metadata original7 missing, top-level-only extension validation; no universal sample guard. (`BidPilot/mcp_server.py:656-685,697-738,803-818`) |
| Medium11 | Confirmed creative-change API still queues ordinary creative work while UI/runtime use amendments; confirmed inquiry scope gate bypass may confuse live requested brief. (`BidPilot/commerce.py:149-163,479-507`; `BidPilot/inquiry_quote.py:14-28`; runtime SKILL `:58`) |
| Medium12 | Operations recovery excludes intake/ordinary changes/unevaluated/needs-info/additional work; shipped adapter verifies, does not wake agent; lanes do not independently order scheduled consumer. (`BidPilot/operations.py:400-480`; `BidPilot/mcp_server.py:492-547`; `BidPilot/scripts/qwenwork_adapter.py:4-19`) |
| Medium13 | Metrics finite values not enforced, sensitive values can hide under innocuous label keys; most metrics hooks not emitted by media path. Captured subprocess output/long initial OCR lease unbounded. (`BidPilot/operations.py:634-657,885-901`; `BidPilot/conversation.py:303-359`; `docs/OPERATIONS.md:159-162`) |
| Medium14 | Historical handoff includes bearer links and source/runtime divergence; later recorded missing-info/tool failures not current reproduction. (`QWENWORK_HANDOFF.md:99,109-119,147-160`; current core `:153-243`) |
| Medium15 | Hosted field names/options mismatch local quote allowlist, budget/deadline optional, consent extra; provider mapping needed. Stored report no CRM verification. (`alexstudio-q2-evidence/check-report.json:157-380`; `BidPilot/server.py:47-91`) |
| Medium16 | Synthetic actuals centered on detailed plan, rolling history despite fixed cutoff, synthetic fees omit expense sell amounts, identical history/ratecard aggregates; no real accuracy claim supported. (`BidPilot/scripts/synthetic_calibration.py:65-95,123-162`; `synthetic-calibration-report.json:5-104`) |
| Lower1 | Static ENGINE_VERSION not source hash; detailed dashboard buffer undefined because estimate lacks buffer_pct; BELOW_FLOOR explanation always budget though list can fail. (`BidPilot/core.py:29`; `BidPilot/server.py:1815,1853-1863`; `BidPilot/estimating.py:379-387`; `BidPilot/bidpilot-engine/engine.py:334-335`) |
| Lower2 | Operations lane migration lacks application's locked second check; arbitrary per-week rounding need not equal aggregate rounding. (`BidPilot/operations.py:385-390`; `BidPilot/db.py:88-99`; `BidPilot/capacity.py:151-179`; engine `:181-187`) |
| Lower3 | Old prompt forbids buyer messages and says missing intake rejected; installed runtime lacks examples; run skill names awaiting_founder but implementation checked. Counts137/100/191 stale versus supplied219. (`docs/QWENWORK_PROMPT.md:45,88,192`; runtime repo `:52-55`; installed runtime `:47-53`; `bidpilot-build-guide/bidpilot-run/SKILL.md:16`; `README.md:98`; `docs/SUBMISSION.md:27`) |
| Lower4 | Config PATH Python3.9.6 vs supported>=3.11, fixed temp embed origin, warnings open resources; broad stage11 description assertion appears inconsistent with explicit booking language. Fresh supplied suite nevertheless OK: do not claim independently reproduced failure. (`qwenwork-bidpilot-mcp.json:5-11`; `bidpilot-embed/index.html:19-24`; `BidPilot/test_stage11.py:417-419`; `BidPilot/mcp_server.py:644-646`; **Audit execution evidence**) |

Stored synthetic result: median error5.17h/underestimated50%/worst10.04 ratecard+history; detailed3.185h/83.33%/10.24h/cost14332.5cents. Lower median error is not safer underquoting or empirical accuracy. (`synthetic-calibration-report.json:5-12,37-44,69-76,102-104`)

## 12. Exact Extension Plan: Callback, Needs Information And Poller

Everything in this section labeled **Proposed** is a future change; no callback or additional tool was implemented by this report.

### 12.1 Proposed POST `/qwenwork/callback`

**Proposed contract:** authenticated server-to-server completion/lead ingestion, not a buyer-origin endpoint and never founder approval. Choose one explicit provider contract before coding; do not overload owner tokens or infer QwenWork APIs. Existing dispatch is outbound `_invoke`, shipped adapter is verification-only; no `/qwenwork/callback` appears in current route dispatch. (`BidPilot/server.py:213-344`; `BidPilot/operations.py:880-914`; `BidPilot/scripts/qwenwork_adapter.py:4-19`)

Proposed implementation order:

1. Add a dedicated early callback branch in `Handler.do_POST` before buyer `same_origin`; this branch must enforce its own documented service authentication, body cap/content type/schema/timestamp or replay proof. All other POSTs retain S/O/P/I behavior. Reject missing/bad auth before DB or business writes. Never use public request DB/root/studio filesystem parameters. Existing seam: `BidPilot/server.py:213-230`; isolation requirement `docs/OPERATIONS.md:11-28`.
2. Proposed payload version, stable event_id, event kind, explicitly allowed identifiers and result/input object; derive tenant/studio from configured process credential, not payload. Persist callback idempotency hash+result atomically with business write. Existing patterns: `_write_operation`, `inquiry_commits`, claim transaction. (`BidPilot/mcp_server.py:395-425`; `BidPilot/conversation.py:146-184`; `BidPilot/core.py:170-175`)
3. If ingesting hosted leads, explicit mapping: name->client/name_business, email->reply_email, brief->raw/project_brief, videos->deliverables, captioned_versions->caption_versions, caption_all->caption_every_deliverable, editing_complexity->editing_level, logistics_notes->travel_kit_talent. Ambiguous format/graphics labels require clarification, not hidden defaults. Preserve consent/source ID evidence. Mapping is **Proposed**, grounded in actual field mismatch. (`alexstudio-q2-evidence/check-report.json:157-380`; `BidPilot/server.py:47-91`)
4. If accepting agent turn completion, reuse `conversation.commit` with inquiry/current sequence/revision and add worker/lease ownership fencing first. Do not allow callback to submit arbitrary SQL, owner actions, paths or externally asserted booking success. Existing gap/seam: `BidPilot/conversation.py:139-182,199-218`.
5. Proposed response200 saved/replayed,202 queued,400 malformed,401/403 auth,409 stale/conflicting event,413 cap,429 quota; consistent JSON without token/private evidence. These codes are proposed, not current callback behavior.
6. Add signature/key rotation, constant-time validation, replay window and explicit rate/storage limits using the organization's authorized integration contract; no invented secret values or undocumented provider signature scheme. Keep secrets in configured credential files, no handoff/logger token writes. Existing private file practice: `BidPilot/operations.py:185-194,801-809`.

Proposed regression additions to `test_http.py`, `test_stage12_mcp_agents.py`, `test_conversation.py`, `test_operations.py`: valid/invalid auth, auth-before-connect, oversized/malformed, duplicate identical event, changed-payload conflict, wrong tenant/inquiry/asset, stale revision/lease, no approval tool/action, restart recovery. Existing test seams: `BidPilot/test_http.py:127-158`; `BidPilot/test_stage12_mcp_agents.py:35-96`; `BidPilot/test_conversation.py:14-36,87-107`; `BidPilot/test_operations.py:241-262,296-341`.

### 12.2 Needs-Information Resumption

Existing `claim_or_resume` is the implementation, not a missing feature: missing-only persists deal/status+missing paths/no offer; completed same identity creates first offer. Do not implement parallel deal-less checkpoint state. (`BidPilot/core.py:153-243`)

Exact current flow:

1. `claim_intake(source,source_id,client,raw_inquiry,offer_request,evidence?,request_id?)`; wrapper fields still required. Save incomplete result status/missing_fields privately. (`BidPilot/mcp_server.py:98-105,136-168`)
2. Discovery `t_list_pending_work().needs_information` reads sample deals/latest missing step. Clarify buyer through bound inquiry if available; legacy contact delivery requires explicit authorized mapping or Alex. (`BidPilot/mcp_server.py:519-534`; runtime SKILL `:30,43-50`)
3. Corrected payload MUST use new request_id while retaining same source/source_id. Old identical request_id replays old result; changed payload conflicts. (`BidPilot/mcp_server.py:109-135,169-175`)
4. Completed resume ->same deal/new first offer/new status; evaluate then negotiate/check/review, no automatic release. (`BidPilot/core.py:189-209`; runtime SKILL `:31-35`)

Proposed improvements: engine validation should report invalid supplied fields even when others missing; preserve full incomplete structured evidence and clarification state; prevent repeated unchanged polls from appending redundant needs-info steps; add a completion marker/query for targeted clarification wake. Relevant current behavior: `BidPilot/bidpilot-engine/engine.py:59-63`; `BidPilot/core.py:179-193`; `BidPilot/mcp_server.py:141-168`.

Tests: `BidPilot/test_mcp.py:300-331`, `test_regressions.py:53-111`, `test_phase5.py:132-157`, `test_stage13_intake_form.py:90-134`; proposed new cases missing+invalid, changed request key, retained partial evidence, no repeated clarification spam, stable external identity.

### 12.3 Poller / Low-Latency Consumer Hooks

Current app discovery `t_list_pending_work`, conversation `pending/claim_work/release_work/context/commit`; operations `discover_app_work/enqueue/claim/extend/complete/fail`; verification `event_completed/AppWorkVerifier`; adapter `payload_from_retry_key`. (`BidPilot/mcp_server.py:492-547`; `BidPilot/conversation.py:101-227`; `BidPilot/operations.py:392-632,715-759`; `BidPilot/scripts/qwenwork_adapter.py:31-55`)

Proposed consumer sequence: discovery->skip revoked/stale->claim fenced inquiry lease->read latest context->inspect current reference evidence/image->one grounded commit using latest buyer sequence (0 attachment-only) and expected revision->price sufficient brief->compose/check->human review. Return lease on stop; reread stale context; reuse exact idempotency key only on exact payload retry. Existing runtime order: `bidpilot-build-guide/bidpilot-runtime/SKILL.md:47-58`.

Proposed operations extension: explicitly add wake kinds for inbox inquiry, ordinary change, unevaluated change, needs-information-resolved and additional-work; derive stable retry keys from committed business rows; add corresponding `event_completed` checks and adapter retry-key reconstruction. Do not mark event complete because an agent says OK. Current four-category limitation and verifier seam: `BidPilot/operations.py:400-480,715-744`; broader worklist `BidPilot/mcp_server.py:492-547`.

Keep scheduled task as independent recovery only; real low-latency trigger must use a verified authorized QwenWork interface. Shipped adapter does not provide it. Existing loop scans before dispatch and sleeps only on empty;12attempts dead-letter with requeue operator action. (`BidPilot/scripts/operations_worker.py:58-66`; `BidPilot/scripts/qwenwork_adapter.py:4-19`; `BidPilot/operations.py:698-712,871-875`)

Proposed tests extend `test_operations.py:114-173,278-341`, `test_conversation.py:87-107`, `test_pending_offers.py:49-77`, `test_expanded_lifecycle.py:160-188`: all new categories, revoked work exclusion, media-only lane fencing, lost lease/stale callbacks, two consumer races, deadline expiration, dead-letter recovery, no extra buyer reply, no automatic release.

## 13. Glossary

| Term | Precise meaning / source |
|---|---|
| Sample data | Explicit synthetic workspace/records; not permission to access real tenant records (`README.md:8-16`; `BidPilot/mcp_server.py:56-62`) |
| as_of | Stored evaluation planning date, demo fixed2026-11-23; not automatically current clock (`BidPilot/core.py:144-149`; runtime SKILL `:30`) |
| Offer | Structured scoped request/evidence/comparable set, numbered round (`BidPilot/db.py:21-23`) |
| Evaluation | Stored deterministic verdict/options/private metrics unique to offer (`BidPilot/db.py:24-27`) |
| Proposal version | Numbered exact draft/page/option set with gate/capability/acceptance state (`BidPilot/db.py:28-31,67-69`) |
| Public option ID | Engine kind within evaluation, distinct from persisted option-row UUID (`bidpilot-build-guide/BUILD_GUIDE.md:23`; `BidPilot/db.py:26-27`) |
| Planning hours | Buffered rounded labor estimate, not specific appointment slots (`BidPilot/bidpilot-engine/engine.py:196-211,181-187`) |
| Capacity | Eligible sample free weekly hours after reserve and active booking allocations (`BidPilot/bidpilot-engine/engine.py:181-187`; `BidPilot/test_stage11.py:124-140`) |
| Floor | Commercial minimum planning*rate+expenses; not true labor cost/margin (`BidPilot/bidpilot-engine/engine.py:201-211`; `docs/REFERENCE_TO_QUOTE_PLAN.md:77`) |
| List | Setup+per-output+addons; does not automatically include direct expenses (`BidPilot/bidpilot-engine/engine.py:201-211`) |
| Comparable | Compatible historical normalized effort evidence; fee history calibration separate (`BidPilot/bidpilot-engine/engine.py:143-178`; `BidPilot/estimating.py:171-199`) |
| Outbound gate | Lexical public-prose checker; not semantic promise validation (`BidPilot/bidpilot-engine/engine.py:357-468`; build guide `:43`) |
| Digest | Hash of exact version/draft/ordered public options/page, binds reviewed bytes (`BidPilot/core.py:595-608`) |
| Capability | Opaque bearer path token; hash persisted, secrecy grants access; must not appear in report (`BidPilot/conversation.py:56-73`; `BidPilot/core.py:924-946`) |
| Acceptance | Buyer selection pending founder confirmation, no reservation (`BidPilot/core.py:1033-1076`) |
| Confirmation | Founder atomic booking and capacity/resource allocation (`BidPilot/core.py:1134-1285`) |
| Scope revision | Inquiry fact revision used for concurrency/pricing binding, not semantic agreement proof (`BidPilot/conversation.py:172-182`; `BidPilot/inquiry_quote.py:35-56`) |
| Fact confirmed | Buyer-verified requested fact, not studio approval (`BidPilot/mcp_server.py:644-646`) |
| Observation / cue | Timestamped frame/OCR/speech or agent interpretation with uncertainty/source (`BidPilot/conversation.py:167-176,390-405`) |
| Setup / derivative | Shared production session and incremental output from master; explicit effort/dependency inputs (`docs/DETAILED_ESTIMATE.md:34-40`) |
| Resource schedule | Greedy dated simultaneous resource blocks; failure not impossibility proof (`BidPilot/estimating.py:447-523`) |
| Amendment | Additive digest-bound work order; does not free prior reservations (`BidPilot/commerce.py:1-5,594-599`) |
| Closeout | Producer-verified actuals/selected scope, optional task totals; eligible reliable history (`BidPilot/commerce.py:619-706`) |
| Idempotency key | Stable key+payload hash replays same result and rejects changed reuse (`BidPilot/mcp_server.py:395-425`) |
| Lease / lane | Time-limited queue ownership/token and related-work serialization; chat commit currently lacks ownership fencing (`BidPilot/operations.py:522-601`; `BidPilot/conversation.py:139-218`) |
| Dead letter | Exhausted failed event requiring operator inspection/requeue (`BidPilot/operations.py:604-632,682-712`) |
| Scheduled consumer | External QwenWork task reasoning/committing; not the shipped verifier adapter (`docs/OPERATIONS.md:99-112`) |
| Audit execution evidence | Actual supplied test/probe results distinguished from source assertions and historical journals (section9) |

**Handover conclusion.** Deterministic fixtures and repository parity pass, and supplied fresh application verification is219tests OK. Existing transactional/version/owner gates are substantial. Before real production commitments, prioritize semantic option association, non-double-counted item schedules, deliverable-ready phases, consistent amendment floors/resources, current-date booking, bridge secret isolation, fenced agent commits and coordinated backup/retention. Passing fixture/integration suites are not proof of estimator accuracy, prompt-injection immunity or production deployment readiness. (`BidPilot/bidpilot-engine/engine.py:417-458`; `BidPilot/estimating.py:236-251,570-597`; `BidPilot/commerce.py:249-272,353-377`; `BidPilot/core.py:1194-1206`; `tools/qwenwork-mcp-bridge.js:41-55`; **Audit execution evidence**, section9)
