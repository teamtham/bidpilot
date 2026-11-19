# Codex build prompt: finish the non-QwenWork BidPilot code

Paste everything below the line into OpenAI Codex (cloud), pointed at the `teamtham/bidpilot` repository.

---

You are finishing the Python code for **BidPilot**, a hackathon entry for QwenWork x Hashmeta AI OPC (deadline 23 Nov 2026, 23:59 SGT). Work in the `teamtham/bidpilot` repo on a new branch off `main`, and open one pull request when done.

## 0. Read first

1. `AGENTS.md` (layout, who builds what, rules).
2. `bidpilot-build-guide/BUILD_GUIDE.md` (source of truth), especially sections 1, 3 and 5.
3. `bidpilot-build-guide/BIDPILOT_QWENWORK_BUILD_PROMPT.txt` (acceptance numbers).
4. The four agent skills in `bidpilot-build-guide/bidpilot-{intake,scoping,negotiator,run}/SKILL.md`. **You do not implement these agents.** Read them only so the tools you build give those agents exactly what they need.
5. The existing code in `BidPilot/`: `core.py`, `db.py`, `capacity.py`, `server.py`, `mcp_server.py`, and every `test_*.py`.

## 1. Product split (most important rule)

BidPilot is judged on QwenWork being "the engine of the product, not a checkbox". So the split is:

- **QwenWork agents (NOT your job):** intake (turning a raw inquiry or change message into a structured offer plus evidence), scoping (choosing comparable past projects and explaining differences), negotiator (writing client-facing draft copy), run (orchestrating the steps), the scheduled polling task, the Hashmeta connector, the Hashmeta website and CMS case study, and the demo video.
- **Your job:** the deterministic Python layer those agents call. That means the engine wrapper, storage, the founder dashboard, client proposal pages, the release boundary, and the **MCP tools** QwenWork uses to read and write that state.

Do not move agent judgement into Python. Concretely:

- Do **not** add an LLM call, OpenAI/Qwen SDK, or any network call anywhere in the repo.
- Do **not** extend `core.parse_change_body` (regex change parsing) or `core.draft_for` (template draft copy). They stay as clearly labelled deterministic fallbacks. New features must accept structured terms and draft text **from the agent** via MCP rather than generate them.
- Do **not** expose approve, reject, release, token rotation, client accept, booking confirmation or deal close via MCP. Those stay founder-only (dashboard) or token-holder-only (client page).

## 2. Current state (verified on `main`)

Already built and tested, keep it working:

- `BidPilot/bidpilot-engine/` (engine, schemas, 5 CSVs, 13 JSON cases): `cd BidPilot/bidpilot-engine && python3 -B -m unittest -v test_engine.py` passes 6/6. A byte-identical reference copy lives in `bidpilot-build-guide/bidpilot-engine/`.
- `db.py`: SQLite schema per BUILD_GUIDE section 5, plus `confirmations`, `mcp_operations`, `reservations`, `allocations`, `capacity_revisions`.
- `core.py`: claim-or-resume by unique `(source, source_id)`, evaluate (idempotent per offer), reevaluate, client change (new offer + version, never overwrites), save draft (new version, resets check), outbound check, SHA-256 digest binding, approve (atomic, supersedes), reject (audit only), token (≥128-bit, hash stored), client accept (once, pending founder), confirm booking with capacity reservation ledger, close deal.
- `server.py`: loopback-only `ThreadingHTTPServer`, per-launch owner token (`X-Owner-Token` or `?owner=`), same-origin required on every POST, founder dashboard at `/`, `/api/state`, `/api/{import,evaluate,check,draft,approve,reject,rotate-token,reevaluate,confirm-booking,close-deal,client-change}`, owner preview `/preview/<version_id>`, client page `/p/<token>` with `/accept` and `/request-change`, generic 404s, `no-store`, `noindex`.
- `mcp_server.py`: stdio JSON-RPC MCP server with exactly 7 tools: `claim_intake`, `get_deal`, `evaluate_offer`, `save_draft`, `check_draft`, `run_trace`, `negotiation_payload`. Sample-data only.
- Tests: `cd BidPilot && python3 -B -m unittest discover -p 'test_*.py'` runs 68 tests; 67 pass, and `test_stage9.py` errors at import because the Python `playwright` package is not installed.
- `tools/qwenwork-mcp-bridge.js` + `qwenwork-mcp.json`: a read-only project-inspection bridge (not the BidPilot product tools).

