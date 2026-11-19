"""Check the QwenWork connector configs at the repo root.

The configs hold absolute paths for the owner's Mac checkout. These tests map
that checkout root onto this repository, so they also pass on CI, and then
check that every path exists and that each connector starts and lists its tools.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIDPILOT_CONFIG = REPO / "qwenwork-bidpilot-mcp.json"
BRIDGE_CONFIG = REPO / "qwenwork-mcp.json"
CHECKOUT_ROOT = "/Users/teamtham/hackathon"

BIDPILOT_TOOLS = {
    "claim_intake", "get_deal", "evaluate_offer", "save_draft", "check_draft",
    "run_trace", "negotiation_payload", "record_step", "save_agent_output",
    "get_scoping_history", "list_pending_work", "submit_change_offer",
}
BRIDGE_TOOLS = {
    "project_summary", "read_project_file", "search_project", "project_diff",
    "run_project_check", "write_handoff",
}


def load_server(config_path, name):
    with open(config_path, encoding="utf-8") as f:
        servers = json.load(f)["mcpServers"]
    return servers[name]


def local(path):
    """Map a path under the configured checkout root onto this repository."""
    if path == CHECKOUT_ROOT:
        return REPO
    prefix = CHECKOUT_ROOT + "/"
    if not path.startswith(prefix):
        raise AssertionError(f"{path} is not under {CHECKOUT_ROOT}")
    return REPO / path[len(prefix):]


def list_tools(argv, env):
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "config-test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            env={**os.environ, **env})
    try:
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            if not line:
                raise AssertionError("server exited before tools/list: " + proc.stderr.read())
            reply = json.loads(line)
            if reply.get("id") == 2:
                return {tool["name"] for tool in reply["result"]["tools"]}
    finally:
        proc.kill()
        proc.communicate(timeout=10)


class BidpilotConnectorConfigTest(unittest.TestCase):
    def setUp(self):
        self.server = load_server(BIDPILOT_CONFIG, "bidpilot")

    def test_stdio_python_command(self):
        self.assertEqual(self.server["type"], "stdio")
        self.assertEqual(self.server["command"], "/usr/bin/env")
        self.assertEqual(self.server["args"][0], "python3")

    def test_paths_point_at_mcp_server_and_dashboard_db(self):
        script = local(self.server["args"][1])
        self.assertEqual(script, REPO / "BidPilot" / "mcp_server.py")
        self.assertTrue(script.is_file())
        # Must be the dashboard's default database, BidPilot/bidpilot.db.
        self.assertEqual(local(self.server["env"]["BIDPILOT_MCP_DB"]),
                         REPO / "BidPilot" / "bidpilot.db")

    def test_starts_and_lists_all_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = list_tools(
                [sys.executable, "-B", str(local(self.server["args"][1]))],
                {"BIDPILOT_MCP_DB": str(Path(tmp) / "bidpilot.db")})
        self.assertEqual(tools, BIDPILOT_TOOLS)


class ProjectBridgeConfigTest(unittest.TestCase):
    def setUp(self):
        self.server = load_server(BRIDGE_CONFIG, "hackathon-project-bridge")

    def test_stdio_node_command(self):
        self.assertEqual(self.server["type"], "stdio")
        self.assertEqual(self.server["command"], "/usr/bin/env")
        self.assertEqual(self.server["args"][0], "node")

    def test_paths_point_at_bridge_and_repo_root(self):
        script = local(self.server["args"][1])
        self.assertEqual(script, REPO / "tools" / "qwenwork-mcp-bridge.js")
        self.assertTrue(script.is_file())
        self.assertEqual(local(self.server["env"]["QWENWORK_PROJECT_ROOT"]), REPO)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_starts_and_lists_all_tools(self):
        tools = list_tools(["node", str(local(self.server["args"][1]))],
                           {"QWENWORK_PROJECT_ROOT": str(REPO)})
        self.assertEqual(tools, BRIDGE_TOOLS)


if __name__ == "__main__":
    unittest.main()
