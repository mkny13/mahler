"""Live trial 2 (mahler#918): one Agent SDK call through the credits gateway.

Run on the mini from a Mahler checkout of this branch, keychain unlocked:
    /opt/homebrew/bin/python3.14 sdk_runner/bootstrap/trial-918.py
No-cost dry run against a fake upstream (e.g. FINDINGS.md's capture server):
    ... trial-918.py --fake-upstream 127.0.0.1:18904

Starts a real `api_credits.Gateway` on loopback with an in-memory ledger and a
synthetic $0.10 grant ceiling, gives `mahler-sdk-run` only a local token, and
asks Sonnet for one word with no tools. Prints the gateway's own ledger
afterwards: the evidence is a settled reservation, not the client's output.
"""
import datetime
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from mahler import api_credits  # noqa: E402
from mahler.ledger import Ledger  # noqa: E402

CEILING_USD = 0.10
MODEL = "claude-sonnet-5-5"
GROUP = "claude-api"

fake = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--fake-upstream" else None
key = "dry-run-key" if fake else subprocess.run(["security", "find-generic-password", "-s", "mahler-anthropic-api",
                      "-a", "work", "-w"], capture_output=True, text=True).stdout.strip()
if not key:
    sys.exit("Cannot read the API key. Over ssh, run: security unlock-keychain")
exe = shutil.which("mahler-sdk-run") or os.path.expanduser("~/.local/bin/mahler-sdk-run")

led = Ledger(":memory:", thread_safe=True)
end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
grant = {"id": "trial-918", "ceiling_usd": CEILING_USD, "end": end}
gw = api_credits.Gateway(led, GROUP, lambda: grant, api_credits.model_pricing({}),
                         [MODEL], 8192, key,
                         connect=(lambda: http.client.HTTPConnection(
                             *fake.rsplit(":", 1), timeout=60)) if fake else None)
port = gw.start("127.0.0.1")
token = gw.register(918)

with tempfile.TemporaryDirectory() as tmp:
    prompt = os.path.join(tmp, "prompt.md")
    with open(prompt, "w") as fh:
        fh.write("Reply with exactly PONG")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_API_KEY=token)
    proc = subprocess.run([exe, "--prompt-file", prompt, "--cwd", tmp, "--model", MODEL,
                           "--max-turns", "1", "--max-output-tokens", "64", "--no-tools",
                           "--max-budget-usd", str(CEILING_USD)],
                          env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=300)
gw.stop()

result = {}
for line in proc.stdout.splitlines():
    try:
        m = json.loads(line)
    except ValueError:
        continue
    if m.get("type") == "result":
        result = m
print("runner exit:", proc.returncode, "| is_error:", result.get("is_error"),
      "| reply:", (result.get("result") or "")[:80],
      "| SDK cost estimate: $%s" % result.get("total_cost_usd"))
if result.get("errors"):
    print("errors:", result["errors"])
print("\nGateway ledger (reservation -> settled):")
for r in led.q("SELECT status, amount_usd, settled_usd FROM credit_reservations ORDER BY id"):
    print(f"  {r['status']:9} reserved ${r['amount_usd']:.4f}  settled "
          + (f"${r['settled_usd']:.6f}" if r["settled_usd"] is not None else "-"))
print("exposure: $%.6f of $%.2f ceiling" % (led.credit_exposure(GROUP, "trial-918"), CEILING_USD))
for e in led.q("SELECT kind, detail FROM events ORDER BY id"):
    print("  event:", e["kind"], (e["detail"] or "")[:160])
led.close()
