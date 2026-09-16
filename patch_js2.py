with open("mahler/console/console.js", "r") as f:
    lines = f.read().splitlines()

# find first function pollRunLog
start_idx = -1
end_idx = -1
for i, line in enumerate(lines):
    if "var runLogTimer = null;" in line:
        start_idx = i
    if start_idx != -1 and line == "  }":
        end_idx = i
        break

poll_lines = lines[start_idx:end_idx+1]
lines = lines[:start_idx] + lines[end_idx+1:]

# find IIFE start
for i, line in enumerate(lines):
    if "function apply() {" in line:
        # insert before apply
        lines = lines[:i] + poll_lines + lines[i:]
        break

with open("mahler/console/console.js", "w") as f:
    f.write("\n".join(lines) + "\n")
