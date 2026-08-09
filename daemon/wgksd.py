#!/usr/bin/env python3
"""WG Kill Switch root daemon.

WireGuard kill-switch via PF, with optional Tailscale coexistence:
clearnet stays on WG; Tailscale control plane / MagicDNS / peers keep working.
"""

from __future__ import annotations

import json
import os
import re
import socket
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
TS_TABLE = "wgks_ts"
HEALTHY_GRACE_TICKS = 1
TS_DNS_REFRESH_SECONDS = 120.0

TAILSCALE_APP = Path("/Applications/Tailscale.app")

# Control-plane anycast used by login/api/controlplane/app.tailscale.com
TS_CONTROL_CIDRS = ("192.200.0.0/24",)

# Hostnames that must remain reachable for admin UI / logging (resolved into table).
TS_EXTRA_HOSTS = (
    "console.tailscale.com",
    "login.tailscale.com",
    "controlplane.tailscale.com",
    "api.tailscale.com",
    "app.tailscale.com",
    "log.tailscale.io",
)

# Public resolvers MagicDNS / apps may use on the underlay NIC.
TS_DNS_RESOLVERS = (
    "1.1.1.1",
    "1.0.0.1",
    "8.8.8.8",
    "8.8.4.4",
    "9.9.9.9",
)

# MagicDNS upstream on macOS commonly forwards to the LAN DHCP resolver
# (e.g. 192.168.0.1 on en0). Without this, public names hang on 100.100.100.100
# while tailnet names still resolve.
TS_DNS_PRIVATE_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
)

DERP_MAP_URL = "https://controlplane.tailscale.com/derpmap/default"


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
    payload = {
        "enabled": bool(data.get("enabled", False)),
        "allowTailscale": bool(data.get("allowTailscale", True)),
    }
    DESIRED_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(DESIRED_PATH, 0o666)
    except PermissionError:
        pass


def read_desired() -> dict[str, Any]:
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
    return (a == 192 and b == 168) or (a == 169 and b == 254)


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


def physical_ifaces() -> list[str]:
    out = run(["/sbin/ifconfig", "-l"]).stdout.split()
    prefixes = ("en", "bridge", "ap", "pdp", "awdl", "llw", "vlan")
    # Exclude unused tunnels gif/stf from block noise; they rarely leak clearnet.
    return [n for n in out if n.startswith(prefixes)]


def tailscale_installed() -> bool:
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
        if "io.tailscale.ipn.macos" in line and "(Connected)" in line:
            return True
    return False


def find_tailscale_ifaces() -> list[str]:
    utuns = list_utun_ipv4()
    return [name for name, ip in sorted(utuns.items()) if is_tailscale_addr(ip)]


def find_wg_ifaces(wg_connected: bool) -> list[str]:
    if not wg_connected:
        return []
    utuns = list_utun_ipv4()
    candidates = [
        name
        for name, ip in sorted(utuns.items())
        if not is_tailscale_addr(ip) and not is_likely_lan_addr(ip)
    ]
    if not candidates:
        candidates = [
            name for name, ip in sorted(utuns.items()) if not is_tailscale_addr(ip)
        ]
    if not candidates:
        return []
    primary = default_route_iface()
    if primary in candidates:
        return [primary]
    return [candidates[0]]


def resolve_host_ipv4(host: str, *, via: Optional[str] = None) -> list[str]:
    """Resolve A records. Prefer dig via a public resolver to avoid MagicDNS hangs."""
    ips: list[str] = []
    dig_cmd = ["/usr/bin/dig", "+time=2", "+tries=1", "+short"]
    if via:
        dig_cmd += [f"@{via}"]
    dig_cmd += [host, "A"]
    dig = run(dig_cmd)
    for line in dig.stdout.splitlines():
        line = line.strip().rstrip(".")
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", line) and line not in ips:
            ips.append(line)
    if ips:
        return ips
    # Fallback: system resolver (may hang if MagicDNS upstream is broken)
    try:
        socket.setdefaulttimeout(2.0)
        for fam, _, _, _, sockaddr in socket.getaddrinfo(host, None, socket.AF_INET):
            if fam == socket.AF_INET:
                ip = sockaddr[0]
                if ip not in ips:
                    ips.append(ip)
    except (socket.gaierror, socket.timeout):
        pass
    finally:
        socket.setdefaulttimeout(None)
    return ips


