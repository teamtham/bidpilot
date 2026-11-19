# BidPilot — agent guide

Hackathon entry for QwenWork x Hashmeta AI OPC. Deadline: **23 Nov 2026, 23:59 SGT**.

Start with [`bidpilot-build-guide/BUILD_GUIDE.md`](bidpilot-build-guide/BUILD_GUIDE.md); it is the source of truth for what to build.

## Layout

- `BidPilot/` — the working Python app: `engine` (pricing/evaluation), `core.py`, `db.py`, `capacity.py`, `server.py` (founder dashboard + client proposal pages), `mcp_server.py` (MCP tools for QwenWork), and the `test_*.py` suites.
- `bidpilot-build-guide/` — the build guide, the QwenWork build prompt, and the agent skills (`bidpilot-intake`, `bidpilot-scoping`, `bidpilot-negotiator`, `bidpilot-run`).
  - `bidpilot-build-guide/bidpilot-engine/` intentionally duplicates `BidPilot/bidpilot-engine/` (the reference copy shipped with the guide). Keep both; if you change the engine, keep them in sync.
- `tools/qwenwork-mcp-bridge.js`, `qwenwork-mcp.json`, `QWENWORK_MCP.md` — local MCP bridge that lets QwenWork inspect this project.

## Who builds what

- **Cloud coding agents** build and test the Python engine, server and MCP code in `BidPilot/`.
- **QwenWork** runs the product's agents (intake, scoping, negotiator, run skills), talking to BidPilot through the MCP server / bridge. QwenWork is meant to be the engine of the product, so don't move agent logic into Python.

## Rules

- Never commit `server.log` or `*.db` (`bidpilot.db`); they contain local owner session tokens. See `.gitignore`.
- Run the tests in `BidPilot/` before pushing (e.g. `cd BidPilot && python -m unittest discover -p 'test_*.py'`).
