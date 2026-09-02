#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server_live_streaming_ox.py — Dual-session UE launcher (gnome-terminal)

Adapted from server_live_streaming.py for the dual-PDU-session setup.
Opens one terminal per service so each stream is easy to monitor.

    Terminal 1:  iperf  sink for UPF1 (10.0.0.x)
    Terminal 2:  iperf  sink for UPF2 (10.0.2.x)
    Terminal 3:  routes + policy routing + ping gNB
    Terminal 4:  iperf3 server (for exporter measurement)
    Terminal 5:  ffplay video receiver on :5006

Run ON THE UE machine (yogs@172.16.177.7):

    python3 server_live_streaming_ox.py

Leave all terminals open for the whole experiment.
"""

import subprocess
import re
import time
import sys
import os


# =========================
# IP Detection
# =========================

def get_tunnel_ips():
    """Detect both tunnel IPs from the local interfaces."""
    ips = {}
    for iface, subnet in [("oaitun_ue1", "10.0.0"), ("oaitun_ue1p2", "10.0.2")]:
        result = subprocess.run(
            ["ip", "-4", "addr", "show", iface],
            capture_output=True, text=True
        )
        match = re.search(r"inet\s+(10\.0\.\d+\.\d+)", result.stdout)
        if match:
            ips[iface] = match.group(1)
    return ips


def wait_for_tunnels():
    """Block until both tunnel interfaces are up."""
    while True:
        ips = get_tunnel_ips()
        if len(ips) >= 2:
            return ips
        print(f"[WAIT] Found {len(ips)}/2 tunnels — retrying in 3s ...")
        time.sleep(3)


# =========================
# Policy Routing (from server_ox.py)
# =========================

def install_policy_rules(ips):
    """
    Per-source policy routing so kernel-generated REPLIES exit through
    the SAME tunnel their request arrived on.
    """
    mapping = [
        ("oaitun_ue1",   ips.get("oaitun_ue1"),   "100", "oaitun_ue1"),
        ("oaitun_ue1p2", ips.get("oaitun_ue1p2"), "101", "oaitun_ue1p2"),
    ]
    for iface, ip, table, dev in mapping:
        if not ip:
            continue
        cmds = [
            ["rule", "del", "priority", table],
            ["rule", "add", "priority", table, "from", ip, "lookup", table],
            ["route", "replace", "table", table, "default", "dev", dev],
        ]
        for c in cmds:
            subprocess.run(["sudo", "-n", "/usr/sbin/ip"] + c,
                           capture_output=True, text=True)
        print(f"[POLICY] from {ip} -> table {table} via {dev}: installed")

    subprocess.run(["sudo", "-n", "/usr/sbin/ip", "route", "flush", "cache"],
                   capture_output=True)


def clear_stale_sinks():
    """Kill leftover iperf receivers from a previous run."""
    subprocess.run(["pkill", "-f", r"iperf .*-s .*-B 10\.0\."],
                   capture_output=True)
    time.sleep(1)


# =========================
# Terminal Launcher
# =========================

def open_terminal(title, command):
    """Open a gnome-terminal running the given command."""
    subprocess.Popen([
        "gnome-terminal", "--title", title,
        "--", "bash", "-c", f"{command}; exec bash"
    ])
    time.sleep(2)


# =========================
# Main
# =========================

def main():
    print("=" * 60)
    print("server_live_streaming_ox.py — dual-session UE launcher")
    print("=" * 60)

    clear_stale_sinks()
    ips = wait_for_tunnels()

    ip1 = ips["oaitun_ue1"]
    ip2 = ips["oaitun_ue1p2"]
    print(f"[OK] UPF1 IP: {ip1}")
    print(f"[OK] UPF2 IP: {ip2}")

    # Install policy routing rules
    install_policy_rules(ips)

    # Terminal 1: iperf sink for UPF1
    open_terminal(
        "iperf-UPF1",
        f"iperf -s -i 1 -u -B {ip1}"
    )

    # Terminal 2: iperf sink for UPF2
    open_terminal(
        "iperf-UPF2",
        f"iperf -s -i 1 -u -B {ip2}"
    )

    # Terminal 3: routes + ping gNB
    open_terminal(
        "routes+ping",
        f"sudo ip route replace default via 10.0.0.1 dev oaitun_ue1 && "
        f"sudo ip route replace default via 10.0.2.1 dev oaitun_ue1p2 && "
        f"ping 192.168.70.129"
    )

    # Terminal 4: iperf3 server (for exporter measurement — single bound)
    open_terminal(
        "iperf3-server",
        "iperf3 -s"
    )

    # Terminal 5: ffplay video receiver
    open_terminal(
        "video-player",
        "ffplay -fflags nobuffer -flags low_delay udp://@:5006"
    )

    print("\n[OK] All 5 terminals launched. Leave them open.")
    print("[OK] Ctrl+C here to quit (terminals stay open).")


if __name__ == "__main__":
    main()
