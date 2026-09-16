import sys

with open('mahler/serve.py', 'r') as f:
    content = f.read()

get_part = '''
    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/run/") and path.endswith("/log"):
            run_id_str = path[len("/api/run/"):-len("/log")]
            try:
                run_id = int(run_id_str)
            except ValueError:
                self.send_error(404)
                return
            
            with self.lock:
                row = self.led.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            
            if not row or row["status"] not in ("running", "stopping"):
                self.send_error(404)
                return
                
            from .console import logtail
            run_dict = dict(row)
            lines = logtail.tail(run_dict)
            status_obj = logtail.live_status(run_dict)
            
            self._json(200, {"lines": lines, "status": status_obj})
            return

        if path not in ("/", "/fragment", "/api/state"):
'''

content = content.replace('''
    def do_GET(self):
        path = urlsplit(self.path).path
        if path not in ("/", "/fragment", "/api/state"):
''', get_part)

with open('mahler/serve.py', 'w') as f:
    f.write(content)
