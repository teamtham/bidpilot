# Prompt: switch on the QwenWork API pipeline (reference video + agent-picked comparables)

Paste everything below the line into your coding agent, opened in `~/idea`.

---

You are working in `~/idea` (git repo, branch `qwenwork-openapi`). The live BidPilot server runs from `~/idea/BidPilot` under the launchd agent `com.bidpilot.server` (`deploy/run_server.sh`). Read `AGENTS.md` and `bidpilot-build-guide/BUILD_GUIDE.md` first and follow their rules. Run `git status` before touching anything: there are uncommitted edits in `conversation.py`, `qwenwork_api.py`, `server.py`, `studio_assistant.py` and tests. Keep them. Do not discard or reformat other people's work. Never commit `bidpilot.db`, `bidpilot.db.media/`, logs, or anything in `~/.bidpilot/`. No co-author trailers in commit messages.

## Goal

When a buyer chats or submits a brief, BidPilot drives a QwenWork **cloud** session through the OpenAPI (`https://api.qwenwork.ai`). Nobody presses Run Now and there's no desktop polling. In that session the agent:
1. watches the buyer's reference video itself, from QwenWork Drive, and
2. chooses comparable past projects and a "similar work" clip itself, from a client-safe portfolio file in QwenWork Drive.

BidPilot stays the authority. It validates everything the agent returns, the deterministic engine does all pricing, and only Alex approves and releases.

## What already exists (reuse it; don't rebuild)

- `qwenwork_api.py`: OAuth client (`Client.api(path, method, data)`, token in `~/.bidpilot/qwenwork_oauth.json`) and `worker()`. The worker calls `qwenwork_runtime.tick()` only when env `BIDPILOT_QWENWORK_EVENT_RUNTIME=1`.
- `qwenwork_runtime.py`: durable queue `qwenwork_work` with kinds `chat` → `brief` → `compose`, one session per inquiry (`qwenwork_inquiry_sessions`), session creation and turn following.
- `qwenwork_commercial.py`: the `brief` prompt inlines the intake and scoping skills from `~/.qwenwork/skills`, then `inquiry_quote.price()`. The `compose` prompt inlines the negotiator skill, then `commerce.save_page()` and `core.check_draft()`.
- Current deliberate blocks to lift: `reference_runtime_disabled` in `qwenwork_runtime.prompt/apply`, `comparable_ids = []` and "comparable_ids must be empty" in `qwenwork_commercial`, and `unverified_portfolio_selection` when `page.portfolio` is non-empty.
- Reference media: `conversation.py` (`reference_assets`, `reference_runs`, `reference_observations`, `media_notifications`, `pending()` returns `media_updates`). Files live in `bidpilot.db.media/`.
- Portfolio: `portfolio.py` (`portfolio_assets`, `public_asset`, `search`). Past projects: `bidpilot-engine/data/past_projects.csv`.

## Verified facts about the API (tested live on 2026-11-22)

- `POST /v1/sessions` takes only `initial_prompt.text`, `name`, `mode`, `locale`, `timezone`, `ttl_seconds`, `multi_agent`, `parent_session_id`. You can't pick skills or connectors, so the BidPilot MCP tools are NOT available in the cloud.
- A cloud session (mode `flash`) has bash, webfetch, the `skill` tool, sub-agents, and the `qwenwork` CLI. It can run `qwenwork drive ls/search/download`, and it analysed a Drive video for real (media-read skill / Qwen omni model + OpenCV). It also chose comparables from a Drive CSV on its own.
- Drive upload: `POST /v1/drive/direct-uploads` (header `Idempotency-Key: <uuid>`, body `{path,size,mime_type}`), then `PUT` the bytes to `upload.url` with **exactly** the returned `upload.headers`, then `POST /v1/drive/direct-uploads/{session_id}/complete`. Paths can't overwrite an existing file, so use unique paths. Python `urllib` with the default SSL context fails certificate verification on this Mac. Use the same HTTP mechanism `qwenwork_api.Client` already uses.

