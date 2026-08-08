#!/usr/bin/env python3
"""WG Kill Switch root daemon.

Watches WireGuard (any profile via scutil), enforces PF kill-switch when enabled,
and notifies the console user if WG is down while locking traffic.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BASE = Path("/Library/Application Support/WGKillSwitch")
DESIRED_PATH = BASE / "desired.json"
STATUS_PATH = BASE / "status.json"
ANCHOR_PATH = BASE / "anchor.conf"
MAIN_PF_PATH = BASE / "main.pf.conf"
STATE_PATH = BASE / "daemon_state.json"

POLL_SECONDS = 1.0
NOTIFY_COOLDOWN = 60.0
ANCHOR_NAME = "wgkillswitch"
# After WG reports connected, wait until iface/route settle before opening tunnel.
HEALTHY_GRACE_TICKS = 1


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[wgksd {ts}] {msg}", flush=True)


def run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def ensure_dirs() -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    if not DESIRED_PATH.exists():
        DESIRED_PATH.write_text(
            json.dumps({"enabled": False}, indent=2) + "\n", encoding="utf-8"
        )
    try:
        os.chmod(DESIRED_PATH, 0o666)
        # Directory must stay writable so helpers can update desired.json
        os.chmod(BASE, 0o775)
    except PermissionError:
        pass


def read_desired() -> bool:
    try:
        data = json.loads(DESIRED_PATH.read_text(encoding="utf-8"))
        return bool(data.get("enabled", False))
    except Exception:
        return False


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def write_status(status: dict) -> None:
    status = dict(status)
    status["updatedAt"] = datetime.now(timezone.utc).isoformat()
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(STATUS_PATH)


def parse_wg_profiles() -> list[dict]:
    """Return WireGuard profiles from scutil --nc list/show."""
    listed = run(["/usr/sbin/scutil", "--nc", "list"]).stdout
    profiles: list[dict] = []
    pattern = re.compile(
        r"^\s*(\*?)\s*\((\w+)\)\s+([0-9A-Fa-f-]{36})\s+VPN\s+\(com\.wireguard\.macos\)\s+\"([^\"]+)\""
    )
    for line in listed.splitlines():
        m = pattern.search(line)
        if not m:
            continue
        connected = m.group(2).lower() == "connected"
        uuid = m.group(3)
        name = m.group(4)
        show = run(["/usr/sbin/scutil", "--nc", "show", uuid]).stdout
        endpoint = None
        em = re.search(r"RemoteAddress\s*:\s*(\S+)", show)
        if em:
            endpoint = em.group(1).strip()
        profiles.append(
            {
                "uuid": uuid,
                "name": name,
                "connected": connected,
                "endpoint": endpoint,
            }
        )
    return profiles


def parse_endpoint(endpoint: Optional[str]) -> Optional[tuple[str, int]]:
    if not endpoint:
        return None
    if endpoint.startswith("["):
        m = re.match(r"^\[([^\]]+)\]:(\d+)$", endpoint)
        if not m:
            return None
        return m.group(1), int(m.group(2))
    if endpoint.count(":") == 1:
        host, port_s = endpoint.rsplit(":", 1)
        try:
            return host, int(port_s)
        except ValueError:
            return None
    return None


def is_tailscale_addr(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return a == 100 and 64 <= b <= 127


def list_utun_ipv4() -> dict[str, str]:
    out = run(["/sbin/ifconfig"]).stdout
    result: dict[str, str] = {}
    current = None
    for line in out.splitlines():
        if line and not line.startswith("\t") and not line.startswith(" "):
            current = line.split(":", 1)[0]
            continue
        if current and current.startswith("utun"):
            m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\b", line)
            if m:
                result[current] = m.group(1)
    return result


def default_route_iface() -> Optional[str]:
    route = run(["/sbin/route", "-n", "get", "1.1.1.1"]).stdout
    for line in route.splitlines():
        line = line.strip()
        if line.startswith("interface:"):
            return line.split(":", 1)[1].strip()
    return None


def find_wg_ifaces(wg_connected: bool) -> list[str]:
    """All non-Tailscale utuns with IPv4 — any of them may be the active WG tunnel."""
    if not wg_connected:
        return []

    utuns = list_utun_ipv4()
    candidates = [
        name for name, ip in sorted(utuns.items()) if not is_tailscale_addr(ip)
    ]
    if not candidates:
        return []

    # Prefer putting the current default-route iface first.
    primary = default_route_iface()
    if primary in candidates:
        return [primary] + [c for c in candidates if c != primary]
    return candidates


def write_main_pf() -> None:
    content = f"""\
#
# WGKillSwitch PF wrapper — keeps Apple anchors, adds ours.
#
scrub-anchor "com.apple/*"
nat-anchor "com.apple/*"
rdr-anchor "com.apple/*"
dummynet-anchor "com.apple/*"
anchor "com.apple/*"
load anchor "com.apple" from "/etc/pf.anchors/com.apple"

