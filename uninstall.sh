#!/bin/zsh
set -euo pipefail

UID_NUM="$(id -u)"
HOME_DIR="$HOME"

echo "→ Stopping menu bar…"
launchctl bootout "gui/${UID_NUM}/com.local.wgkillswitch.agent" 2>/dev/null || true
rm -f "$HOME_DIR/Library/LaunchAgents/com.local.wgkillswitch.agent.plist"

echo "→ Removing privileged components (потребуется пароль администратора)…"
if [[ -t 0 ]] || sudo -n true 2>/dev/null; then
  sudo launchctl bootout system/com.local.wgkillswitch.daemon 2>/dev/null || true
  sudo /sbin/pfctl -f /etc/pf.conf 2>/dev/null || true
  sudo rm -f /Library/LaunchDaemons/com.local.wgkillswitch.daemon.plist
  sudo rm -rf /Applications/WGKillSwitch.app
  sudo rm -rf /usr/local/libexec/wgkillswitch
  sudo rm -f /usr/local/bin/wgksctl
  sudo rm -rf "/Library/Application Support/WGKillSwitch"
  sudo rm -rf /Library/Logs/WGKillSwitch
else
  osascript <<'EOF'
do shell script "launchctl bootout system/com.local.wgkillswitch.daemon 2>/dev/null || true; /sbin/pfctl -f /etc/pf.conf 2>/dev/null || true; rm -f /Library/LaunchDaemons/com.local.wgkillswitch.daemon.plist; rm -rf /Applications/WGKillSwitch.app; rm -rf /usr/local/libexec/wgkillswitch; rm -f /usr/local/bin/wgksctl; rm -rf '/Library/Application Support/WGKillSwitch'; rm -rf /Library/Logs/WGKillSwitch" with administrator privileges
EOF
fi

echo "Снято."