def fetch_derp_ipv4s() -> list[str]:
    """IPv4s of Tailscale DERP relays (needed on underlay for HTTPS/UDP relay)."""
    ips: list[str] = []
    # Resolve via public DNS IP so we don't depend on MagicDNS/LAN upstream.
    control_ips = resolve_host_ipv4("controlplane.tailscale.com", via="1.1.1.1")
    if not control_ips:
        control_ips = resolve_host_ipv4("controlplane.tailscale.com", via="8.8.8.8")
    curl_cmd = ["/usr/bin/curl", "-fsS", "--max-time", "12"]
    if control_ips:
        curl_cmd += [
            "--resolve",
            f"controlplane.tailscale.com:443:{control_ips[0]}",
        ]
    curl_cmd += [DERP_MAP_URL]
    res = run(curl_cmd)
    if res.returncode != 0 or not res.stdout.strip():
        return ips
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return ips
    for region in (data.get("Regions") or {}).values():
        if not isinstance(region, dict):
            continue
        for node in region.get("Nodes") or []:
            if not isinstance(node, dict):
                continue
            ip = node.get("IPv4")
            if isinstance(ip, str) and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
                if ip not in ips:
                    ips.append(ip)
    return ips


def collect_ts_allow_targets() -> list[str]:
    """CIDRs/IPs allowed on physical NIC for Tailscale control/admin/DERP."""
    targets: list[str] = list(TS_CONTROL_CIDRS)
    for host in TS_EXTRA_HOSTS:
        for ip in resolve_host_ipv4(host, via="1.1.1.1"):
            if ip not in targets:
                targets.append(ip)
    for ip in fetch_derp_ipv4s():
        if ip not in targets:
            targets.append(ip)
    return targets


def sync_ts_table(targets: list[str]) -> None:
    if not targets:
        run(["/sbin/pfctl", "-t", TS_TABLE, "-T", "flush"])
        return
    run(["/sbin/pfctl", "-t", TS_TABLE, "-T", "replace"] + targets)


def write_main_pf() -> None:
    # Table lives in the main ruleset (global) so anchor rules can reference it
    # without "namespace collision" warnings from redefining it in the anchor.
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