anchor "{ANCHOR_NAME}"
load anchor "{ANCHOR_NAME}" from "{ANCHOR_PATH}"
"""
    MAIN_PF_PATH.write_text(content, encoding="utf-8")


def write_anchor(
    enabled: bool, endpoints: list[tuple[str, int]], ifaces: list[str]
) -> None:
    if not enabled:
        ANCHOR_PATH.write_text("# kill-switch disabled\npass all\n", encoding="utf-8")
        return

    lines = [
        "# WGKillSwitch kill-switch anchor",
        "block drop out all",
        "pass out quick on lo0 all",
        "pass in quick on lo0 all",
        "pass out quick inet proto udp from any port 68 to any port 67 keep state",
        "pass in quick inet proto udp from any port 67 to any port 68 keep state",
        "pass out quick inet proto udp from any to 255.255.255.255 port 67 keep state",
    ]
    for host, port in endpoints:
        if ":" in host:
            lines.append(
                f"pass out quick inet6 proto udp from any to {host} port {port} keep state"
            )
            lines.append(
                f"pass in quick inet6 proto udp from {host} port {port} to any keep state"
            )
        else:
            lines.append(
                f"pass out quick inet proto udp from any to {host} port {port} keep state"
            )
            lines.append(
                f"pass in quick inet proto udp from {host} port {port} to any keep state"
            )

    for iface in ifaces:
        lines.append(f"pass out quick on {iface} all")
        lines.append(f"pass in quick on {iface} all")

    ANCHOR_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_pf(enabled: bool, state: dict, *, force_full: bool = False) -> tuple[bool, str]:
    """Apply PF rules.

    Full ruleset load only on enable/disable transitions (or force).
    Interface/endpoint updates only reload our anchor — avoids breaking
    Network Extension after WireGuard reconnect.
    """
    write_main_pf()
    was_enabled = bool(state.get("pfEngaged"))

    if not enabled:
        # Empty/pass our anchor then restore stock ruleset.
        run(["/sbin/pfctl", "-a", ANCHOR_NAME, "-F", "all"])
        res = run(["/sbin/pfctl", "-f", "/etc/pf.conf"])
        err = (res.stderr or res.stdout or "").strip()
        state["pfEngaged"] = False
        if res.returncode != 0:
            return False, err or "pfctl restore failed"
        return True, "restored /etc/pf.conf"

    run(["/sbin/pfctl", "-e"])
    need_full = force_full or not was_enabled or not state.get("pfEngaged")
    if need_full:
        res = run(["/sbin/pfctl", "-f", str(MAIN_PF_PATH)])
        err = (res.stderr or res.stdout or "").strip()
        if res.returncode != 0:
            return False, err or "pfctl load failed"
        state["pfEngaged"] = True
        # Anchor already loaded via main; refresh explicitly.
        run(["/sbin/pfctl", "-a", ANCHOR_NAME, "-f", str(ANCHOR_PATH)])
        return True, err or "full-load ok"

    # Soft update: only our anchor + flush states so reconnect isn't stuck.
    res = run(["/sbin/pfctl", "-a", ANCHOR_NAME, "-f", str(ANCHOR_PATH)])
    err = (res.stderr or res.stdout or "").strip()
    run(["/sbin/pfctl", "-a", ANCHOR_NAME, "-F", "states"])
    if res.returncode != 0:
        # Fallback to full reload once.
        res2 = run(["/sbin/pfctl", "-f", str(MAIN_PF_PATH)])
        err2 = (res2.stderr or res2.stdout or "").strip()
        run(["/sbin/pfctl", "-a", ANCHOR_NAME, "-f", str(ANCHOR_PATH)])
        state["pfEngaged"] = res2.returncode == 0
        if res2.returncode != 0:
            return False, err2 or err or "anchor reload failed"
        return True, "full-fallback ok"
    return True, err or "anchor-reload ok"


def console_user() -> Optional[tuple[int, str]]:
    res = run(["/usr/bin/stat", "-f", "%u:%Su", "/dev/console"])
    if res.returncode != 0:
        return None
    text = res.stdout.strip()
    if not text or text.endswith(":root") or text.startswith("0:"):
        return None
    uid_s, name = text.split(":", 1)
    try:
        return int(uid_s), name
    except ValueError:
        return None


def notify(title: str, body: str, state: dict) -> None:
    now = time.time()
    last = float(state.get("lastNotifyAt", 0))
    key = f"{title}|{body}"
    if state.get("lastNotifyKey") == key and now - last < NOTIFY_COOLDOWN:
        return
    user = console_user()
    if not user:
        return
    uid, _name = user
    script = f'display notification {json.dumps(body)} with title {json.dumps(title)}'
    run(
        [
            "/bin/launchctl",
            "asuser",
            str(uid),
            "/usr/bin/osascript",
            "-e",
            script,
        ]
    )
    state["lastNotifyAt"] = now
    state["lastNotifyKey"] = key


def tick(state: dict) -> dict:
    ensure_dirs()
    enabled = read_desired()
    profiles = parse_wg_profiles()
    connected = [p for p in profiles if p["connected"]]
    endpoints: list[tuple[str, int]] = []
    seen = set()
    for p in profiles:
        parsed = parse_endpoint(p.get("endpoint"))
        if not parsed or parsed in seen:
            continue
        seen.add(parsed)
        endpoints.append(parsed)

    wg_ok = len(connected) > 0
    active_name = connected[0]["name"] if connected else None
    ifaces = find_wg_ifaces(wg_ok)

    # Require both scutil Connected and a real tunnel iface before opening traffic.
    tunnel_ready = wg_ok and len(ifaces) > 0
    if tunnel_ready:
        state["healthyTicks"] = int(state.get("healthyTicks", 0)) + 1
    else:
        state["healthyTicks"] = 0

    open_tunnel = tunnel_ready and int(state.get("healthyTicks", 0)) >= HEALTHY_GRACE_TICKS
    anchor_ifaces = ifaces if open_tunnel else []

    rules_sig = json.dumps(
        {
            "enabled": enabled,
            "endpoints": [[h, p] for h, p in endpoints],
            "ifaces": anchor_ifaces,
        },
        sort_keys=True,
    )
    prev_rules_sig = state.get("rulesSig")
    became_healthy = open_tunnel and not state.get("wasOpenTunnel")
    lost_tunnel = (not open_tunnel) and bool(state.get("wasOpenTunnel"))

    if rules_sig != prev_rules_sig or became_healthy or lost_tunnel:
        write_anchor(enabled, endpoints, anchor_ifaces)
        # Full reload only when engaging/disengaging KS; soft reload on reconnect.
        force_full = enabled != bool(state.get("pfEngaged"))
        ok, pf_msg = apply_pf(enabled, state, force_full=force_full)
        state["rulesSig"] = rules_sig
        state["lastPfOk"] = ok
        state["lastPfMessage"] = pf_msg
        if became_healthy:
            log(f"tunnel restored via {','.join(anchor_ifaces)} — soft PF reload")
    else:
        ok = bool(state.get("lastPfOk", True))
        pf_msg = state.get("lastPfMessage", "unchanged")

    state["wasOpenTunnel"] = open_tunnel

    unhealthy = enabled and not open_tunnel
    if unhealthy:
        notify(
            "WG Kill Switch",
            "WireGuard не подключён — весь трафик заблокирован",
            state,
        )
        state["alertActive"] = True
    elif state.get("alertActive") and enabled and open_tunnel:
        notify(
            "WG Kill Switch",
            f"WireGuard снова в сети ({active_name})",
            state,
        )
        state["alertActive"] = False
    elif not enabled:
        state["alertActive"] = False

    primary_iface = ifaces[0] if ifaces else None
    status = {
        "enabled": enabled,
        "pfOk": ok,
        "pfMessage": pf_msg,
        "wgConnected": wg_ok,
        "tunnelReady": open_tunnel,
        "activeProfile": active_name,
        "interface": primary_iface,
        "interfaces": ifaces,
        "endpoints": [f"{h}:{p}" for h, p in endpoints],
        "profiles": profiles,
        "blocking": enabled,
        "unhealthy": unhealthy,
        "icon": (
            "error"
            if unhealthy or not ok
            else ("on" if enabled and open_tunnel else ("off" if not enabled else "warn"))
        ),
    }
    write_status(status)
    save_state(state)

    sig = [
        enabled,
        wg_ok,
        open_tunnel,
        active_name,
        ifaces,
        status["endpoints"],
        ok,
        unhealthy,
    ]
    if state.get("lastSig") != sig:
        log(
            f"enabled={enabled} wg={active_name or '-'} ifaces={ifaces or '-'} "
            f"ready={open_tunnel} endpoints={len(endpoints)} unhealthy={unhealthy} pf={ok}"
        )
        state["lastSig"] = sig
        save_state(state)
    return state


def main() -> None:
    ensure_dirs()
    log("starting")
    state = load_state()
    # Don't trust stale pfEngaged across restarts — reconcile on first tick.
    state["pfEngaged"] = False
    state["rulesSig"] = None
    while True:
        try:
            state = tick(state)
        except Exception as exc:
            log(f"tick error: {exc!r}")
            write_status(
                {
                    "enabled": read_desired(),
                    "pfOk": False,
                    "pfMessage": repr(exc),
                    "wgConnected": False,
                    "tunnelReady": False,
                    "activeProfile": None,
                    "interface": None,
                    "interfaces": [],
                    "endpoints": [],
                    "profiles": [],
                    "blocking": read_desired(),
                    "unhealthy": True,
                    "icon": "error",
                }
            )
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
