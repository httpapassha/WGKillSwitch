#!/usr/bin/env python3
"""User-facing control helper for WGKillSwitch desired state."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

DESIRED = Path("/Library/Application Support/WGKillSwitch/desired.json")
STATUS = Path("/Library/Application Support/WGKillSwitch/status.json")


def read_desired() -> dict[str, Any]:
    data: dict[str, Any] = {"enabled": False, "allowTailscale": True}
    if DESIRED.exists():
        try:
            loaded = json.loads(DESIRED.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data["enabled"] = bool(loaded.get("enabled", False))
                data["allowTailscale"] = bool(loaded.get("allowTailscale", True))
        except Exception:
            pass
    return data


def write_desired(data: dict[str, Any]) -> None:
    DESIRED.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": bool(data.get("enabled", False)),
        "allowTailscale": bool(data.get("allowTailscale", True)),
    }
    DESIRED.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(DESIRED, 0o666)
        os.chmod(DESIRED.parent, 0o775)
    except PermissionError:
        pass


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in {"-h", "--help"}:
        print(
            "usage: wgksctl enable|disable|toggle|status|"
            "tailscale-on|tailscale-off|tailscale-toggle"
        )
        return 2

    cmd = argv[1]
    cur = read_desired()

    if cmd == "enable":
        cur["enabled"] = True
        write_desired(cur)
        print("enabled")
        return 0
    if cmd == "disable":
        cur["enabled"] = False
        write_desired(cur)
        print("disabled")
        return 0
    if cmd == "toggle":
        cur["enabled"] = not cur["enabled"]
        write_desired(cur)
        print("enabled" if cur["enabled"] else "disabled")
        return 0
    if cmd == "tailscale-on":
        cur["allowTailscale"] = True
        write_desired(cur)
        print("allowTailscale=true")
        return 0
    if cmd == "tailscale-off":
        cur["allowTailscale"] = False
        write_desired(cur)
        print("allowTailscale=false")
        return 0
    if cmd == "tailscale-toggle":
        cur["allowTailscale"] = not cur["allowTailscale"]
        write_desired(cur)
        print("allowTailscale=" + ("true" if cur["allowTailscale"] else "false"))
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
