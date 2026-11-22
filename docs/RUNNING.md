# Run BidPilot locally

## Approve and send configuration

The dashboard's Approve and send action uses `/api/approve-and-send`. It requires a completed QwenWork composition, passing check, confirmed private recipient, exact reviewed options/digest and current scope/capacity. One transaction publishes that version and inserts a unique `proposal_outbox` row. The saved page is never regenerated after approval. Existing `/api/approve` is publication-only for shipped API clients; it does not send email.

Configure these environment variables on the service process, never in checked-in secrets:

- `BIDPILOT_PUBLIC_URL`: publicly reachable HTTPS origin, e.g. `https://proposals.your-studio.example`. No paths, query, credentials, localhost or private IPs. Configure DNS, TLS and proxy routing to `/p/<private-token>` yourself; syntax validation does not prove internet reachability.
- `BIDPILOT_MAIL_FROM`: studio sender mailbox authorized by the provider.
- `BIDPILOT_SMTP_HOST`, `BIDPILOT_SMTP_USER`, `BIDPILOT_SMTP_PASSWORD`: provider SMTP settings/credentials.
- `BIDPILOT_SMTP_PORT`: defaults to `587`.
- `BIDPILOT_SMTP_SECURITY`: `starttls` (default) or `ssl`. Plaintext SMTP is not supported.

Owner state reports composition readiness, recipient confirmation, configuration and separate email delivery status. Missing settings produce a durable failed delivery, not a claim of sending. Retry uses `/api/retry-email` on the same approved version and pinned recipient/link. Provider acceptance is `accepted`, not inbox delivery. A known SMTP rejection is `failed` and retryable. Network/interruption outcome is `unknown` (or `sending` after a process crash); reconcile its stable Message-ID with the provider before manually resolving status. Never blind-resend uncertain SMTP outcomes. Concurrent clicks cannot create another publication/outbox; SMTP itself offers no exactly-once guarantee after an ambiguous disconnect.

Queued jobs left by a crash before SMTP starts can be drained by an operator calling `delivery.deliver_once` against the configured service database; do not retry `sending`/`unknown`. Keep DB/outbox private because it contains recipient addresses and buyer capability URLs. Use provider sandbox accounts for acceptance; the automated suite mocks SMTP and must never use a real lead.

Real local transport acceptance: from `BidPilot/`, run `../.venv/bin/python -m unittest -v test_local_smtp_workflow.LocalSMTPWorkflow`. This starts an ephemeral loopback HTTP server and TLS socketserver SMTP catcher, uses real `smtplib.SMTP_SSL` authentication/DATA, and captures only synthetic mail. Its self-signed TLS certificate is generated in a temporary directory. No external provider or public HTTPS connectivity is tested. Set `BIDPILOT_SMTP_EVIDENCE` to an output directory to export synthetic emails, buyer/preview HTML and a capability-free JSON report; no database or owner token is exported. Composition uses deterministic MCP calls, not a live QwenWork connector.

Executable regression: `../.venv/bin/python -m unittest -v test_proposal_delivery.ProposalDeliveryTests` from `BidPilot/`. It starts with an attachment-only private inquiry, uploads the real sample clip, prepares through MCP, supplies email later and corrects it, captures the mocked SMTP EmailMessage and opens its private link through the HTTP handler. This is deterministic contract testing, not an actual QwenWork buyer run or proof of reference visual matching.