## Changes

1. **Drive helper** (`qwenwork_api.py`): `Client.drive_upload(local_path, drive_path, mime)` returns `file_id`. It must be idempotent per (local file sha256, drive path). Record uploads in a new table `qwenwork_drive_files(sha256, drive_path, file_id, uploaded_at)` and skip re-uploads.
2. **Client-safe portfolio sync**: on worker start, and whenever portfolio data changes, write `portfolio.json` and upload it to `/bidpilot/portfolio/<sha8>/portfolio.json`, plus the sample clips it references. It holds project/asset id, title, format, production mode, shoot days, deliverables, captions, style tags, description and clip Drive path. It must contain **no** fees, rates, hours, floors, capacity, or other clients' private notes. Add a test that fails if any such key or value appears.
3. **Reference video**: when an inquiry has a reference asset, upload it to `/bidpilot/inquiries/<inquiry_id>/<asset_id>.<ext>` and stop raising `reference_runtime_disabled`. The `chat` prompt tells the agent the Drive path and asks for observations with `start`/`end` seconds and client-safe `production_cues`. In `apply`, save them through the existing conversation functions (`commit` with `media_analysis_id` and `production_cues`, and record observations so they link to the analysis). Validate timestamps against the clip duration, and limit cue count and length to the existing schema limits.
4. **Agent-chosen comparables**: in the `brief` prompt, give the Drive path of `portfolio.json` and tell the agent to open it itself and return `comparable_ids` (max 6), each with a one-line why/how-it-differs, plus one `similar_work_asset_id`. In `apply`, accept them only if every ID exists in the local data, then pass `comparable_ids` to `inquiry_quote.price()`. If any ID is unknown, drop it and record the drop. Never trust an ID the agent invents. Prices still come only from the engine.
5. **Similar-work clip on the draft**: in `compose`, allow `page.portfolio` only when it contains IDs from the validated set saved in step 4, and render them with the existing portfolio media. Keep `unverified_portfolio_selection` for anything else.
6. **Visibility**: name sessions `BidPilot · <client or inquiry short id>` so they're recognisable in the QwenWork app. Write each stage (chat, reference, intake/scoping, engine, compose, outbound check) to the existing run steps / agent timeline with `trigger="api"`, so the founder dashboard timeline shows it.
7. **Switch it on**: in `deploy/run_server.sh` export `BIDPILOT_QWENWORK_EVENT_RUNTIME=1`. If the OAuth token is missing, the server must keep running and the dashboard must say "QwenWork not connected".

## Safety rules (keep them all)

- Treat buyer text, video content and agent output as untrusted data. The agent never receives private estimate numbers or other clients' data, and nothing it returns can approve, publish, book or move money.
- All arithmetic stays in the engine. `check_draft` must still block private numbers.
- Idempotent at every step. Use stable request ids, and never blindly re-dispatch a turn whose outcome is unknown (keep the existing `dispatch_outcome_unknown` behaviour).

## Tests and proof

- Add unit tests with a fake `Client` for: Drive upload idempotency, the portfolio file having no private fields, the reference path from upload to saved cues, rejection of comparable validation for unknown IDs, a page portfolio limited to the validated set, and the runtime off when the env var is unset.
- Run the full test suite (`python3 -m pytest -q` in `BidPilot/`) plus the engine tests, and report the real counts.
- Then do **one live run**: restart with `launchctl kickstart -k gui/$(id -u)/com.bidpilot.server`, open `http://127.0.0.1:8765/inquire`, and send a café brief with `BidPilot/stock/espresso.mp4` attached. Show: the new session in QwenWork (`GET /v1/sessions`), the agent's reference cues, the comparables and similar-work clip it chose, the engine verdict, and the draft that passed `check_draft` waiting in the founder dashboard. Approve nothing.
- Commit on `qwenwork-openapi` with a clear message. Don't push unless asked.

Report: what changed (files), test counts, the live run's session id and results, and anything you couldn't do.
