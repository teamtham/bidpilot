# QwenWork project bridge

This project includes a local MCP server that gives QwenWork safe, useful visibility into this repository. It runs entirely on this Mac and does not require an API key.

## Add it to QwenWork

1. In QwenWork, open **Extensions → Connectors → + Add**.
2. Choose **Paste JSON configuration** and paste the complete contents of [`qwenwork-mcp.json`](qwenwork-mcp.json).
3. Import it, enable **hackathon-project-bridge**, then start a **new** QwenWork task (new tasks load newly enabled connectors).
4. In that task, tell QwenWork: “Use the hackathon-project-bridge to inspect the project before proposing changes.”

If QwenWork asks for a manual STDIO configuration instead, enter:

```text
/usr/bin/env node "/Users/teamtham/hackathon/tools/qwenwork-mcp-bridge.js"
```

with this environment variable:

```text
QWENWORK_PROJECT_ROOT=/Users/teamtham/hackathon
```

## Tools exposed to QwenWork

- `project_summary` — repository files, package scripts, and Git status.
- `read_project_file` and `search_project` — read/search source files within this project only.
- `project_diff` — current Git changes.
- `run_project_check` — runs only declared `npm run test`, `lint`, `build`, or `check` scripts.
- `write_handoff` — appends an update to `QWENWORK_HANDOFF.md` for a later review.

The bridge intentionally does **not** expose arbitrary shell execution, files outside the project, or any credentials.

## BidPilot product tools

Import [qwenwork-bidpilot-mcp.json](qwenwork-bidpilot-mcp.json) as a second stdio connector and enable **bidpilot**. The configuration uses `/usr/bin/env python3` and shares the SQLite database with the founder dashboard. If the checkout is in a different directory or the dashboard uses `--db`, update the script path and `BIDPILOT_MCP_DB` to match.

| Skill or step | Tools |
| --- | --- |
| Intake | `list_pending_work`, `claim_intake`, `submit_change_offer`, `save_agent_output` |
| Scoping | `get_deal`, `get_scoping_history`, `save_agent_output` |
| Engine and run | `evaluate_offer`, `record_step`, `check_draft`, `run_trace` |
| Negotiator | `negotiation_payload`, `save_draft`, `save_agent_output` |
| Buyer conversation | `get_conversation_context`, `commit_chat_turn`, `price_inquiry_scope` |
| Reference interpretation | `get_reference_evidence`, `get_reference_frame`, `acknowledge_reference_update` |
| Proposal website | `compose_proposal_page`, `search_portfolio`, `commit_portfolio_search` |
| Additional work | `get_change_orders`, `price_additional_work`, `price_detailed_additional_work` |

Use synthetic sample records and stable request IDs for retries. Keep scoping history and run traces private. The negotiator receives public options through `negotiation_payload`. Release and booking confirmation stay in the founder dashboard, and client acceptance stays on the token-holder page.

The same runner handles private chat turns and portfolio searches on released proposal websites. Discover both through `list_pending_work`. A buyer can discuss a creative direction with QwenWork immediately. Only the founder can approve the commercial terms of the resulting revision. The [runtime skill](bidpilot-build-guide/bidpilot-runtime/SKILL.md) describes resuming a turn and delivering a result to the right proposal version.