Run these commands from the repository root with Python 3.11 or later.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r BidPilot/requirements-media.txt
.venv/bin/python -B BidPilot/server.py
```

Open the owner URL printed in the terminal to use the founder dashboard. Keep that URL private. The default port is `8765` and the database is `BidPilot/bidpilot.db`. Override either with `--port` or `--db`.

Open [Start an inquiry](http://127.0.0.1:8765/inquire) in a second browser as the buyer. Describe a project, open its private conversation and upload a reference. For a reproducible café example use `BidPilot/sample-portfolio/cafe.mp4`. The local worker decodes the video, stores timestamped frames and exposes the analysis to QwenWork. The buyer can play the clip and attach a moment to a message. Preserve the inquiry URL to return to the same brief. The original [structured form](http://127.0.0.1:8765/quote) is also available.

In QwenWork, import and enable both stdio configurations, `qwenwork-mcp.json` and `qwenwork-bidpilot-mcp.json`. Adjust the paths for your checkout and keep `BIDPILOT_MCP_DB` pointed at the dashboard's database. Start a fresh QwenWork task after enabling the connectors. See [the MCP setup](../QWENWORK_MCP.md).

After upgrading an existing installation, open **Skills & extensions → Connectors → Mine** and inspect **bidpilot**. The current catalog includes `get_conversation_context`, `get_reference_frame` and `commit_chat_turn`. If it still lists only the original 12 tools, switch **bidpilot** off and back on, verify those three names appear, then start a new QwenWork task.

Give the QwenWork task the current [runtime skill](../bidpilot-build-guide/bidpilot-runtime/SKILL.md) and run it against pending work. QwenWork reads the conversation, interprets reference frames, records scope facts and prepares the proposal. Pending messages remain visible while the runner is offline. A continuously running deployment can wake its configured runner through the [operations worker](OPERATIONS.md).

## Studio Assistant settings

`BIDPILOT_STUDIO_ASSISTANT=gemini|qwenwork` defaults to `gemini`, even when the key is missing. Set `GEMINI_API_KEY` only in the server process environment, never in browser code, URLs, checked-in files or MCP configuration. `BIDPILOT_GEMINI_MODEL` defaults to `gemini-3.1-flash-lite` (live verified). The former `gemini-flash-latest` alias timed out during local verification. An explicit environment or `.env` model setting still takes precedence. On macOS the transport adds the trusted system CA bundle without disabling certificate or hostname verification. Contract-invalid output receives one bounded model repair attempt; failed repair preserves the buyer message for retry. Gemini uses the official generateContent HTTPS endpoint with a 25-second timeout and JSON output. No scheduler or model polling is used for buyer messages: POST `/inquire`, `/i/<token>/messages` and `/i/<token>/retry` perform one synchronous reply attempt and return its status. A missing key, network failure or rejected output leaves the buyer message saved and shows Retry assistant. Retry does not add another buyer message. Reload restores the persisted failure notice. Reference decoding runs locally. An open inquiry checks for completed analysis and requests one grounded Gemini reply. A failed reply can be retried on the same inquiry.

Gemini reloads the buyer-facing sections of the intake, scoping and runtime skills and checks that the two production question banks match on each request. It uses the inquiry's transcript, sourced facts, corrections, recipient and selected reference moments. It can save grounded production cues and look up cleared portfolio examples, including a representative frame. The proposal page's portfolio-search queue still belongs to QwenWork. For a declined submitted brief, Gemini can ask one question about flexibility and carry the buyer's answer into a revised brief. Gemini sees only the constraint category for this question. QwenWork retains private comparisons, pricing, proposal composition and commercial orchestration. Alex approves commercial terms and bookings.

Put your key in `GEMINI_API_KEY` in the repository-root `.env` file. Use `.env.example` as the template for a fresh checkout. The server loads this file automatically at startup, including when launched from another directory. Existing environment variables take precedence. Install the dependencies with `.venv/bin/python -m pip install -r BidPilot/requirements-media.txt`, then run `.venv/bin/python -B BidPilot/server.py` from the repository root. To use the legacy Studio Assistant, run `BIDPILOT_STUDIO_ASSISTANT=qwenwork .venv/bin/python -B BidPilot/server.py`. Restart the existing service when changing its configuration.

**Send my brief** posts the displayed revision/processed sequence to `/i/<token>/submit`. It saves an immutable snapshot and returns a receipt ID; repeat submission of that revision/sequence returns the same receipt. Pending/unanswered turns or reference work block a new submission. `list_pending_work` exposes current unpriced receipts as `studio_briefs` with `inquiry_id`, `expected_revision`, `sequence`, `receipt_id` and `snapshot_json`. QwenWork reads that snapshot/context and uses existing `price_inquiry_scope`, estimate, composition and founder-review tools. It must not claim or reply to Gemini-owned conversation turns. Corrections require a new submitted revision; obsolete receipts remain historical but disappear from pending work. Pricing already bound to a revision remains idempotent; stale scope cannot be released, and stale callbacks are held rather than rewriting it. A receipt confirms storage for review, not a quote, email delivery or booking.

Switch by setting `BIDPILOT_STUDIO_ASSISTANT=qwenwork` on the server and restarting it. Provider routing reconciles on the next authenticated buyer state/action. An active Gemini lease or QwenWork conversation lease retains ownership until completion/expiry; it cannot be stolen by switching. Gemini commits require the exact unexpired lease. Gemini-owned turns are excluded from the QwenWork conversation queue and reject legacy claims/commits. There is no silent fallback to QwenWork on Gemini failure. Existing MCP tools, legacy callback and the QwenWork conversation runner remain available in QwenWork mode. Configure callbacks separately as before.

External ground-truth verifier caveat: the unmodified `verify_qwenwork_regressions.py` supplies a raw legacy fixture without `pricing_mode=legacy_replay`. Current production intake correctly pauses for a missing dated producer template, so it creates zero options and the verifier reports `original_options=0` even though `option_set_errors` rejects the empty option set. This is an outdated setup assumption, not permission to enable legacy fallback. Keep the external script unmodified; exercise deleted-option protection with explicit legacy replay (existing application regressions) and the engine's 13 pinned fixtures separately.

Synthetic verification: run `python -m unittest -v test_studio_assistant` from `BidPilot/`; these use ephemeral HTTP/SQLite and mocked Gemini, not a real API key or live buyer.

## Automatic conversation replies (QwenWork mode)

Two authorized pieces make the website chat agentic without moving agent logic into Python:

1. **Scheduled QwenWork consumer.** A QwenWork scheduled task runs the bidpilot-run/bidpilot-runtime conversation order against `list_pending_work`: claim each inquiry (`claim_conversation_work`), read context and reference evidence, inspect frames, commit one grounded reply (`commit_chat_turn`), and price/compose a proposal when the brief is sufficient — stopping at founder review. Create it paused, verify one manual cycle, then enable it. Replies appear in the buyer's private thread; nothing is released or booked automatically.
2. **Operations worker + shipped adapter.** For durable wakes, ordering and verification, run:

```sh
.venv/bin/python -B BidPilot/scripts/operations_worker.py \
  --root /private/bidpilot-runtime --studio STUDIO_UUID \
  --command-json '["/usr/bin/python3","/checkout/BidPilot/scripts/qwenwork_adapter.py","--app-db","/private/bidpilot-runtime/studios/STUDIO_UUID/app.db"]'
