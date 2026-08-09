#!/bin/zsh
set -euo pipefail

ROOT="$1"
APP_SRC="$2"
DAEMON_SRC="$3"
CTL_SRC="$4"
DAEMON_PLIST_SRC="$5"
HOME_DIR="$6"
UID_NUM="$7"
AGENT_PLIST_SRC="$8"

mkdir -p \
  "/usr/local/libexec/wgkillswitch" \
  "/Library/Application Support/WGKillSwitch" \
  "/Library/Logs/WGKillSwitch" \
  "/usr/local/bin"

cp "$DAEMON_SRC" "/usr/local/libexec/wgkillswitch/wgksd.py"
cp "$CTL_SRC" "/usr/local/libexec/wgkillswitch/wgksctl.py"
chmod 755 "/usr/local/libexec/wgkillswitch/wgksd.py" "/usr/local/libexec/wgkillswitch/wgksctl.py"
ln -sf "/usr/local/libexec/wgkillswitch/wgksctl.py" "/usr/local/bin/wgksctl"

DESIRED="/Library/Application Support/WGKillSwitch/desired.json"
if [[ ! -f "$DESIRED" ]]; then
  printf '%s\n' '{"enabled": false, "allowTailscale": true}' > "$DESIRED"
else
  /usr/bin/python3 - <<'PY'
import json
from pathlib import Path
p = Path("/Library/Application Support/WGKillSwitch/desired.json")
try:
    data = json.loads(p.read_text())
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}
data.setdefault("enabled", False)
data.setdefault("allowTailscale", True)
p.write_text(json.dumps(data, indent=2) + "\n")
PY
fi
chmod 666 "$DESIRED"
chmod 775 "/Library/Application Support/WGKillSwitch"
chmod 755 "/Library/Logs/WGKillSwitch"
# Ensure status is readable by the menu bar app
chmod 644 "/Library/Application Support/WGKillSwitch/"*.json 2>/dev/null || true
chmod 666 "/Library/Application Support/WGKillSwitch/desired.json"

rm -rf "/Applications/WGKillSwitch.app"
cp -R "$APP_SRC" "/Applications/WGKillSwitch.app"

cp "$DAEMON_PLIST_SRC" "/Library/LaunchDaemons/com.local.wgkillswitch.daemon.plist"
chown root:wheel "/Library/LaunchDaemons/com.local.wgkillswitch.daemon.plist"
chmod 644 "/Library/LaunchDaemons/com.local.wgkillswitch.daemon.plist"

launchctl bootout system/com.local.wgkillswitch.daemon 2>/dev/null || true
launchctl bootstrap system "/Library/LaunchDaemons/com.local.wgkillswitch.daemon.plist"
launchctl enable system/com.local.wgkillswitch.daemon
launchctl kickstart -k system/com.local.wgkillswitch.daemon

AGENT_DIR="$HOME_DIR/Library/LaunchAgents"
mkdir -p "$AGENT_DIR"
cp "$AGENT_PLIST_SRC" "$AGENT_DIR/com.local.wgkillswitch.agent.plist"
chown "$UID_NUM" "$AGENT_DIR/com.local.wgkillswitch.agent.plist"
chmod 644 "$AGENT_DIR/com.local.wgkillswitch.agent.plist"