## 3. Hard constraints

1. **Never modify** `bidpilot-engine/engine.py`, its schemas, `data/*.csv` or `test_cases.json` (either copy). Never change a fixture or an expected number to make a test pass. If you find an engine bug, stop and describe it in the PR instead.
2. **Pinned numeric conventions.** All pricing and hours come from `engine.evaluate` / `engine.outbound_check`, never from new arithmetic. If you must display or compare, use the engine's values as given. The engine uses Python float `round(x, 2)` (not `Decimal` half-even); planning hours for Inquiry 1 must be **35.07** (half-even on 35.075 would give 35.08, which is wrong for this demo). Floor = `round(planning_hours * 70 + direct_costs_sgd, 2)`; effective rate = `round(budget_sgd / planning_hours, 2)`. Format SGD and hours to two decimals in the UI only.
3. **Acceptance numbers must still hold** (as_of 2026-11-23):
   - Inquiry 1: `accept`; planning 35.07 h, capacity 50.40 h, list S$2,700.00, floor S$2,454.90, rate S$76.99/h.
   - Counteroffer: `counter`; planning 52.32 h, capacity 28.80 h, list S$4,000.00, floor S$3,662.40, rate S$51.61/h; options: 2 videos by 2026-12-04 for S$2,050 (26.45 h, floor S$1,851.50); 3 by 2026-12-11 for S$2,700; 5 by 2026-12-18 for S$4,000; phased 2 by 2026-12-04 + 3 more by 2026-12-18 for S$4,000 total.
   - A draft containing "Our price floor is S$3,662.40" is blocked (`PRIVATE_NUMBER`/`PRIVATE_LABEL`) and cannot be approved or published.
4. **Loopback only.** The HTTP server binds `127.0.0.1` and nothing else. No `0.0.0.0`, no tunnels, no deploy configs.
5. **Never commit** `server.log`, `*.log`, `*.db`, `*.db-journal`, `__pycache__/` or anything holding owner tokens. `.gitignore` already covers them; check `git status` before every commit. Never print the owner token anywhere except stdout of the launching process.
6. **MCP contract stability.** The existing 7 tools keep their names, required arguments and response keys exactly. You may only add new tools, add optional arguments, or add new response keys. Update `test_mcp.py` only to add tests, never to loosen existing assertions.
7. **Standard library only** for runtime code (`sqlite3`, `http.server`, `hashlib`, `secrets`, `json`). Playwright is allowed as a test-only dependency.
8. Schema changes go through `db._MIGRATIONS` / `CREATE TABLE IF NOT EXISTS` so an existing `bidpilot.db` upgrades in place.
9. Every new write path is idempotent by a caller-supplied `request_id` (reuse the `mcp_operations` pattern in `t_save_draft`: same id + same payload hash replays the stored result, same id + different payload is rejected, all in one `BEGIN IMMEDIATE` transaction).
10. Anonymous routes and MCP responses meant for the negotiator must never contain floor, capacity, planning hours, rate, past-project rows, other client names, owner tokens or token hashes.
11. Keep `BidPilot/bidpilot-engine/` and `bidpilot-build-guide/bidpilot-engine/` byte-identical.

## 4. What to build

Work in this order and keep the full suite green after each stage. Name new test files as shown.

### Stage 12: MCP tools for the QwenWork agents (`mcp_server.py`, `core.py`, `db.py`)

The run skill needs to log real steps, persist intake/scoping/negotiator outputs, poll for work, and submit structured change terms. Add these tools (all sample-data only, all with JSON input schemas listing required fields):