```

The adapter verifies completion against the studio app database; it never writes replies itself. See [operations](OPERATIONS.md) for lanes, leases, dead letters and the attempt budget. For the single-database hackathon demo the scheduled consumer alone is sufficient; the worker adds production-grade durability when a studio runtime root is onboarded.

From the founder dashboard, open **Portfolio library** to link shareable work to past projects. Review the estimate and its booking impact before approving the proposal. The buyer can then open the released website, explore portfolio work and select an option. Confirm the booking as Alex, then use the same client page to request additional work. Closeout hours and reliable completed jobs feed future estimates. The [detailed estimator guide](DETAILED_ESTIMATE.md) covers dated work, resource schedules, rates and calibration.

The bundled FFmpeg decoder handles the sample videos. The worker uses Tesseract when available and can use Vision on macOS. Set `BIDPILOT_TESSERACT` to select a specific Tesseract executable. For speech recognition, set `BIDPILOT_WHISPER_CLI` and `BIDPILOT_WHISPER_MODEL` to a local whisper.cpp executable and model. The server also discovers `bin/whisper-cli` and `share/bidpilot/ggml-tiny.en.bin` inside its virtual environment. The demo installation uses the English tiny model. Inspect the provider status on the analysis run to see which evidence was recovered.

## Connect a hosted studio site

Point the Hashmeta “Request a quote” button at the buyer service's HTTPS `/inquire` URL. To embed the same page, supply the studio site's HTTPS origin with `--embed-origin`. Start BidPilot behind a TLS reverse proxy with `--public-origin https://brief.example.com --embed-origin https://studio.example.com`, replacing both example origins. Publish only `/inquire`, `/i/*`, `/p/*`, `/portfolio/media/*` and optionally `/quote`. Configure the proxy to set the upstream Host to the buyer origin's hostname and port. The service still listens on loopback. Alex uses the local owner URL.

To clear the demo, enter `RESET SAMPLE DATA` in the founder dashboard's **Reset sample data** form. This clears sample records in SQLite and reloads the five seed tables. The public quote form and all workflows are for synthetic data.

## Verify the demo

```sh
.venv/bin/python -m pip install playwright
.venv/bin/python -m playwright install chromium
(cd BidPilot/bidpilot-engine && ../../.venv/bin/python -B -m unittest -v test_engine.py)
(cd BidPilot && ../.venv/bin/python -B -m unittest discover -p 'test_*.py')
.venv/bin/python -B BidPilot/scripts/demo_smoke.py
```

The smoke script replays synthetic agent outputs against a temporary database. QwenWork still runs intake extraction, comparable selection and reasoning, draft writing, and workflow orchestration. Its demo setup also covers the scheduled task, Hashmeta connector, website and CMS case study, and recording the demo video. Configure and verify those separately in the sample workspace.
