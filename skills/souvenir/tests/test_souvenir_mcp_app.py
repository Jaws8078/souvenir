"""Contract tests for the dependency-free Souvenir MCP Apps server."""
import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[3]
SCRIPT = ROOT / "skills" / "souvenir" / "scripts" / "souvenir_mcp_app.py"


class SouvenirMcpAppTests(unittest.TestCase):
    def test_mcp_manifest_points_at_stdio_server(self):
        manifest = json.loads((ROOT / "mcp.json").read_text(encoding="utf-8"))
        server = manifest["mcpServers"]["souvenir-app"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["command"], "python3")
        self.assertIn("souvenir_mcp_app.py", server["args"][0])

    def test_stdio_server_lists_tools_and_inlines_widget(self):
        packet = {
            "provider": "host-agent",
            "candidates": [{"id": "hotel-1", "title": "테스트 숙소", "price": 90000, "currency": "KRW"}],
        }
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "ui://souvenir/candidate-review-v1.html"}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "souvenir_render_candidate_review", "arguments": {"packet": packet, "kind": "hotels"}}},
        ]
        completed = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="\n".join(json.dumps(item, ensure_ascii=False) for item in requests) + "\n",
            text=True,
            capture_output=True,
            cwd=str(ROOT),
            timeout=10,
            check=True,
        )
        responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        tools = next(item["result"]["tools"] for item in responses if item.get("id") == 2)
        self.assertEqual({item["name"] for item in tools}, {"souvenir_render_candidate_review", "souvenir_search_candidates"})
        html = next(item["result"]["contents"][0]["text"] for item in responses if item.get("id") == 3)
        self.assertIn("window.openai", html)
        self.assertNotIn('src="candidate-review.js"', html)
        rendered = next(item["result"]["structuredContent"] for item in responses if item.get("id") == 4)
        self.assertEqual(rendered["sections"]["hotels"]["candidates"][0]["title"], "테스트 숙소")


if __name__ == "__main__":
    unittest.main()