1. **`record_step`**: `{deal_id, offer_id?, step_key, status, private_note?, request_id}`. `step_key` ∈ `claimed, intake, scoping, engine, draft, outbound, review, needs_information, stale_version, blocked, error`. `status` ∈ `running, ok, blocked, error, skipped`. Appends to `run_steps` (append-only; a `running` step may be finished by a later call with the same `deal_id`/`offer_id`/`step_key` that sets `finished_at`). Idempotent by `request_id`. Rejects unknown keys/statuses and offers not owned by the deal.
2. **`save_agent_output`**: `{deal_id, offer_id?, kind, payload, request_id}`. `kind` ∈ `intake, scoping, negotiator, clarification`. Store in a new append-only table `agent_outputs (id, deal_id, offer_id, kind, payload_json, request_id UNIQUE, created_at)`. Validate shape only: intake payload has `offer`, `evidence`, `missing_fields`, `clarification`; scoping has `scope_profile` (list of `{field, value, evidence}`), `comparable_ids`, `differences` (list of `{project_id, fields, why, evidence}`); negotiator has `draft`, `option_ids`; clarification has `questions` (list of strings). Do **not** feed this payload into the engine; the engine request still comes only from `claim_intake`/`submit_change_offer`. Clarifications are founder-only and never published.
3. **`get_scoping_history`**: `{}` returns the `seed_past_projects` rows (read-only mirror of `past_projects.csv`) for the scoping agent. Mark the response `"private": true` with a note that it must not be forwarded to the negotiator. Do not include it in `negotiation_payload` or any client route.
4. **`list_pending_work`**: `{}` returns `{inquiries: [...], change_requests: [...]}`.
   - `inquiries`: unclaimed rows from the new public intake inbox (Stage 13), each with `source`, `source_id`, raw fields and `received_at`. Excludes ones already claimed (a deal exists for that `(source, source_id)`).
   - `change_requests`: inbound client messages whose change was queued for clarification (no offer created), each with `deal_id`, `request_id`, `body`, `version_id` it was made against, and whether that version is still the current published one.
5. **`submit_change_offer`**: `{deal_id, request_id, version_id, offer_request, evidence?}`. This is how the intake agent turns a queued (or new) client change into a new offer round instead of the regex parser. Rules: `version_id` must be the deal's current published version, else return a `stale_version` error and record a `stale_version` step; `request_id` must match a stored inbound change message for this deal (or create the message if absent, with `body` from `evidence.raw_message`); `offer_request` must pass `engine.validation_errors`; create the new offer exactly once (retry returns the same `offer_id`); set deal status `change_requested` exactly as `core.client_change` does. Refactor shared logic out of `core.client_change` rather than duplicating it; existing `client_change` behaviour and tests must not change.
6. Extend **`claim_intake`** additively: accept `"site"` as a source (alongside `hashmeta` and `manual`) and an optional `evidence` object that is stored as an `intake` `agent_outputs` row. Do not change what `offers.evidence_json` holds (it is the engine request and `evaluate` reads it).
7. Extend **`run_trace`** additively with `agent_outputs` (kind, created_at, payload) for the deal.

Tests: `BidPilot/test_stage12_mcp_agents.py`. Drive the real stdio server in a subprocess on a temp DB (copy the harness in `test_mcp.py`). Cover: `tools/list` still contains the original 7 tools with unchanged schemas plus the new ones; idempotent retries and payload-conflict rejection for every new write tool; ownership and sample-only guards; a stale `version_id` on `submit_change_offer` is rejected and logged; a valid change submission produces the Counteroffer numbers above after `evaluate_offer`; `negotiation_payload` still never contains floor, capacity, planning hours, rate or another client's name after scoping history has been read; `approve`/`confirm_booking` are still unknown tools.

### Stage 13: Server-backed public intake form (`server.py`, `db.py`)

BUILD_GUIDE section 6 says that if Hashmeta CRM leads cannot be read, the Hashmeta site links to "the Page's own server-backed intake form" and the scheduled task polls that queue. Build it:

