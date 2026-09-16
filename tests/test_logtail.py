import os
import tempfile
import unittest
from mahler.console import logtail
from mahler import config

class TestLogtail(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state)
        self.old_state = config.STATE
        config.STATE = self.state

    def tearDown(self):
        config.STATE = self.old_state
        self.tmp.cleanup()

    def test_missing_file(self):
        run = {"log_path": os.path.join(self.state, "missing.log")}
        self.assertEqual(logtail.tail(run), [])

    def test_outside_state(self):
        outside = os.path.join(self.tmp.name, "outside.log")
        with open(outside, "w") as f:
            f.write("foo")
        run = {"log_path": outside}
        self.assertEqual(logtail.tail(run), [])

    def test_claude_stream_json(self):
        log = os.path.join(self.state, "claude.log")
        with open(log, "w") as f:
            f.write('{"type":"assistant","message":{"content":[{"type":"text","text":"I\'ll start by reading GH_TOKEN=secret"}]}}\n')
            f.write('{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"gh issue view 253"}}]}}\n')
            f.write('{"type":"result","result":"Pushed successfully.","is_error":false}\n')
        run = {"platform": "claude", "log_path": log}
        
        lines = logtail.tail(run)
        self.assertEqual(len(lines), 3)
        # Newest first
        self.assertEqual(lines[0]["text"], "result: Pushed successfully.")
        self.assertEqual(lines[0]["tone"], "good")
        self.assertEqual(lines[1]["text"], "tool Bash gh issue view 253")
        self.assertEqual(lines[1]["tone"], "mut")
        self.assertEqual(lines[2]["text"], "I'll start by reading GH_TOKEN=<redacted>")
        self.assertEqual(lines[2]["tone"], "ink")

    def test_agy_stream_json(self):
        log = os.path.join(self.state, "agy.log")
        with open(log, "w") as f:
            f.write('{"type":"assistant.message_delta","data":{"deltaContent":"I will "}}\n')
            f.write('{"event":"step_update","step_update":{"state":"ACTIVE","step_type":"tool","tool_name":"run_command","tool_info":{"name":"run_command","parameters":{"CommandLine":"cat foo.txt"}}}}\n')
            f.write('{"event":"result","result":{"status":"SUCCESS","response":"Done"}}\n')
        run = {"platform": "agy-gemini", "log_path": log}
        
        lines = logtail.tail(run)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["text"], "result: Done")
        self.assertEqual(lines[0]["tone"], "good")
        self.assertEqual(lines[1]["text"], "tool run_command cat foo.txt")
        self.assertEqual(lines[1]["tone"], "mut")
        self.assertEqual(lines[2]["text"], "I will ")
        self.assertEqual(lines[2]["tone"], "ink")

    def test_cline_json_lines(self):
        log = os.path.join(self.state, "cline.log")
        with open(log, "w") as f:
            f.write('{"type":"agent_event","event":{"type":"content_start","contentType":"reasoning","reasoning":"Let me start"}}\n')
            f.write('{"type":"agent_event","event":{"type":"content_start","contentType":"tool","toolName":"run_commands","input":{"commands":["git push origin HEAD"]}}}\n')
            f.write('{"type":"agent_event","event":{"type":"content_end","contentType":"tool","output":[{"success":true,"result":"Done"}]}}\n')
        run = {"platform": "cline-free", "log_path": log}
        
        lines = logtail.tail(run)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["text"], "result: Done")
        self.assertEqual(lines[0]["tone"], "good")
        self.assertEqual(lines[1]["text"], "tool run_commands git push origin HEAD")
        self.assertEqual(lines[1]["tone"], "mut")
        self.assertEqual(lines[2]["text"], "Let me start")
        self.assertEqual(lines[2]["tone"], "ink")
        
        # Test live_status
        status = logtail.live_status(run)
        self.assertEqual(status["text"], "pushing commits")

    def test_live_status_no_output(self):
        log = os.path.join(self.state, "stale.log")
        with open(log, "w") as f:
            f.write('{"type":"assistant.message_delta","data":{"deltaContent":"I will "}}\n')
        # Set mtime to 6 minutes ago
        os.utime(log, (os.path.getatime(log), os.path.getmtime(log) - 360))
        run = {"platform": "agy-gemini", "log_path": log}
        
        status = logtail.live_status(run)
        self.assertEqual(status["text"], "no output 6m")
        self.assertEqual(status["tone"], "warn")

if __name__ == "__main__":
    unittest.main()
