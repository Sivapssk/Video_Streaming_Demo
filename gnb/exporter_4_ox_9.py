#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
exporter_4_ox_6.py — ZTAN controller with socat + UPF1 probe + UPF1&UPF2 ext-DN + video recovery time

Changes from _5:
  - UPF2 ext-DN measurement added (docker exec oai-upf2 iperf3 -c extDN -p 5202)
  - Video recovery time: time from steer_to() to first clear measurement
  - Both UPF1 and UPF2 ext-DN measured every cycle

    gNB FFmpeg  ──udp://127.0.0.1:5006──►  socat  ──udp──►  UPF1 or UPF2

Prometheus  :8000
"""

import subprocess
import json
import time
import os
import re
import sys
import signal
import shutil
import matplotlib
matplotlib.use("Agg")
from prometheus_client import start_http_server, Gauge

# =========================
# Prometheus Metrics
# =========================

tp_g = Gauge("iperf_throughput_mbps", "UE Throughput (Mbps)")
jitter_g = Gauge("iperf_jitter_ms", "UE Jitter (ms)")
loss_g = Gauge("iperf_packet_loss_percent", "UE Packet Loss (%)")
lat_g = Gauge("ping_rtt_ms", "UE Latency (ms)")

cong_g = Gauge("network_congestion_index", "ZTAN Congestion Index")
loop_g = Gauge("ztan_loop_state", "0=open, 1=closed")

upf1_extdn_tp_g = Gauge("upf1_extdn_throughput_mbps",
                         "UPF1 to extDN Throughput (Mbps)")
upf2_extdn_tp_g = Gauge("upf2_extdn_throughput_mbps",
                         "UPF2 to extDN Throughput (Mbps)")

recovery_cmd_g = Gauge("ztan_recovery_command_ms",
                        "Time taken to execute the UPF recovery command, in ms")
recovery_cmd_g.set(0)

selected_upf_g = Gauge("ztan_selected_upf", "Active UPF (1 or 2)")
selected_upf_g.set(1)
steering_time_ms_g = Gauge("ztan_steering_time_ms",
                           "Time to retarget socat to the other UPF, in ms")
steering_time_ms_g.set(0)

video_recovery_g = Gauge("ztan_video_recovery_ms",
                         "Time from steer to video clear (ms)")
video_recovery_g.set(0)

upf1_probe_cong_g = Gauge("upf1_probe_congestion",
                          "UPF1 background probe congestion index")

# =========================
# Configuration
# =========================

EXTDN_IP = "192.168.70.135"
VIDEO_FILE = "/home/iiitb/oai-cn5g/demo_3.mp4"
VIDEO_PORT = 5006
LOCAL_PROXY = "127.0.0.1"

BITRATES = {
    "LOW": "800k",
    "MEDIUM": "1200k",
    "HIGH": "1800k"
}

CONGESTION_THRESHOLD = 0.2
CONGESTION_WINDOW = 2

STEER_THRESHOLD = 0.4
STEER_RETURN = 0.15
STEER_UP_WINDOW = 2
STEER_DOWN_WINDOW = 5

current_profile = "HIGH"
active_upf = 1
active_ip = None
active_port = 5201
ffmpeg_process = None
socat_process = None

UE_IP = None
UE_IP_UPF2 = None

t_steered = None


# =========================
# IP Detection
# =========================

def get_ue_ip_from_upf1():
    result = subprocess.run(
        ["docker", "logs", "oai-upf"],
        capture_output=True, text=True
    )
    matches = re.findall(r"\|\s*(10\.0\.0\.\d+)\s*\|", result.stdout)
    if matches:
        ip = matches[-1]
        print(f"[IP] UE tunnel IP from UPF1: {ip}")
        return ip
    print("[IP] Could not find UE IP in UPF1 logs")
    return None


def get_ue_ip_from_upf2():
    result = subprocess.run(
        ["docker", "logs", "oai-upf2"],
        capture_output=True, text=True
    )
    matches = re.findall(r"\|\s*(10\.0\.2\.\d+)\s*\|", result.stdout)
    if matches:
        ip = matches[-1]
        print(f"[IP] UE tunnel IP from UPF2: {ip}")
        return ip
    print("[IP] Could not find UE IP in UPF2 logs")
    return None


# =========================
# FFmpeg (never restarted on steer)
# =========================

def start_ffmpeg(bitrate):
    global ffmpeg_process

    if ffmpeg_process:
        ffmpeg_process.send_signal(signal.SIGINT)
        try:
            ffmpeg_process.wait(timeout=2)
        except Exception:
            ffmpeg_process.kill()

    cmd = [
        "ffmpeg",
        "-stream_loop", "-1",
        "-re",
        "-i", VIDEO_FILE,
        "-vcodec", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-b:v", bitrate,
        "-f", "mpegts",
        f"udp://{LOCAL_PROXY}:{VIDEO_PORT}"
    ]
    ffmpeg_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[FFMPEG] Streaming at {bitrate} -> {LOCAL_PROXY}:{VIDEO_PORT} (local socat proxy)")


# =========================
# socat UDP Proxy
# =========================

def start_socat(dest_ip):
    global socat_process
    if socat_process:
        stop_socat()

    cmd = [
        "socat",
        f"UDP4-LISTEN:{VIDEO_PORT},reuseaddr",
        f"UDP4:{dest_ip}:{VIDEO_PORT}"
    ]
    socat_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[SOCAT] Forwarding 127.0.0.1:{VIDEO_PORT} -> {dest_ip}:{VIDEO_PORT}")


def stop_socat():
    global socat_process
    if socat_process:
        try:
            socat_process.terminate()
            socat_process.wait(timeout=2)
        except Exception:
            try:
                socat_process.kill()
            except Exception:
                pass
        socat_process = None


# =========================
# Cleanup
# =========================

def signal_handler(sig, frame):
    print("\n[ZTAN] Ctrl+C detected.")
    cleanup()
    sys.exit(0)


def cleanup():
    global ffmpeg_process
    print("\n[ZTAN] Shutting down cleanly...")
    stop_socat()
    if ffmpeg_process is not None:
        try:
            ffmpeg_process.terminate()
            ffmpeg_process.wait(timeout=3)
        except Exception:
            try:
                ffmpeg_process.kill()
            except Exception:
                pass
    print("[ZTAN] All processes stopped.")


# =========================
# Network Measurement (for a given UPF target)
# =========================

def measure_upf(target_ip, port):
    cmd = [
        "iperf3",
        "-c", target_ip,
        "-u",
        "-b", "2M",
        "-t", "2",
        "-p", str(port),
        "--json"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7)
        data = json.loads(result.stdout)
        stats = data["end"]["sum_received"]
        tp = stats["bits_per_second"] / 1e6
        jitter = stats["jitter_ms"]
        loss = stats["lost_percent"]

        ping_out = subprocess.check_output(
            ["ping", "-c", "3", target_ip],
            timeout=5
        ).decode()
        rtts = [float(x.split("time=")[1].split()[0])
                for x in ping_out.splitlines() if "time=" in x]
        lat = sum(rtts) / len(rtts)
        return tp, jitter, loss, lat
    except Exception:
        return None, None, None, None


# =========================
# UPF1 <-> extDN Measurement (-b 5M, port 5201)
# =========================

def measure_upf1_extdn():
    cmd = [
        "docker", "exec", "oai-upf",
        "iperf3",
        "-c", EXTDN_IP,
        "-p", "5201",
        "-b", "5M",
        "-t", "1",
        "--json"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if not data.get("end"):
            return None
        tp = data["end"]["sum_received"]["bits_per_second"] / 1e6
        return tp
    except Exception:
        return None


# =========================
# UPF2 <-> extDN Measurement (-b 5M, port 5202)
# =========================

def measure_upf2_extdn():
    cmd = [
        "docker", "exec", "oai-upf2",
        "iperf3",
        "-c", EXTDN_IP,
        "-p", "5202",
        "-b", "5M",
        "-t", "1",
        "--json"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if not data.get("end"):
            return None
        tp = data["end"]["sum_received"]["bits_per_second"] / 1e6
        return tp
    except Exception:
        return None


# =========================
# Congestion Logic
# =========================

def compute_congestion(tp, jitter, loss):
    c_tp = max(0, (2 - tp) / 2)
    c_j = min(jitter / 30, 1)
    c_l = min(loss / 5, 1)
    return (c_tp + c_j + c_l) / 3


# =========================
# Recovery (legacy)
# =========================

ENABLE_TC_RECOVERY = False

def recover_network():
    print("[RECOVERY] Removing UPF shaping")
    subprocess.run(
        ["docker", "exec", "oai-upf",
         "tc", "qdisc", "del", "dev", "eth0", "root"],
        stderr=subprocess.DEVNULL
    )


# =========================
# Steering (restarts socat only, NOT ffmpeg)
# =========================

def steer_to(new_upf):
    global active_upf, active_ip, active_port, t_steered
    t1 = time.perf_counter()
    active_upf = new_upf
    if new_upf == 2:
        active_ip = UE_IP_UPF2
        active_port = 5202
    else:
        active_ip = UE_IP
        active_port = 5201
    stop_socat()
    start_socat(active_ip)
    t2 = time.perf_counter()
    ms = (t2 - t1) * 1000
    t_steered = time.time()
    selected_upf_g.set(active_upf)
    steering_time_ms_g.set(ms)
    print(f"[STEER] Moved to UPF{new_upf} ({active_ip}:{active_port}) in {ms:.1f}ms")


# =========================
# Main Controller
# =========================

def main():
    global current_profile, active_ip, active_upf, active_port, UE_IP, UE_IP_UPF2, t_steered

    force_steer = "--force-steer" in sys.argv

    if not shutil.which("socat"):
        print("[FATAL] socat not found. Install: sudo apt install socat")
        sys.exit(1)

    ue_ip1 = get_ue_ip_from_upf1()
    ue_ip2 = get_ue_ip_from_upf2()

    if ue_ip1 is None or ue_ip2 is None:
        print("[FATAL] Could not resolve both tunnel IPs. Is the UE attached?")
        sys.exit(1)

    UE_IP = ue_ip1
    UE_IP_UPF2 = ue_ip2
    active_ip = ue_ip1

    print("[ZTAN] Controller Started (socat + UPF1 probe + UPF1&UPF2 ext-DN + recovery time)")
    if force_steer:
        print("[ZTAN] --force-steer ENABLED: simulating CONG at T+5s, recovery at T+25s")
    print(f"[IP]   UPF1 target: {ue_ip1}:5201    UPF2 target: {ue_ip2}:5202")

    signal.signal(signal.SIGINT, signal_handler)
    start_http_server(8000)

    congestion_counter = 0
    closed_loop = False
    steer_counter = 0
    start_ffmpeg(BITRATES[current_profile])
    start_socat(ue_ip1)
    force_cong_active = False
    t_start = time.time()

    upf1_cong = 0.0

    try:
        while True:

            tp, jitter, loss, lat = measure_upf(active_ip, active_port)
            if tp is None:
                time.sleep(1)
                continue

            upf1_tp = measure_upf1_extdn()
            upf2_tp = measure_upf2_extdn()

            if upf1_tp is not None:
                upf1_extdn_tp_g.set(upf1_tp)
            if upf2_tp is not None:
                upf2_extdn_tp_g.set(upf2_tp)

            if force_steer:
                elapsed_s = time.time() - t_start
                if 5 <= elapsed_s < 25:
                    tp = 0.5
                    jitter = 35.0
                    loss = 40.0
                    force_cong_active = True
                elif elapsed_s >= 25:
                    force_cong_active = False

            active_cong = compute_congestion(tp, jitter, loss)

            # ---- Video recovery time ----
            if t_steered is not None and active_cong < STEER_RETURN:
                recovery_ms = (time.time() - t_steered) * 1000
                video_recovery_g.set(recovery_ms)
                print(f"[RECOVERY] Video recovery in {recovery_ms:.0f}ms")
                t_steered = None

            if active_upf == 2:
                probe_tp, probe_j, probe_l, probe_lat = measure_upf(UE_IP, 5201)
                if probe_tp is not None:
                    upf1_cong = compute_congestion(probe_tp, probe_j, probe_l)
                    print(f"[PROBE] UPF1 cong={upf1_cong:.2f} (tp={probe_tp:.2f}, j={probe_j:.2f}, l={probe_l:.2f}%)")
                else:
                    print("[PROBE] UPF1 probe failed")
                # upf1_probe_cong_g.set(upf1_cong)
                # upf2_probe_cong_g.set(active_cong)
            else:
                # On UPF1: measure UPF1 normally, probe UPF2 only for logging
                upf1_cong = active_cong
            upf1_probe_cong_g.set(upf1_cong)

            tp_g.set(tp)
            jitter_g.set(jitter)
            loss_g.set(loss)
            lat_g.set(lat)
            #cong_g.set(active_cong)
            cong_g.set(upf1_cong)

            if active_cong > CONGESTION_THRESHOLD:
                congestion_counter += 1
            else:
                congestion_counter = 0
            closed_loop = congestion_counter >= CONGESTION_WINDOW
            loop_g.set(1 if closed_loop else 0)

            if closed_loop and ENABLE_TC_RECOVERY:
                t1 = time.perf_counter()
                recover_network()
                t2 = time.perf_counter()
                ms = (t2 - t1) * 1000
                print(f"[RECOVERY COMMAND] {ms:.3f} ms")
                recovery_cmd_g.set(ms)

            if active_cong > STEER_THRESHOLD:
                steer_counter = max(steer_counter, 0) + 1
                if steer_counter >= STEER_UP_WINDOW and active_upf == 1:
                    steer_to(2)
                    steer_counter = 0
            elif active_upf == 2:
                if upf1_cong < STEER_RETURN:
                    steer_counter = min(steer_counter, 0) - 1
                    if steer_counter <= -STEER_DOWN_WINDOW:
                        steer_to(1)
                        steer_counter = 0
                else:
                    steer_counter = 0
                    print(f"[PROBE] UPF1 still congested (cong={upf1_cong:.2f}), staying on UPF2")
            elif active_cong < STEER_RETURN:
                steer_counter = 0
            else:
                steer_counter = 0

            mode = "CLOSED" if closed_loop else "OPEN"
            steer_mark = " <- STEERED" if active_upf == 2 else ""
            upf1_ext_display = f"{upf1_tp:.2f}" if upf1_tp is not None else "N/A"
            upf2_ext_display = f"{upf2_tp:.2f}" if upf2_tp is not None else "N/A"
            force_mark = " [FORCE-CONG]" if force_cong_active else ""

            print(
                f"[{mode}] "
                f"UE={tp:.2f} Mbps | "
                f"UPF1_ext={upf1_ext_display} | UPF2_ext={upf2_ext_display} | "
                f"J={jitter:.2f} | "
                f"Loss={loss:.2f}% | "
                f"RTT={lat:.2f} | "
                f"CONG={active_cong:.2f} | "
                f"UPF1_probe={upf1_cong:.2f} | "
                f"Profile={current_profile} | "
                f"Active=UPF{active_upf}:{active_port}{steer_mark}{force_mark}"
            )

            time.sleep(0.2)

    except KeyboardInterrupt:
        print("\n[ZTAN] Keyboard interrupt received.")
    finally:
        cleanup()


if __name__ == "__main__":
    main()