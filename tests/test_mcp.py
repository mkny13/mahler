import json
import sys
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch

from mahler.ledger import Ledger
from mahler.mcp import serve

class DummyConfig:
    def __getitem__(self, key):
        return {"repo": "test/repo", "interactive_lease_minutes": 30}
    def get(self, key):
        return None

class TestMCP(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(":memory:")
        self.cfg = {
            "defaults": {"interactive_lease_minutes": 30},
            "projects": {"mahler": {"repo": "mkny13/mahler"}}
        }
        
    def run_mcp(self, inputs):
        stdin = StringIO("\n".join(json.dumps(i) for i in inputs) + "\n")
        stdout = StringIO()
        with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout):
            serve(self.cfg, self.led)
        return [json.loads(line) for line in stdout.getvalue().strip().split("\n") if line.strip()]

    def test_initialize(self):
        out = self.run_mcp([{"jsonrpc": "2.0", "id": 1, "method": "initialize"}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], 1)
        self.assertEqual(out[0]["result"]["serverInfo"]["name"], "mahler-mcp")

    def test_tools_list(self):
        out = self.run_mcp([{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
        self.assertEqual(len(out), 1)
        tools = out[0]["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertIn("list_items", names)
        self.assertIn("add_item", names)
        self.assertIn("claim", names)

    def test_list_items(self):
        self.led.upsert_item("mahler", 4, title="Test item")
        out = self.run_mcp([{
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "list_items", "arguments": {"project": "mahler"}}
        }])
        content = out[0]["result"]["content"][0]["text"]
        items = json.loads(content)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["number"], 4)

    @patch("mahler.mcp.GH")
    def test_add_item(self, mock_gh):
        instance = mock_gh.return_value
        instance.create_issue.return_value = "https://github.com/mkny13/mahler/issues/5"
        out = self.run_mcp([{
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "add_item", "arguments": {"project": "mahler", "title": "New", "claim": True}}
        }])
        text = out[0]["result"]["content"][0]["text"]
        self.assertIn("Created issue", text)
        self.assertIn("Claimed as", text)
        lease = self.led.lease("mahler", 5)
        self.assertIsNotNone(lease)
        self.assertEqual(lease["holder"], "interactive:mcp")
        
    def test_claim_and_release(self):
        self.led.upsert_item("mahler", 6, title="Claim me")
        # claim
        out = self.run_mcp([{
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "claim", "arguments": {"project": "mahler", "number": 6, "holder": "agy"}}
        }])
        self.assertIn("Claimed successfully", out[0]["result"]["content"][0]["text"])
        lease = self.led.lease("mahler", 6)
        self.assertIsNotNone(lease)
        self.assertEqual(lease["holder"], "interactive:agy")
        
        # release
        out2 = self.run_mcp([{
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "release", "arguments": {"project": "mahler", "number": 6, "holder": "agy"}}
        }])
        self.assertIn("Released successfully", out2[0]["result"]["content"][0]["text"])
        self.assertIsNone(self.led.lease("mahler", 6))

    @patch("mahler.mcp.GH")
    def test_handoff(self, mock_gh):
        instance = mock_gh.return_value
        out = self.run_mcp([{
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "handoff", "arguments": {"project": "mahler", "number": 6, "comment": "Notes"}}
        }])
        self.assertIn("Handoff comment posted", out[0]["result"]["content"][0]["text"])
        instance.comment.assert_called_with(6, "<!-- mahler:handoff -->\nNotes")

if __name__ == '__main__':
    unittest.main()
