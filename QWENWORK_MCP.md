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
