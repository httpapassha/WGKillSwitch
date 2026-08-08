#!/usr/bin/env python3
"""User-facing control helper for WGKillSwitch desired state."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DESIRED = Path("/Library/Application Support/WGKillSwitch/desired.json")
STATUS = Path("/Library/Application Support/WGKillSwitch/status.json")


def write_enabled(enabled: bool) -> None:
    DESIRED.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"enabled": enabled}, indent=2) + "\n"
    # Non-atomic write: directory is root-owned and atomic replace needs creat in-dir.
    DESIRED.write_text(payload, encoding="utf-8")
    try:
        os.chmod(DESIRED, 0o666)
        os.chmod(DESIRED.parent, 0o775)
    except PermissionError:
        pass


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in {"-h", "--help"}:
        print("usage: wgksctl enable|disable|status|toggle")
        return 2
    cmd = argv[1]
    if cmd == "enable":
        write_enabled(True)
        print("enabled")
        return 0
    if cmd == "disable":
        write_enabled(False)
        print("disabled")
        return 0
    if cmd == "toggle":
        enabled = False
        if DESIRED.exists():
            try:
                enabled = bool(json.loads(DESIRED.read_text()).get("enabled"))
            except Exception:
                enabled = False
        write_enabled(not enabled)
        print("disabled" if enabled else "enabled")
        return 0
    if cmd == "status":
        if STATUS.exists():
            print(STATUS.read_text(), end="")
        else:
            print("{}")
        return 0
    print("unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
