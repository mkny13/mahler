import re

with open("mahler/console_walkthrough.py") as f:
    text = f.read()

insertions = """
    # Insert fixture data for the agent to interact with
    led.con.execute("INSERT INTO items (project, number, title, state, labels) VALUES ('mahler', 301, 'Walkthrough item', 'ready', 'mahler:working')")
    led.con.execute("INSERT INTO items (project, number, title, state, question, options) VALUES ('mahler', 302, 'Needs you item', 'needs-you', 'What should we do?', '[\\\"Option A\\\", \\\"Option B\\\"]')")
    led.con.execute("INSERT INTO uat (project, number, pr, sha, title, needs, shipped_at, verdict, verdict_at, bug, note) VALUES ('mahler', 303, 100, 'abcdef', 'UAT item', 'Check this.', '2026-09-16T00:00:00', NULL, NULL, NULL, NULL)")
"""

text = re.sub(
    r"# Insert fixture data for the agent to interact with.*?NULL\)\"\)",
    insertions.strip(),
    text,
    flags=re.DOTALL
)

with open("mahler/console_walkthrough.py", "w") as f:
    f.write(text)
