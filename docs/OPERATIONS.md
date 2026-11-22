# BidPilot local operations

`BidPilot/operations.py` is the deployment boundary for a single-studio pilot.
It keeps each studio in its own private directory with a separate application
database, operations queue, media tree, and credential directory. It does not
put agent behavior in Python: QwenWork remains the intake/scoping/negotiation
agent through the existing scoped MCP tools.

## Onboard and isolate a studio

Run onboarding from an operator-only process. Do not accept the runtime root,
studio ID, credential names, or credential contents from a public request.

```python
from operations import RetentionPolicy, StudioRegistry

registry = StudioRegistry("/private/bidpilot-runtime")
paths = registry.onboard(
    "example-studio",
    RetentionPolicy(media_days=30, inquiry_link_days=30),
    {"worker-token": b"explicitly-provisioned-secret"},
)
```

The returned `paths.app_db` is the database path for this studio's web and MCP
processes. The credential files are mode `0600`; their directory and workspace
are mode `0700`. The registry resolves a studio before a process opens state.
Do not select a database from an HTTP header or caller-provided filesystem path.

## Durable QwenWork turn dispatch

The worker recovery-scans the app's durable work tables before every dispatch.
It projects queued `inquiry_jobs`, queued `media_notifications`, inbound
`commerce_reference_requests`, and pending `portfolio_search_requests` into
stable, idempotent wake events. This makes
the app database the source of truth and closes the crash window between an app
commit and a wake. Repeated scans do not duplicate work.

Only routing identifiers cross into the operations queue: inquiry and sequence;
inquiry, analysis ID and terminal status; or deal, proposal version and request
ID. Message bodies, filenames, media bytes, observations, option terms and
creative instructions and portfolio search text stay in `app.db`. The
authorized adapter retrieves the
actual pending records through the studio-scoped MCP tools.

Start `scripts/operations_worker.py` under the service manager with either an
explicit absolute command argv or an authorized URL. Example command adapter:

```sh
python3 -B BidPilot/scripts/operations_worker.py \
  --root /private/bidpilot-runtime --studio STUDIO_UUID \
  --command-json '["/usr/bin/python3","/checkout/BidPilot/scripts/qwenwork_adapter.py","--app-db","/private/bidpilot-runtime/studios/STUDIO_UUID/app.db"]'
```

For a URL, use HTTPS and a private `0600` token file. Plain HTTP is accepted
only for loopback. The dispatcher never discovers browser sessions, reads
cookies, scrapes credentials, invokes a shell, or inherits the web server's
environment. The notification contains only studio/event identifiers, an
idempotency key, and a payload digest—not the conversation body. The adapter
reads pending work via the existing local MCP connector and must return:

```json
{"ok": true, "event_id": "the-received-event-id"}
```

The adapter should return success only after the QwenWork turn and its
idempotent BidPilot commit finish. An `event_id` may be delivered again after
a timeout or lost acknowledgement, so the adapter and existing request IDs
remain the final exactly-once boundary. Leases prevent concurrent claims;
lease tokens prevent an expired worker from completing a newly reclaimed job.
Failures back off and dead-letter after the configured attempt count. A failed
wake is retried from the operations queue; a missing wake is recreated by the
durable app scan. The worker polls every 250 ms by default, while a scheduled
QwenWork pending-work poll remains an independent recovery route.

### Verification, ordering and dead letters

The worker does not trust adapter self-attestation. After an `ok` response it
runs `AppWorkVerifier` against the studio's `app.db` (read-only): a
conversation turn is complete only when the inquiry's processed sequence has
advanced and an assistant reply exists; a media update only when its
notification is consumed; a creative request only when its change offer
exists; a portfolio search only when results are committed. An unverified
`ok` fails the event with `adapter_unverified` and requeues it.

Events carry a `lane` (the inquiry, deal or version they belong to). While one
event in a lane is leased, other events in the same lane wait, so two workers
never process the same inquiry concurrently. At the application level,
`claim_conversation_work` / `return_conversation_work` lease an inquiry's
pending turns to one agent run; a commit consumes the lease and an expired
lease returns the work to the queue.

