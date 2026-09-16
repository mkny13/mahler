with open("tests/test_serve.py", "r") as f:
    text = f.read()

text = text.replace('self.led.con.execute("INSERT INTO runs (id, project, number, role, platform, epoch, status) VALUES (?, "mahler", 1, "build", "agy-gemini", 1, "running"), (run_id,))',
                    'self.led.con.execute("INSERT INTO runs (id, project, number, role, platform, epoch, status) VALUES (?, \'mahler\', 1, \'build\', \'agy-gemini\', 1, \'running\')", (run_id,))')
text = text.replace('self.led.con.execute("INSERT INTO runs (id, project, number, role, platform, epoch, status) VALUES (?, "mahler", 1, "build", "agy-gemini", 1, "ended"), (run_id,))',
                    'self.led.con.execute("INSERT INTO runs (id, project, number, role, platform, epoch, status) VALUES (?, \'mahler\', 1, \'build\', \'agy-gemini\', 1, \'ended\')", (run_id,))')

with open("tests/test_serve.py", "w") as f:
    f.write(text)
