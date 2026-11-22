# BidPilot full implementation plan

**Status — proposed design.** The target is a solo producer quoting real client work from a brief, one or more reference videos, and their own production history. The benchmark for depth is whether a producer can inspect, correct and defend every material assumption in a quote. A feature-level comparison with Gravity requires the specific product or demo.

## What the producer and client experience

A café wants three launch reels. It supplies one reference for editing pace and another for lighting. The producer sees a timeline for each reference, the passages the client wants to match, and a production plan that groups work across all three deliverables. Each significant estimate line opens its supporting scope facts, rate source and any outstanding decision.

The client opens a private chat with the studio’s AI assistant. They can start with their own words, ask for an explanation, attach a reference, point to a moment in the player or change their mind at any time. A compact project brief beside the chat shows requested deliverables, selected reference moments, supplied assets and open decisions. On mobile the brief opens from the conversation. After founder approval, the client sees the commercial options. A new request after approval opens a change round. The producer sees the delta in work, costs, dates and assumptions before releasing revised terms.

## From the Hashmeta website to the approved proposal

1. **Start on Alex Studio’s website.** “Request a quote” opens a branded chat with the QwenWork intake agent. A visitor can begin with a message or reference video. BidPilot creates the private inquiry on first submission and returns a resume link. The public website uses a generic chat entry URL, so each visitor starts their own inquiry.
2. **Build the brief together.** The client describes what they want. The intake agent answers questions, asks its own follow-ups automatically and updates the visible brief. For example, “one main reel and two shorter cuts” becomes one master and two derivatives. Reference moments, corrections and supplied footage stay attached to the inquiry.
3. **Review the proposal.** Once the brief is ready, BidPilot checks scope, cost and capacity and presents the options to Alex. After Alex approves a version, the conversation offers “View proposal” for the current client proposal at `/p/<token>`. The client can select an option or request a change on that deal. Reopening the inquiry restores the conversation and its current proposal status.

**Where the chat runs.** BidPilot serves the conversation, files and proposal. First test a BidPilot-hosted embed on the actual Hashmeta site using a sample inquiry on desktop and mobile. Verify message delivery, video upload, resume access and opening the proposal. If embedding is unavailable or unreliable, the same “Request a quote” action opens the branded BidPilot chat as a full page. An existing conversation can open full-page using its private resume link. Both layouts use the same backend and transcript.

## Ownership and integration

- **Hashmeta** hosts the studio website and its chat entry point. BidPilot records the website as the referral source. If a verified CRM integration supplies a stable external lead ID, associate it with the existing inquiry to avoid creating a second deal. For external CRM leads, deliver the link through an authorized studio channel once the contact mapping is verified.
- **QwenWork** runs intake, reference interpretation, scoping, clarification, comparison with past work and negotiation as skills. The run skill chooses the next action from persisted deal state. QwenWork can hold an open-ended, two-way scoping conversation directly with the buyer without per-message founder review. The buyer can confirm what they want. Only Alex approves the studio’s final price, deliverables and delivery terms, including any revisions, and confirms the booking after buyer selection.
- **Media worker** inspects media in a background process, outside the HTTP request and MCP call. Start with FFmpeg/ffprobe plus a cut detector. Add timestamped speech transcription, on-screen text recognition and a vision model behind replaceable adapters. Preserve intermediate outputs and provider versions. A media failure leaves text intake usable.
- **Python service and MCP server** own authorization, typed writes, evidence storage, work queues, estimate arithmetic, schedule checks and release. The server validates state transitions for every caller. No pricing or workflow decision depends on a model remembering a prior conversation.

## Evidence from reference to brief

