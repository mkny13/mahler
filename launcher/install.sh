#!/bin/sh
# Install or refresh the Mahler daemon. Safe to re-run. Does not start it:
# it prints the launchctl command for that.
set -eu
M="$HOME/.mahler"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$M/bin" "$M/logs" "$HOME/.local/bin" "$HOME/Library/LaunchAgents"

remote=$(git -C "$SRC" remote get-url origin)
[ -d "$M/app/.git" ] || git clone -q "$remote" "$M/app"
git -C "$M/app" fetch -q origin
git -C "$M/app" checkout -q --detach origin/main

cp "$SRC/launcher/mahler-launcher" "$M/bin/mahler-launcher"
chmod +x "$M/bin/mahler-launcher"
sed "s#__HOME__#$HOME#g" "$SRC/launcher/local.mahler.plist" \
  > "$HOME/Library/LaunchAgents/local.mahler.plist"
[ -f "$M/config.toml" ] || cp "$SRC/config.example.toml" "$M/config.toml"
ln -sf "$M/app/bin/mahler" "$HOME/.local/bin/mahler"

echo "installed. app at $(git -C "$M/app" rev-parse --short HEAD); config at $M/config.toml"
echo "start:  launchctl bootstrap gui/$(id -u) $HOME/Library/LaunchAgents/local.mahler.plist"
echo "stop:   launchctl bootout gui/$(id -u)/local.mahler"