- `GET /quote`: anonymous, accessible, mobile-friendly "Request a quote" form showing the sample badge "Sample data · 23 Nov 2026 · Asia/Singapore". Fields (from BUILD_GUIDE section 6): name/business, reply email, project brief, video format, edit-only or on-location, shoot days, locations, interviewees, scripting (none/light/full), number of videos, editing complexity, graphics type, revision rounds, captioned video count, a visible "Caption every deliverable" checkbox, delivery deadline (date), SGD budget, free-text travel/kit/talent needs. A visible "Use demo preset" button fills standard editing, basic titles, light scripting, two revisions, no interviewees, one shoot day, one location. No direct-costs field (founder-only).
- `POST /quote`: same-origin required, JSON or form-encoded, body ≤ 16 KB, field-level validation of types only (no pricing). Store in a new table `inbox (id, source TEXT DEFAULT 'site', source_id TEXT UNIQUE, fields_json, raw_message, received_at, sample_data INTEGER DEFAULT 1)` with a server-generated `source_id` like `site-<16 hex>`. Simple in-memory rate limit (e.g. 20 submissions per 10 minutes per process). Response is a receipt page/JSON with the reference ID and **no price, date or timing promise**.
- The inbox does **not** create a deal. QwenWork claims it via `list_pending_work` → `claim_intake(source="site", source_id=...)`.
- Founder dashboard gets an "Inbox" panel listing unclaimed and claimed inquiries.

Tests: `BidPilot/test_stage13_intake_form.py`: GET renders the badge and all fields; cross-origin POST is 403; oversized and malformed bodies are 400; a valid POST stores one inbox row and returns a receipt with no `S$`, `$` or hour figures; the row appears in `list_pending_work` and disappears after `claim_intake`; a duplicate claim resumes the same deal; anonymous GETs of `/api/state` and `/` remain 403.

### Stage 14: Founder dashboard operations (`server.py`)

1. **Live timeline**: poll `/api/state` every 4 seconds while the tab is visible (pause on `visibilitychange`), with a visible "Live updates on/off" toggle and a manual Refresh button. Never animate invented events; only render stored `run_steps`.
2. Render the canonical run-skill steps (`claimed → intake → scoping → engine → draft → outbound → review`) per offer round, merging agent-recorded steps (Stage 12) with the Python-recorded ones (`intake`, `resume`, `engine_evaluate`, `client_change`, `reevaluate`, …) in timestamp order, with text labels for status (not colour alone).
3. Show agent outputs per offer: intake evidence table, scoping comparables and differences, negotiator `option_ids`, and any founder-only clarification draft labelled "Needs information, not sent".
4. **Import Run JSON (owner-only fallback)**: `POST /api/import-run` accepting the bundle from BUILD_GUIDE section 5 fallbacks: `{source, source_id, deal_id?, offer_request, evaluation?, draft, option_ids, outbound_result?, steps[]}`. Behaviour: claim-or-resume by `(source, source_id)`; **ignore** any supplied `evaluation` and `outbound_result` and re-run the real engine and outbound check server-side (store the supplied ones only as an `agent_outputs` row for comparison, flag a mismatch in the response); save `draft` as a new version; verify every `option_id` is an `option_key` of that version's own evaluation; append `steps[]` via the Stage 12 logic. Label imported runs "Imported run" in the dashboard. Add an upload/paste box for it.
5. **Sample reset (owner-only)**: `POST /api/reset-sample` requiring body `{"confirm": "RESET SAMPLE DATA"}`. Deletes rows from deal/offer/evaluation/option/version/step/message/approval/confirmation/reservation/allocation/agent_outputs/inbox/mcp_operations tables only where they belong to sample data, re-imports the seed mirror from the unchanged CSVs, and returns counts. Never touches files on disk.
6. **Launcher**: `python3 server.py [--port 8765] [--db PATH]` (defaults: port 8765, `BidPilot/bidpilot.db`). Port 0 stays available for tests via `make_server`. Print the owner URL to stdout once; never write it to a file.

Tests: `BidPilot/test_stage14_dashboard_ops.py`: polling script present and only hits `/api/state`; import-run with a tampered `evaluation` (e.g. floor 0) still stores the real engine result and reports the mismatch; import-run with an option ID from another evaluation is rejected; a duplicate import resumes the same deal; reset without the exact phrase is 400, with it empties sample rows and leaves seed tables at CSV row counts; every new route is 403 without the owner token and 403 cross-origin.

