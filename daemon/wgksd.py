#!/usr/bin/env python3
"""WG Kill Switch root daemon.

Watches WireGuard (any profile via scutil), enforces PF kill-switch when enabled,
optionally allows Tailscale overlay, and notifies if WG is down while locking traffic.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

BASE = Path("/Library/Application Support/WGKillSwitch")
DESIRED_PATH = BASE / "desired.json"
STATUS_PATH = BASE / "status.json"
ANCHOR_PATH = BASE / "anchor.conf"
MAIN_PF_PATH = BASE / "main.pf.conf"
STATE_PATH = BASE / "daemon_state.json"

POLL_SECONDS = 1.0
NOTIFY_COOLDOWN = 60.0
ANCHOR_NAME = "wgkillswitch"
HEALTHY_GRACE_TICKS = 1

TAILSCALE_APP = Path("/Applications/Tailscale.app")


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[wgksd {ts}] {msg}", flush=True)


def run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def ensure_dirs() -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    if not DESIRED_PATH.exists():
        write_desired({"enabled": False, "allowTailscale": True})
    try:
        os.chmod(DESIRED_PATH, 0o666)
        os.chmod(BASE, 0o775)
    except PermissionError:
        pass


def write_desired(data: dict[str, Any]) -> None:
    DESIRED_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(DESIRED_PATH, 0o666)
    except PermissionError:
        pass


def read_desired() -> dict[str, Any]:
    """Return desired settings. allowTailscale defaults to True."""
    try:
        data = json.loads(DESIRED_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"enabled": False, "allowTailscale": True}
        return {
            "enabled": bool(data.get("enabled", False)),
            "allowTailscale": bool(data.get("allowTailscale", True)),
        }
    except Exception:
        return {"enabled": False, "allowTailscale": True}


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
    """Tailscale CGNAT 100.64.0.0/10 (includes node IPs; MagicDNS is 100.100.100.100)."""
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return a == 100 and 64 <= b <= 127


def is_likely_lan_addr(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    if a == 192 and b == 168:
        return True
    if a == 169 and b == 254:
        return True
    return False


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


def tailscale_installed() -> bool:
    """True if Tailscale client is present on this Mac (ignores peer list)."""
    if TAILSCALE_APP.is_dir():
        return True
    listed = run(["/usr/sbin/scutil", "--nc", "list"]).stdout
    if "io.tailscale.ipn.macos" in listed:
        return True
    for candidate in (
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/usr/local/bin/tailscale",
        "/opt/homebrew/bin/tailscale",
    ):
        if Path(candidate).exists():
            return True
    return False


def tailscale_service_connected() -> bool:
    listed = run(["/usr/sbin/scutil", "--nc", "list"]).stdout
    for line in listed.splitlines():
        if "io.tailscale.ipn.macos" not in line:
            continue
        if "(Connected)" in line:
            return True
    return False


def find_tailscale_ifaces() -> list[str]:
    """utun interfaces that carry Tailscale addressing — not dependent on peers."""
    utuns = list_utun_ipv4()
    return [name for name, ip in sorted(utuns.items()) if is_tailscale_addr(ip)]


def find_wg_ifaces(wg_connected: bool) -> list[str]:
    """Active WireGuard tunnel ifaces (excludes Tailscale and LAN-looking utuns)."""
    if not wg_connected:
        return []

    utuns = list_utun_ipv4()
    candidates = [
        name
        for name, ip in sorted(utuns.items())
        if not is_tailscale_addr(ip) and not is_likely_lan_addr(ip)
    ]
    if not candidates:
        # Last resort: any non-Tailscale utun
        candidates = [
            name for name, ip in sorted(utuns.items()) if not is_tailscale_addr(ip)
        ]
    if not candidates:
        return []

    primary = default_route_iface()
    if primary in candidates:
        return [primary]
    return [candidates[0]]


def physical_ifaces() -> list[str]:
    """Interfaces where clearnet can leak (Wi‑Fi/Ethernet/etc.)."""
    out = run(["/sbin/ifconfig", "-l"]).stdout.split()
    prefixes = ("en", "bridge", "ap", "pdp", "awdl", "llw", "vlan", "gif", "stf")
    return [n for n in out if n.startswith(prefixes)]


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
    enabled: bool,
    endpoints: list[tuple[str, int]],
    wg_ifaces: list[str],
    ts_ifaces: list[str],
    *,
    allow_tailscale: bool,
) -> None:
    if not enabled:
        ANCHOR_PATH.write_text("# kill-switch disabled\npass all\n", encoding="utf-8")
        return

    lines = [
        "# WGKillSwitch kill-switch anchor",
        "pass out quick on lo0 all",
        "pass in quick on lo0 all",
        "pass out quick inet proto udp from any port 68 to any port 67 keep state",
        "pass in quick inet proto udp from any port 67 to any port 68 keep state",
        "pass out quick inet proto udp from any to 255.255.255.255 port 67 keep state",
    ]

    # WG handshake must reach the peer on a physical NIC before the tunnel is up.
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

    for iface in wg_ifaces:
        lines.append(f"pass out quick on {iface} all")
        lines.append(f"pass in quick on {iface} all")

    if allow_tailscale:
        # Coexistence mode: block clearnet *out* on physical NICs only.
        # Tailscale NE / MagicDNS need a few physical exceptions:
        # - UDP/41641 peer paths
        # - DNS/53 upstream used by MagicDNS (otherwise browser hangs on
        #   console.tailscale.com while dig @1.1.1.1 still works)
        for iface in ts_ifaces:
            lines.append("# Tailscale overlay")
            lines.append(f"pass out quick on {iface} all")
            lines.append(f"pass in quick on {iface} all")
        for iface in physical_ifaces():
            lines.append(
                f"pass out quick on {iface} proto udp to any port 41641 keep state"
            )
            lines.append(
                f"pass in quick on {iface} proto udp from any port 41641 to any keep state"
            )
            lines.append(
                f"pass out quick on {iface} proto udp to any port 53 keep state"
            )
            lines.append(
                f"pass out quick on {iface} proto tcp to any port 53 keep state"
            )
            lines.append(
                f"pass in quick on {iface} proto udp from any port 53 to any keep state"
            )
            lines.append(
                f"pass in quick on {iface} proto tcp from any port 53 to any keep state"
            )
            lines.append(f"block drop out quick on {iface} all")
    else:
        # Strict mode: everything blocked except lo0/DHCP/WG.
        lines.insert(1, "block drop out all")

    ANCHOR_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_pf(enabled: bool, state: dict, *, force_full: bool = False) -> tuple[bool, str]:
    write_main_pf()
    was_enabled = bool(state.get("pfEngaged"))

    if not enabled:
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
        run(["/sbin/pfctl", "-a", ANCHOR_NAME, "-f", str(ANCHOR_PATH)])
        return True, err or "full-load ok"

    res = run(["/sbin/pfctl", "-a", ANCHOR_NAME, "-f", str(ANCHOR_PATH)])
    err = (res.stderr or res.stdout or "").strip()
    run(["/sbin/pfctl", "-a", ANCHOR_NAME, "-F", "states"])
    if res.returncode != 0:
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
    desired = read_desired()
    enabled = desired["enabled"]
    allow_ts = desired["allowTailscale"]

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
    wg_ifaces = find_wg_ifaces(wg_ok)

    ts_installed = tailscale_installed()
    ts_connected = tailscale_service_connected()
    ts_ifaces_all = find_tailscale_ifaces()
    # Only punch Tailscale into PF when user asked and client exists.
    ts_ifaces = ts_ifaces_all if (enabled and allow_ts and ts_installed) else []

    tunnel_ready = wg_ok and len(wg_ifaces) > 0
    if tunnel_ready:
        state["healthyTicks"] = int(state.get("healthyTicks", 0)) + 1
    else:
        state["healthyTicks"] = 0

    open_tunnel = tunnel_ready and int(state.get("healthyTicks", 0)) >= HEALTHY_GRACE_TICKS
    anchor_wg = wg_ifaces if open_tunnel else []

    rules_sig = json.dumps(
        {
            "enabled": enabled,
            "allowTailscale": allow_ts,
            "endpoints": [[h, p] for h, p in endpoints],
            "wg": anchor_wg,
            "ts": ts_ifaces,
        },
        sort_keys=True,
    )
    prev_rules_sig = state.get("rulesSig")
    became_healthy = open_tunnel and not state.get("wasOpenTunnel")
    lost_tunnel = (not open_tunnel) and bool(state.get("wasOpenTunnel"))

    if rules_sig != prev_rules_sig or became_healthy or lost_tunnel:
        write_anchor(
            enabled,
            endpoints,
            anchor_wg,
            ts_ifaces,
            allow_tailscale=bool(allow_ts and ts_installed),
        )
        force_full = enabled != bool(state.get("pfEngaged"))
        ok, pf_msg = apply_pf(enabled, state, force_full=force_full)
        state["rulesSig"] = rules_sig
        state["lastPfOk"] = ok
        state["lastPfMessage"] = pf_msg
        if became_healthy:
            log(
                f"tunnel restored via {','.join(anchor_wg)} "
                f"ts={','.join(ts_ifaces) or '-'} — soft PF reload"
            )
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

    primary_iface = wg_ifaces[0] if wg_ifaces else None
    ts_active_in_pf = bool(ts_ifaces)
    status = {
        "enabled": enabled,
        "allowTailscale": allow_ts,
        "tailscaleInstalled": ts_installed,
        "tailscaleConnected": ts_connected,
        "tailscaleActive": ts_active_in_pf,
        "tailscaleInterfaces": ts_ifaces_all,
        "pfOk": ok,
        "pfMessage": pf_msg,
        "wgConnected": wg_ok,
        "tunnelReady": open_tunnel,
        "activeProfile": active_name,
        "interface": primary_iface,
        "interfaces": wg_ifaces,
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
        allow_ts,
        wg_ok,
        open_tunnel,
        active_name,
        wg_ifaces,
        ts_installed,
        ts_connected,
        ts_ifaces,
        status["endpoints"],
        ok,
        unhealthy,
    ]
    if state.get("lastSig") != sig:
        log(
            f"enabled={enabled} allowTS={allow_ts} wg={active_name or '-'} "
            f"wg_if={wg_ifaces or '-'} ts_installed={ts_installed} "
            f"ts_if={ts_ifaces or '-'} ready={open_tunnel} unhealthy={unhealthy} pf={ok}"
        )
        state["lastSig"] = sig
        save_state(state)
    return state


def main() -> None:
    ensure_dirs()
    log("starting")
    state = load_state()
    state["pfEngaged"] = False
    state["rulesSig"] = None
    while True:
        try:
            state = tick(state)
        except Exception as exc:
            log(f"tick error: {exc!r}")
            desired = read_desired()
            write_status(
                {
                    "enabled": desired["enabled"],
                    "allowTailscale": desired["allowTailscale"],
                    "tailscaleInstalled": False,
                    "tailscaleConnected": False,
                    "tailscaleActive": False,
                    "tailscaleInterfaces": [],
                    "pfOk": False,
                    "pfMessage": repr(exc),
                    "wgConnected": False,
                    "tunnelReady": False,
                    "activeProfile": None,
                    "interface": None,
                    "interfaces": [],
                    "endpoints": [],
                    "profiles": [],
                    "blocking": desired["enabled"],
                    "unhealthy": True,
                    "icon": "error",
                }
            )
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
