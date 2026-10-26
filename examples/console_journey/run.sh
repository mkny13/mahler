#!/bin/sh
# Seed an isolated console, run the browser journey against it, tear down.
# Run it through the desktop gate:  mahler desktop run -- sh examples/console_journey/run.sh [out-dir]
set -u
here=$(cd "$(dirname "$0")" && pwd)
out=${1:-"$here/../../docs/audits/1119-journey"}
home=$(mktemp -d)
log="$home/seed.log"
MAHLER_HOME="$home" python3 "$here/seed.py" --home "$home" >"$log" 2>&1 &
seed=$!
trap 'kill "$seed" 2>/dev/null; wait "$seed" 2>/dev/null; rm -rf "$home"' EXIT
url=
for _ in $(seq 1 100); do
  url=$(sed -n 's/^CONSOLE_URL=//p' "$log")
  [ -n "$url" ] && break
  sleep 0.1
done
[ -n "$url" ] || { echo "seed did not start:"; cat "$log"; exit 1; }
echo "console: $url (MAHLER_HOME=$home)"
node "$here/journey.mjs" "$url" --home "$home" --out "$out"