1. Accept a private upload or an explicitly supported import. Issue an opaque asset ID, validate the decoded media, hash the file and create a proxy for playback. Store binaries outside SQLite. Scope the cache key by studio, content hash, analysis settings and pipeline version. Cap file size, decoded duration and processing time.
2. Extract shot boundaries and representative frames, including frames inside long shots. Align speech, OCR and audio segments on the original media timebase. Keep overlapping tracks so a voice-over spanning four visual cuts remains one passage. Preserve a thumbnail and start/end timestamps for every material observation.
3. QwenWork labels production cues such as dialogue, product inserts, appearing talent, caption style, motion graphics, apparent settings, reusable shots and client-supplied footage. Each observation carries its source, method, timestamp and uncertainty. Camera and lighting methods inferred from pixels are suggestions. Where a frame cannot resolve a material distinction, record the alternatives.
4. Ask which elements the client wants from each reference. Link selected segments to a deliverable and dimension such as pace, framing, sound or graphic treatment. Mark excluded elements explicitly. A client correction supersedes an earlier interpretation and records why.
5. Group confirmed requirements into candidate setups and work items. Give the producer controls to merge or split setups, mark supplied footage, replace an assumption, and compare the resulting estimate. A group of shots becomes a shared setup only when production constraints allow it.

