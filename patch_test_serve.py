import sys

with open('tests/test_serve.py', 'r') as f:
    content = f.read()

new_test = '''

class TestRunLog(_Served):
    def test_log_returns_lines_and_status(self):
        # Insert a running run
        run_id = 9999
        self.led.db.execute("INSERT INTO runs (id, project, status) VALUES (?, ?, ?)", (run_id, "mahler", "running"))
        
        status, headers, body = self.request(f"/api/run/{run_id}/log")
        self.assertEqual(status, 200)
        
        import json
        obj = json.loads(body)
        self.assertIn("lines", obj)
        self.assertIn("status", obj)

    def test_log_404_for_unknown_run(self):
        status, _, _ = self.request("/api/run/9998/log")
        self.assertEqual(status, 404)

    def test_log_404_for_ended_run(self):
        run_id = 9997
        self.led.db.execute("INSERT INTO runs (id, project, status, end_time) VALUES (?, ?, ?, 123)", (run_id, "mahler", "ended"))
        status, _, _ = self.request(f"/api/run/{run_id}/log")
        self.assertEqual(status, 404)
        
    def test_log_404_for_bad_id(self):
        status, _, _ = self.request("/api/run/abc/log")
        self.assertEqual(status, 404)

'''

content = content.replace('\nif __name__ == "__main__":', new_test + '\nif __name__ == "__main__":')

with open('tests/test_serve.py', 'w') as f:
    f.write(content)
