# Run BidPilot locally

Run these commands from the repository root with Python 3.11 or later.

```sh
python3 -B BidPilot/server.py
```

Open the owner URL printed in the terminal to use the founder dashboard. Keep that URL private. The default port is `8765` and the database is `BidPilot/bidpilot.db`. Override either with `--port` or `--db`. The sample intake form is at `http://127.0.0.1:8765/quote`.

In QwenWork, import and enable both stdio configurations, `qwenwork-mcp.json` and `qwenwork-bidpilot-mcp.json`. Adjust the paths for your checkout and keep `BIDPILOT_MCP_DB` pointed at the dashboard's database. Start a fresh QwenWork task after enabling the connectors. See [the MCP setup](../QWENWORK_MCP.md).

To clear the demo, enter `RESET SAMPLE DATA` in the founder dashboard's **Reset sample data** form. This clears sample records in SQLite and reloads the five seed tables. The public quote form and all workflows are for synthetic data.

## Verify the demo

```sh
python3 -m pip install playwright
python3 -m playwright install chromium
(cd BidPilot/bidpilot-engine && python3 -B -m unittest -v test_engine.py)
(cd BidPilot && python3 -B -m unittest discover -p 'test_*.py')
python3 -B BidPilot/scripts/demo_smoke.py
```

The smoke script replays synthetic agent outputs against a temporary database. QwenWork still runs intake extraction, comparable selection and reasoning, draft writing, and workflow orchestration. Its demo setup also covers the scheduled task, Hashmeta connector, website and CMS case study, and recording the demo video. Configure and verify those separately in the sample workspace.