### Stage 15: Wiring, smoke test and hygiene

1. **QwenWork MCP config for the product tools**: add `qwenwork-bidpilot-mcp.json` at the repo root registering `mcp_server.py` as a stdio server named `bidpilot`:
   `/usr/bin/env python3 /Users/teamtham/hackathon/BidPilot/mcp_server.py` with env `BIDPILOT_MCP_DB=/Users/teamtham/hackathon/BidPilot/bidpilot.db` (same DB the dashboard uses). Document it in `QWENWORK_MCP.md` under a new "BidPilot product tools" section listing every tool and which agent skill uses it (intake → `list_pending_work`, `claim_intake`, `submit_change_offer`, `save_agent_output`; scoping → `get_deal`, `get_scoping_history`, `save_agent_output`; engine/run → `evaluate_offer`, `record_step`, `check_draft`; negotiator → `negotiation_payload`, `save_draft`, `save_agent_output`). Do not edit the skill files.
2. **End-to-end smoke script** `BidPilot/scripts/demo_smoke.py`: on a temp DB, start the HTTP server in-process and the MCP server as a subprocess, then replay the BUILD_GUIDE section 7 demo path using **fixture agent outputs clearly labelled "fixture replay, not the QwenWork agents"**: submit `/quote` → `list_pending_work` → `claim_intake` → `record_step`s → `evaluate_offer` → `save_draft`/`check_draft` → owner approve → open `/p/<token>` → client request-change → `submit_change_offer` → evaluate (counter) → blocked floor draft → clean draft passes → approve → client accepts phased → confirm booking. Assert every pinned number in section 3 and print a PASS/FAIL table. Exit non-zero on any failure. Add `BidPilot/test_demo_smoke.py` that runs it.
3. **Playwright stage 9**: make `test_stage9.py` importable without Playwright by wrapping the import and using `@unittest.skipUnless(HAVE_PLAYWRIGHT, "pip install playwright")` on the class. In your own environment, install it (`pip install playwright` then `python -m playwright install chromium`, or point `executable_path` at an existing Chromium) and actually run it so all tests pass, not skip. Report which happened.
4. **Engine sync test** `BidPilot/test_engine_sync.py`: assert every file under both `bidpilot-engine/` copies is byte-identical (ignore `__pycache__`).
5. **CI**: `.github/workflows/tests.yml` on push/PR: Python 3.11, install Playwright + Chromium, run the engine suite and `python3 -B -m unittest discover -p 'test_*.py'` from `BidPilot/`.
6. **Docs**: `docs/RUNNING.md` with: start the dashboard, register both MCP servers in QwenWork, reset sample data, run tests, run the smoke script, and the list of what QwenWork must do (section 5 below). Keep it short.

## 5. Explicitly NOT to build (QwenWork does these)

- Any intake/extraction logic from free text (LLM or new regex). Agents send structured `offer_request` and evidence.
- Comparable selection or difference reasoning beyond what the engine already validates.
- Client-facing copywriting beyond the existing `draft_for` fallback.
- An orchestrator, scheduler, cron, polling daemon or "run" loop in Python. QwenWork's scheduled task calls `list_pending_work` and the tools.
- Any Hashmeta API client, CRM integration, website, CMS page or case-study generator.
- Sending email/messages, publishing anywhere public, or booking real work.
- The demo video or the "unguarded AI" comparison.
- Changes to the five `SKILL.md` files.

## 6. Definition of done

- `cd BidPilot/bidpilot-engine && python3 -B -m unittest -v test_engine.py` passes (6 tests).
- `cd BidPilot && python3 -B -m unittest discover -p 'test_*.py'` passes with 0 failures, 0 errors, and Stage 9 actually run (or a clear note if your environment could not run a browser).
- `python3 -B BidPilot/scripts/demo_smoke.py` prints all PASS.
- `git status` shows no `.db`, `.log` or `__pycache__` files staged.
- The PR description lists: each stage and its files; the exact test output summary; new MCP tools with their argument schemas; any deviation from this prompt and why; and anything you could not verify.
