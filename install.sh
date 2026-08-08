#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Только macOS." >&2
  exit 1
fi

if ! xcrun --find swiftc >/dev/null 2>&1; then
  echo "Нужны Xcode Command Line Tools." >&2
  echo "Запустите: xcode-select --install" >&2
  exit 1
fi

if [[ ! -d /Applications/WireGuard.app ]]; then
  echo "Предупреждение: /Applications/WireGuard.app не найден."
  echo "Kill Switch рассчитан на официальный клиент WireGuard."
fi

chmod +x "$ROOT/build.sh" "$ROOT/install-root.sh" "$ROOT/uninstall.sh" 2>/dev/null || true

echo "→ Building…"
./build.sh

STAGE="$(mktemp -d -t wgks-stage)"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

echo "→ Staging…"
mkdir -p "$STAGE/app" "$STAGE/daemon" "$STAGE/build"
cp -R "$ROOT/build/WGKillSwitch.app" "$STAGE/build/"
cp "$ROOT/daemon/wgksd.py" "$STAGE/daemon/"
cp "$ROOT/daemon/wgksctl.py" "$STAGE/daemon/"
cp "$ROOT/daemon/com.local.wgkillswitch.daemon.plist" "$STAGE/daemon/"
cp "$ROOT/app/com.local.wgkillswitch.agent.plist" "$STAGE/app/"
cp "$ROOT/install-root.sh" "$STAGE/install-root.sh"
chmod +x "$STAGE/install-root.sh"
chmod -R a+rX "$STAGE"

APP_SRC="$STAGE/build/WGKillSwitch.app"
DAEMON_SRC="$STAGE/daemon/wgksd.py"
CTL_SRC="$STAGE/daemon/wgksctl.py"
DAEMON_PLIST_SRC="$STAGE/daemon/com.local.wgkillswitch.daemon.plist"
AGENT_PLIST_SRC="$STAGE/app/com.local.wgkillswitch.agent.plist"
ROOT_INSTALL="$STAGE/install-root.sh"
UID_NUM="$(id -u)"
HOME_DIR="$HOME"

run_privileged() {
  if [[ -n "${SUDO_ASKPASS:-}" ]] || sudo -n true 2>/dev/null; then
    sudo "$ROOT_INSTALL" "$@"
    return
  fi
  # GUI admin prompt when terminal sudo isn't available
  TMP_RUN="$(mktemp -t wgks-install)"
  cat > "$TMP_RUN" <<EOF
#!/bin/zsh
set -euo pipefail
"$ROOT_INSTALL" \\
  "$STAGE" \\
  "$APP_SRC" \\
  "$DAEMON_SRC" \\
  "$CTL_SRC" \\
  "$DAEMON_PLIST_SRC" \\
  "$HOME_DIR" \\
  "$UID_NUM" \\
  "$AGENT_PLIST_SRC"
EOF
  chmod +x "$TMP_RUN"
  osascript <<EOF
do shell script "$TMP_RUN" with administrator privileges
EOF
  rm -f "$TMP_RUN"
}

echo "→ Installing (потребуется пароль администратора)…"
if sudo -n true 2>/dev/null; then
  sudo "$ROOT_INSTALL" \
    "$STAGE" \
    "$APP_SRC" \
    "$DAEMON_SRC" \
    "$CTL_SRC" \
    "$DAEMON_PLIST_SRC" \
    "$HOME_DIR" \
    "$UID_NUM" \
    "$AGENT_PLIST_SRC"
else
  # Prefer interactive sudo in a real terminal; fall back to macOS dialog
  if [[ -t 0 ]]; then
    sudo "$ROOT_INSTALL" \
      "$STAGE" \
      "$APP_SRC" \
      "$DAEMON_SRC" \
      "$CTL_SRC" \
      "$DAEMON_PLIST_SRC" \
      "$HOME_DIR" \
      "$UID_NUM" \
      "$AGENT_PLIST_SRC"
  else
    run_privileged
  fi
fi

echo "→ Loading menu bar agent…"
AGENT_PLIST="$HOME/Library/LaunchAgents/com.local.wgkillswitch.agent.plist"
launchctl bootout "gui/${UID_NUM}/com.local.wgkillswitch.agent" 2>/dev/null || true
launchctl bootstrap "gui/${UID_NUM}" "$AGENT_PLIST"
launchctl enable "gui/${UID_NUM}/com.local.wgkillswitch.agent"
launchctl kickstart -k "gui/${UID_NUM}/com.local.wgkillswitch.agent"

sleep 1.5
echo "→ Status:"
/usr/local/bin/wgksctl status || true
echo ""
echo "Готово. Иконка щита в меню-баре."
echo "Вкл/выкл: клик по иконке → Kill Switch"
echo "CLI: wgksctl enable|disable|status|toggle"