**Selection of the media stack.** Evaluate [video-tools-mcp](https://github.com/video-context/video-tools-mcp) for local shot times and frames, [PySceneDetect](https://github.com/Breakthrough/PySceneDetect) for a Python worker, and the [Qwen video cookbook](https://github.com/QwenLM/Qwen3-VL/blob/main/cookbooks/video_understanding.ipynb) for timestamped visual interpretation. They are alternatives or components. Test one authorized fast-cut promo, one talking-head video and one graphics-heavy clip before committing to an adapter. Verify frame access in the actual QwenWork runtime. If it only receives text, expose timestamped observations over MCP and retain the media preview in the founder UI.

## Persistent records and contracts

Keep existing deals, offers, evaluations, options, proposal versions, approvals and reservations. Add the following records with studio ownership and stable IDs. A scope revision is the unit of concurrency. An evaluation identifies the exact revision it prices.

| Proposed records | Contents and invariants |
| --- | --- |
| Inquiry access, scope revisions | A hashed client token, inbox identity, deal link once claimed, prior revision, normalized brief and confirmed facts. Create access when the client submits their first message, reference or existing intake form, even if QwenWork has yet to claim the lead. |
| Reference assets, analysis runs, observations | Asset and analysis versions, time spans, transcript/OCR excerpts, frame references, correction history and links to proposed scope facts. Analysis of a replacement reference cannot silently change an approved scope. |
| Deliverables, setups, work items and dependencies | Each output has format, duration, aspect ratio, language and usage. Each task has an owner, unit, quantity, dependencies and links to the outputs it serves. A shared shoot or master edit has one cost-bearing task. |
| Conversation turns and scope facts | Persist ordered messages, sender, request ID, references and timestamps. Each fact links to its originating message or reference segment and carries a subject, value, status and supersession history. Track open decisions independently so one message can resolve several or introduce a new one. |
| Rate versions, estimate lines and actuals | Persist quantities, effort basis, labour cost rates, expenses, commercial rates, currency, uncertainty and assumptions. Record actual hours and costs at the finest level the producer can verify. |
| Work events and delivery outbox | Persist event ID, scope revision, work type, retry key, lease expiry and processing result. Save each incoming message and its processing event in one transaction. Delivery failure can be retried independently. |

Proposed MCP calls include `start_reference_analysis(asset_id)`, `get_reference_evidence(analysis_id)`, `get_conversation_context(inquiry_id, through_sequence)`, `commit_chat_turn(inquiry_id, through_sequence, expected_revision, fact_changes, reply, request_id)`, and `estimate_scope(deal_id, scope_revision_id)`. Each write returns the resulting IDs and revision, or a stale-revision error that requires a reread. Keep the existing safe negotiation payload and proposal release endpoints. Wrap legacy `claim_intake` for supported offers. Persist the supplied and confirmed values before handing an intake to another run.

## Conversational intake and scoping

Serve the conversation at `/i/<token>`. The client can send a message before the assistant asks anything. The composer accepts free text and optional attachments, with a “Refer to this moment” action capturing the asset and time range from the video player. Suggested replies are shortcuts. On return, restore the transcript, latest brief and any outstanding follow-up.

A turn through the service works as follows.

1. `POST /i/<token>/messages` accepts a client-generated message ID, text and optional authorized asset/segment IDs. Bind it to the inquiry, append it with a monotonic sequence and enqueue processing atomically. A retry with the same payload returns the original receipt. Messages arriving while analysis is running remain visible immediately.
2. A worker wakes QwenWork for that inquiry. The intake skill reads recent turns, the structured brief, open decisions and any cited observations. It handles multiple intentions in one turn. For instance, “Make it two reels. We have the drink footage. What’s a cutdown?” updates two facts and calls for an explanation. Keep discussion of hypothetical alternatives separate from requested scope changes.
3. QwenWork returns a client-safe conversational reply and zero or more typed fact changes with evidence. It can answer directly, summarize a correction, request a reference or ask a focused follow-up when the answer affects the work. If “make the second one shorter” could refer to two videos, ask which deliverable the client means before changing its duration. Send producer judgments to the founder queue and use approved option data when discussing commercial commitments.
4. `commit_chat_turn` validates field types, source permissions and reply visibility, then writes the response, scope revision if changed, and processed sequence in one transaction. If a newer client message or conflicting revision arrived, retain all user messages and regenerate from the latest state. A retry cannot create a duplicate agent reply. Push committed updates via server-sent events or cursor-based polling. A connection loss can resume from the last seen sequence.
5. Show material changes in the live brief. The client can correct facts conversationally or mark the brief ready for a quote. QwenWork resumes the estimate path when required inputs are resolved. A correction to an already priced scope produces a new revision. An amendment to a published proposal goes through the existing change and founder approval workflow.

A responsive chat requires prompt turn dispatch. Verify an authorized low-latency trigger or an active QwenWork consumer that can claim pending turns. Measure send-to-reply latency in the actual environment. The scheduled lead poller can recover missed work. If the deployment supports scheduled processing only, acknowledge receipt immediately and show the real pending state so the client can leave and return. Keep long video-analysis jobs asynchronous and continue discussing the rest of the brief while they run.

## Estimation, scheduling and commercial options

Introduce a separately versioned detailed estimator behind the existing evaluation boundary. The existing model is a reproducible baseline for supported promos. Use one estimator per estimate line or package and record its basis. Combining a full per-video baseline with all its component tasks would charge editing twice.

| Unit of work | Example and pricing basis |
| --- | --- |
| Per project | Creative development, briefing, scheduling and script planning. |
| Per setup, session or shoot day | Call time, lighting, sound, crew, kit, travel and location hire. Model minimum call periods, movement between locations and setup reuse. |
| Per master deliverable | Footage selection, assembly, colour, audio mix, graphics and agreed review rounds. |
| Per derivative | Reframe an approved master, localize text, make captions or replace an end card. Charge incremental work after the master exists. |
| Direct costs and usage | Talent, permits, specialist quotes, licensed assets, rental and usage period. Record who supplies or pays and whether an expense is provisional. |

For each work item, obtain a quantity and a unit-effort basis from the studio rate catalog, matched historical tasks, or a dated producer override. Sum labour by role. Internal production cost is labour at cost rates plus direct expenses. Apply the studio’s own floor and separately configured selling rates, licensing and discount policy to generate the client price. The existing S$70 minimum hourly rate represents a commercial floor. A separate cost rate is required to report true margin. Store money as minor units in the detailed estimator and round at defined line and total boundaries. Preserve the legacy engine’s pinned rounding when replaying older estimates.

Where uncertainty matters, save low/base/high quantities or hours and the assumption responsible. Model shared uncertainties once across linked work items. A possible second shoot day affects all deliverables on that shoot. Label scenario ranges as scenarios until historical coverage supports probability claims. Require a vendor or producer estimate for unsupported custom animation or specialist work.

Upgrade scheduling from aggregate weekly hours to tasks with release dates, dependencies, review windows and required resources. A shoot needs a contiguous slot and the necessary people and location. An edit can be split across available blocks. Check both the delivery date and resource feasibility. Retain the weekly ledger for rough planning. Recheck against the current resource calendar when the founder confirms a booking, and reserve atomically.

Generate alternatives by changing allowed scope variables and rerunning the same engine. Examples include combining shoots, reducing masters, deriving more cutdowns, using supplied footage or phasing delivery. Show the exact work, costs and milestones that changed. Bind the founder’s approval to the immutable scope revision, rate version, estimate and public option terms. A subsequent client change forks a revision and reuses unaffected media and tasks.

## Calibration and evidence of accuracy

### Worked pricing fixture for the detailed estimator

Use a synthetic café case to test arithmetic before calibrating task times. The client confirms three distinct masters, one shared shoot, one shared caption design, captions on all three and two included rounds of feedback. Assume a single producer/editor. The sample below uses invented unit times and costs for testing, separate from the pinned demo rate card.

| Work | Quantity × hours | Base hours |
| --- | --- | ---: |
| Brief, scripts and shoot plan | 1 project × 3 | 3 |
| Set up and film | 1 session × 5 | 5 |
| Ingest and select shared takes | 1 batch × 2 | 2 |
| Edit distinct masters | 3 masters × 4 | 12 |
| Design one caption treatment | 1 treatment × 1 | 1 |
| Time and check captions | 3 versions × 0.5 | 1.5 |
| Included review and finishing allowance | 1 batch × 3 | 3 |
| **Total** | | **27.5** |

With an illustrative 20% planning allowance, the plan is 33 hours. At an assumed internal labour cost of S$45/hour and S$120 of direct costs, the planned production cost is S$1,605. At the demo commercial minimum of S$70/hour, the corresponding floor is S$2,430. Selling price follows the studio’s approved package or rate policy. If the client adds one captioned cutdown using an existing master, the test should add only the confirmed adaptation, caption adjustment and review work. If the client asks for a new rooftop scene, it should reopen location, permission, travel and shoot-slot questions before repricing.

### Validation on completed jobs

Start with hand-labelled completed jobs for which the creator can provide the original brief, reference, awarded scope, hours, vendor spend and change orders. Avoid training on the final video or post-booking facts that were unavailable at quote time. Existing seed records have project-level actual hours and a sale price. Neither task-level labour nor true expense can be recovered from those fields.

Compare three runs on the same held-out jobs: current brief-only engine, detailed estimator using the brief alone, and detailed estimator with reference evidence plus answers available by quote time. Split by project and time, keeping variations from a single campaign together. Report median absolute hours error, SGD cost error where actual costs exist, share of jobs underquoted, worst underestimates, question count and creator correction time. Break down results by edit-only, on-location and graphics-heavy jobs. For the media system, score missed cost-driving elements, mistaken setup merges and unsupported assertions. Evaluate actual job scope against the scope that was originally quoted and account for approved changes separately.

Capture actuals after the job and ask the producer to classify major differences, such as bad estimation, scope change, late assets or an unexpected reshoot. Suggest rate-card updates in a private review. Apply accepted changes to future revisions and retain prior rates for audit and replay.

## Delivery plan and acceptance criteria

| Milestone | Implementation | Exit test |
| --- | --- | --- |
| 1. Baseline and data contract | Record the current quote flow and tests. Define the evidence, scope and task schemas. Assemble consented historical cases and a brief-only baseline. | Reproduce current sample proposals. Replay one historical job with the information available at quote time. |
| 2. Inquiry conversation | Connect Hashmeta’s “Request a quote” action to the branded chat. Test embed support and provide full-page access. Extend the inbox, DB and MCP queue. Add scoped chat access, transcript, live brief, turn dispatch, revision checks and worker leases. Update QwenWork intake/run/runtime skills. | From the signed-out Hashmeta site, start and resume a private chat on desktop and mobile, verify full-page access, correct the brief and cite a video moment. Verify brief updates, reconnect, rapid follow-on messages, duplicate retries and measured reply latency. |
| 3. Reference analysis | Build upload/import, media jobs and evidence adapters. Add synchronized player, shot list, transcript and source-linked corrections. | A reviewer jumps from a cue to its frame, corrects it and resumes the run. Unsupported media or a provider timeout still allows a text quote. |
| 4. Production scoping | Extend QwenWork scoping with deliverables, shared setups, tasks and dependencies. Add producer overrides and automatic question prioritization. | Three reels sharing one shoot yield one shoot task. Supplied clips, a newly filmed intro and a shared caption style map to the correct work. |
| 5. Detailed cost and schedule | Add estimator v2, rate versions, vendor costs, scenarios, constrained schedule and option search. Keep pricing changes synchronized in both engine directories. | Sum line items independently. Verify shared work, a deadline blocked by a missing slot, and the incremental cost of a changed delivery. Replay legacy fixtures. |
| 6. Commercial lifecycle | Attach approved scope and assumptions to proposals. Update offer changes, option comparisons, hold expiry, booking and release projections. | After Alex approves, the client can open the current proposal from the same inquiry. A stale client link cannot accept a superseded price. Competing bookings cannot reserve the same resource. A free-form scoping exchange updates the requested brief without founder intervention. A buyer asking the agent to agree to a price or promise delivery leaves the proposal and booking pending Alex’s decision. |
| 7. Pilot and full release | Add actuals capture, backtesting, studio onboarding, scoped credentials, retention, operations metrics, backup/restore and channel delivery. Verify live QwenWork trigger and tenant isolation before real leads. | Shadow estimates on real completed jobs, review underquotes by cause, then pilot with one studio. Set rollout thresholds with the studio from the baseline and its tolerated loss. |

Milestones 2 and 3 can proceed in parallel once the records in milestone 1 are agreed. Milestone 4 integrates them. Work on costing can start on hand-authored scopes while video integration is underway. Start collecting historical actuals immediately because they determine when accuracy claims can be supported. The 23 November hackathon can demonstrate the reference, question, answer and repricing path. Detailed costing, resource scheduling and accuracy validation each have their own acceptance criteria.

## Operations and production deployment

Use the current Python/SQLite implementation for a single-studio trial. Put private media in object storage and run bounded media jobs separately from the web service. Multi-studio deployment needs studio-scoped authorization, a transactional database with concurrency controls, a durable job queue and per-studio credentials. Store tenant IDs on all business records and check them on every worker, MCP and HTTP access. Resolve reference IDs to controlled assets. Fetch external media only through supported importers with bounded downloads. Expire and revoke inquiry links, redact tokens from logs, and delete media and derived frames according to the studio retention policy.

Track queue age, analysis time and cost per reference, failed or partial analysis, outstanding client questions, duplicate delivery attempts, manual scope overrides, estimate error and booking conflicts. Persist provider/model and prompt versions so differences can be reproduced. Run tests for partial writes, duplicate events, out-of-order replies, cross-studio reads, private data in public projections and stale approval. Roll out automatic clarification before automatic use of new estimate lines. Expose each detailed estimate in founder review during the pilot.

## Repository entry points

- [HTTP intake, client routes and dashboard](../BidPilot/server.py)
- [SQLite schema and migrations](../BidPilot/db.py)
- [Intake resumption, evaluation, release and booking](../BidPilot/core.py)
- [QwenWork tool registration and pending work](../BidPilot/mcp_server.py)
- [Capacity ledger](../BidPilot/capacity.py)
- [Runtime skill and tool mapping](../bidpilot-build-guide/bidpilot-runtime/SKILL.md)
- [Commitment Engine and fixed demo specification](../bidpilot-build-guide/BUILD_GUIDE.md)
