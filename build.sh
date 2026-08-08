#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BUILD="$ROOT/build"
APP_DIR="$BUILD/WGKillSwitch.app"
MACOS_DIR="$APP_DIR/Contents/MacOS"
RES_DIR="$APP_DIR/Contents/Resources"

ARCH="$(uname -m)"
case "$ARCH" in
  arm64) TARGET="arm64-apple-macos13.0" ;;
  x86_64) TARGET="x86_64-apple-macos13.0" ;;
  *)
    echo "Неподдерживаемая архитектура: $ARCH" >&2
    exit 1
    ;;
esac

rm -rf "$BUILD"
mkdir -p "$MACOS_DIR" "$RES_DIR" "$BUILD/daemon"

echo "→ Compiling menu bar app ($TARGET)…"
xcrun swiftc \
  -O \
  -framework AppKit \
  -framework UserNotifications \
  -target "$TARGET" \
  -o "$MACOS_DIR/WGKillSwitch" \
  "$ROOT/app/main.swift"

cp "$ROOT/app/Info.plist" "$APP_DIR/Contents/Info.plist"
cp "$ROOT/daemon/wgksd.py" "$BUILD/daemon/wgksd.py"
chmod +x "$BUILD/daemon/wgksd.py" "$MACOS_DIR/WGKillSwitch"

echo "→ Built: $APP_DIR"