`OperationsStore.dead_letters()` lists exhausted events with their last error;
`requeue(event_id)` gives a dead event a fresh attempt budget. The dispatcher
renews its lease while a slow adapter runs, and an adapter may report a
machine-readable reason such as `{"error": "awaiting_agent"}` on a non-zero
exit to distinguish "work still pending" from a broken adapter.

### Scheduled-consumer deployment (this hackathon)

`scripts/qwenwork_adapter.py` is the shipped adapter for deployments where the
authorized QwenWork consumer is a scheduled pending-work task rather than a
headless agent API. It performs no agent work and no writes: it recovers the
routing identifiers from the stable retry key, checks durable completion in
`app.db`, and answers `ok` only when the scheduled QwenWork consumer has
actually committed the reply or consumed the update. Otherwise it exits with
`awaiting_agent` and the queue backs off (agent-dependent wakes allow up to 12
attempts, roughly a 28-minute window). The scheduled consumer runs the
bidpilot-run/bidpilot-runtime conversation order through the scoped MCP
connector; the operations queue provides durability, ordering, verification
and observability around it. If the consumer is offline, events dead-letter
and remain inspectable instead of being silently marked done.

## Metrics

`OperationsStore.record_metric` accepts a fixed set of metrics covering queue
and reply latency, analysis time/cost/failure/partial results, outstanding
questions, duplicate delivery attempts, manual overrides, estimate errors and
booking conflicts. Labels reject credential- and message-like names. The
`metrics()` result also reports queue depth and oldest pending age. Export only
aggregates; keep the operations database private.

## Snapshot, restore, retention and revocation

`create_snapshot(paths, archive)` uses SQLite's online backup API for both
databases and copies the exact `app.db.media` tree used by uploads and derived
analysis frames without following symlinks. It records SHA-256 and
size for every file, and verifies the completed ZIP. Credentials are explicitly
excluded. Store the archive in encrypted access-controlled storage outside the
studio workspace.

`restore_snapshot(paths, archive)` verifies the studio ID, every digest and
both SQLite integrity checks before an atomic directory swap, preserving the
current credential directory. Stop the web service and workers before restore;
the function refuses a restore while a live queue lease exists. Test a restore
into a disposable runtime before relying on a backup.

`apply_retention(..., dry_run=True)` previews expired media and links. With
`dry_run=False`, it quarantines media, deletes original and derived files with
their analysis rows transactionally where possible, revokes expired inquiry
access and clears expired proposal capabilities. `registry.revoke(studio_id)`
revokes all inquiry/proposal access, dead-letters pending work and deletes the
studio's local credential files. It intentionally makes no secure-erasure claim
for SSDs or backups; apply the storage provider's deletion policy separately.

## Integration hooks (shared files not changed here)

- `server.py`: choose one operator-resolved `paths.app_db` at process startup;
  never multiplex studios using untrusted request values. Public writes return
  their existing pending receipt immediately; the worker discovers the durable
  rows without adding a cross-database transaction to the request path.
- `db.py`: no schema merge is required for the pilot because the operations
  queue and registry are separate databases. A future shared multi-tenant
  database must add and enforce `studio_id` on every business table.
- `mcp_server.py`: start one process/config per studio with
  `BIDPILOT_MCP_DB=paths.app_db`. Its current sample-data guard remains in
  place for the hackathon. Do not expose credentials, registry lookup,
  snapshots, restore, or arbitrary event payloads as MCP tools.
- Media workers: record `analysis_seconds`, `analysis_cost_sgd`, provider/model
  and prompt versions (as non-sensitive labels or business evidence), plus
  failed/partial outcomes. Enqueue a notification when analysis changes the
  conversation's pending state.
- Service manager: run one web/MCP/media/dispatch process group per studio,
  stop that group before restore or revocation, and keep a scheduled pending
  work poll as missed-notification recovery.