table <{TS_TABLE}> persist

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
        for iface in ts_ifaces:
            lines.append("# Tailscale overlay")
            lines.append(f"pass out quick on {iface} all")
            lines.append(f"pass in quick on {iface} all")

        for iface in physical_ifaces():
            # Peer path (direct WireGuard)
            lines.append(
                f"pass out quick on {iface} inet proto udp to any port 41641 keep state"
            )
            lines.append(
                f"pass in quick on {iface} inet proto udp from any port 41641 to any keep state"
            )
            # Control plane / admin / logs (table, not all of 443)
            lines.append(
                f"pass out quick on {iface} inet proto tcp to <{TS_TABLE}> port 443 keep state"
            )
            lines.append(
                f"pass out quick on {iface} inet proto tcp to <{TS_TABLE}> port 80 keep state"
            )
            # MagicDNS upstream: public resolvers + LAN/DHCP resolvers
            for dns in TS_DNS_RESOLVERS:
                lines.append(
                    f"pass out quick on {iface} inet proto udp to {dns} port 53 keep state"
                )
                lines.append(
                    f"pass out quick on {iface} inet proto tcp to {dns} port 53 keep state"
                )
            for cidr in TS_DNS_PRIVATE_CIDRS:
                lines.append(
                    f"pass out quick on {iface} inet proto udp to {cidr} port 53 keep state"
                )
                lines.append(
                    f"pass out quick on {iface} inet proto tcp to {cidr} port 53 keep state"
                )
            # Kill clearnet leak on this NIC (IPv4 + IPv6)
            lines.append(f"block drop out quick on {iface} all")
    else:
        lines.insert(2, "block drop out all")

    ANCHOR_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_pf(
    enabled: bool,
    state: dict,
    *,
    force_full: bool = False,
    flush_states: bool = False,
) -> tuple[bool, str]:
    write_main_pf()
    was_enabled = bool(state.get("pfEngaged"))

    if not enabled:
        run(["/sbin/pfctl", "-a", ANCHOR_NAME, "-F", "all"])
        run(["/sbin/pfctl", "-t", TS_TABLE, "-T", "flush"])
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
        # Full load replaces ruleset; flush only when tightening (enable / lost tunnel).
        if flush_states:
            run(["/sbin/pfctl", "-a", ANCHOR_NAME, "-F", "states"])
        return True, err or "full-load ok"

    res = run(["/sbin/pfctl", "-a", ANCHOR_NAME, "-f", str(ANCHOR_PATH)])
    err = (res.stderr or res.stdout or "").strip()
    # Never flush on routine soft reload — that kills Tailscale's long-lived
    # HTTPS map sync and causes mapresponse-timeout / Mac offline in console.
    if flush_states:
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
    seen: set[tuple[str, int]] = set()
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
    use_ts_mode = bool(enabled and allow_ts and ts_installed)
    ts_ifaces = ts_ifaces_all if use_ts_mode else []

    # Refresh Tailscale allow table (control plane / console CDN / logs)
    now = time.time()
    ts_targets: list[str] = list(state.get("tsTargets") or [])
    if use_ts_mode and (
        now - float(state.get("tsTargetsAt", 0)) >= TS_DNS_REFRESH_SECONDS
        or not ts_targets
    ):
        ts_targets = collect_ts_allow_targets()
        state["tsTargets"] = ts_targets
        state["tsTargetsAt"] = now
        sync_ts_table(ts_targets)
    elif not use_ts_mode and state.get("tsTargets"):
        sync_ts_table([])
        state["tsTargets"] = []
        state["tsTargetsAt"] = 0

    tunnel_ready = wg_ok and len(wg_ifaces) > 0
    if tunnel_ready:
        state["healthyTicks"] = int(state.get("healthyTicks", 0)) + 1
    else:
        state["healthyTicks"] = 0

    open_tunnel = tunnel_ready and int(state.get("healthyTicks", 0)) >= HEALTHY_GRACE_TICKS
    anchor_wg = wg_ifaces if open_tunnel else []

    # Intentionally omit tsTargets: table is updated via pfctl -T replace only.
    # Including DNS IPs here caused anchor reload + state flush every refresh,
    # which tore down Tailscale's control-plane TCP and marked the Mac offline.
    rules_sig = json.dumps(
        {
            "enabled": enabled,
            "allowTailscale": allow_ts,
            "useTsMode": use_ts_mode,
            "endpoints": [[h, p] for h, p in endpoints],
            "wg": anchor_wg,
            "ts": ts_ifaces,
            "phys": physical_ifaces() if use_ts_mode else [],
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
            allow_tailscale=use_ts_mode,
        )
        force_full = enabled != bool(state.get("pfEngaged"))
        # Flush only when tightening: enable KS or lose WG tunnel.
        flush_states = (enabled and not bool(state.get("pfEngaged"))) or lost_tunnel
        ok, pf_msg = apply_pf(
            enabled,
            state,
            force_full=force_full,
            flush_states=flush_states,
        )
        if use_ts_mode and ts_targets:
            sync_ts_table(ts_targets)
        state["rulesSig"] = rules_sig
        state["lastPfOk"] = ok
        state["lastPfMessage"] = pf_msg
        if became_healthy:
            log(
                f"tunnel restored via {','.join(anchor_wg)} "
                f"ts_mode={use_ts_mode} ts_if={','.join(ts_ifaces) or '-'}"
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
    status = {
        "enabled": enabled,
        "allowTailscale": allow_ts,
        "tailscaleInstalled": ts_installed,
        "tailscaleConnected": ts_connected,
        "tailscaleActive": use_ts_mode and bool(ts_ifaces),
        "tailscaleInterfaces": ts_ifaces_all,
        "tailscaleAllowTargets": len(ts_targets) if use_ts_mode else 0,
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
        use_ts_mode,
        wg_ok,
        open_tunnel,
        active_name,
        wg_ifaces,
        ts_installed,
        ts_connected,
        ts_ifaces,
        len(ts_targets),
        ok,
        unhealthy,
    ]
    if state.get("lastSig") != sig:
        log(
            f"enabled={enabled} allowTS={allow_ts} ts_mode={use_ts_mode} "
            f"wg={active_name or '-'} wg_if={wg_ifaces or '-'} "
            f"ts_if={ts_ifaces or '-'} ts_targets={len(ts_targets)} "
            f"ready={open_tunnel} unhealthy={unhealthy} pf={ok}"
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
    state["tsTargetsAt"] = 0
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
                    "tailscaleAllowTargets": 0,
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
