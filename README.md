# ZTAN Dual-UPF 5G System

Zero-Touch Autonomous Networking with dual-UPF steering. When congestion is detected (CONG > 0.4), video traffic automatically shifts from UPF1 to UPF2. Recovery happens when congestion clears (CONG < 0.15).

## Architecture

```
                    ┌─────────────┐
                    │  ext-DN     │
                    │ (iperf3 -s  │
                    │  :5201/5202)│
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
        ┌─────┴─────┐ ┌───┴────┐ ┌────┴────┐
        │  oai-upf  │ │  SMF   │ │ oai-upf2│
        │ (port5201)│ │        │ │(port5202)│
        └─────┬─────┘ └───┬────┘ └────┬────┘
              │            │            │
              └────────────┼────────────┘
                           │ N3 (GTP-U)
                    ┌──────┴──────┐
                    │   gNB       │
                    │ (exporter)  │
                    │ FFmpeg+socat│
                    └──────┬──────┘
                           │ N2/N3
                    ┌──────┴──────┐
                    │     UE      │
                    │ (ipERF/ffplay)│
                    └─────────────┘
```

## Prerequisites

- 3 machines: gNB (172.16.132.157), UE (172.16.177.7), ext-DN runs inside docker
- Docker containers: oai-upf, oai-upf2, oai-smf, oai-amf, oai-ausf, oai-udm, oai-udr, oai-nrf, oai-ext-dn, mysql
- Python venv at `/home/iiitb/dash_env/` with: `prometheus_client`, `matplotlib`
- `socat` installed on gNB: `sudo apt install socat`
- `ffmpeg` installed on gNB
- `iperf3` installed inside ext-DN, oai-upf, oai-upf2 containers

## Setup Flow

### Step 1: Start Core + gNB (on gNB system)

```bash
cd ~/nr-dualupf
./run_core_gnb.sh
```

This will:
1. Start 5G Core (`docker compose up -d`)
2. Add routes for UE tunnel IPs (10.0.0.0/24, 10.0.2.0/24)
3. Kill any existing nr-softmodem
4. Start gNB with `gnb-dualupf.conf`

### Step 2: Attach UE (on UE system)

```bash
cd ~/nr-softmodem-new
sudo ./nr-uesoftmodem -O ~/ue_dual.conf -r 106 --numerology 1 --band 78 -C 3619200000 -E --ue-fo-compensation
```

Wait until UE registers (check AMF logs: `docker logs oai-amf | tail -20`).

### Step 3: Install iperf3 in containers

```bash
# ext-DN
docker exec -it oai-ext-dn bash -c "apt update && apt install -y iperf3"

# UPF1
docker exec -it oai-upf bash -c "apt update && apt install -y iperf3"

# UPF2
docker exec -it oai-upf2 bash -c "apt update && apt install -y iperf3"
```

### Step 4: Start iperf3 servers on ext-DN

```bash
docker exec -d oai-ext-dn iperf3 -s -D -p 5201
docker exec -d oai-ext-dn iperf3 -s -D -p 5202
```

### Step 5: Start UE streaming (on UE system)

```bash
cd ~/nr-softmodem-new
python3 /home/yogs/nr-softmodem-new/server_live_streaming_ox.py
```

This opens 5 terminals:
- iperf sink for UPF1
- iperf sink for UPF2
- Routes + ping gNB
- iperf3 server (for exporter)
- ffplay video receiver on :5006

### Step 6: Start ZTAN exporter (on gNB system)

```bash
sudo /home/iiitb/dash_env/bin/python -u ~/5g_dashboard/exporter/exporter_4_ox_9.py
```

Optional flags:
- `--force-steer` : Simulates congestion spike at T+5s, recovery at T+25s

Prometheus metrics exposed at `http://localhost:8000`.

### Step 7: Start congestion cycle (on gNB system)

```bash
cd ~/oai-cn5g
python3 tc_cycle_1.py
```

Cycles tc rates: 1800kbit -> 1500kbit -> 700kbit -> 900kbit every 10s.

## Manual Commands

### Delete congestion shaping
```bash
docker exec oai-upf tc qdisc del dev eth0 root
```

### Show current TC rule
```bash
docker exec oai-upf tc qdisc show dev eth0
```

### Check UPF1 ext-DN measurement
```bash
docker exec oai-upf iperf3 -c 192.168.70.135 -p 5201 -b 5M -t 1 --json
```

### Check UPF2 ext-DN measurement
```bash
docker exec oai-upf2 iperf3 -c 192.168.70.135 -p 5202 -b 5M -t 1 --json
```

## Grafana Dashboard

Import `dashboard/video_grafana_3.json` into Grafana.

Dashboard UID: `59ebbe82-bd97-4272-9c3e-e8a1f5f171ce`

Panels:
- Row 0: E2E Throughput | Congestion Index | Migration Time | Video Recovery Time
- Row 1: UPF1 ext-DN BW | UPF2 ext-DN BW
- Row 2: E2E Jitter | Packet Loss
- Row 3: E2E Latency | Active UPF

## Key Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| STEER_THRESHOLD | 0.4 | Congestion above this triggers steering to UPF2 |
| STEER_RETURN | 0.15 | Congestion below this triggers return to UPF1 |
| STEER_UP_WINDOW | 2 | Consecutive readings above threshold before steering |
| STEER_DOWN_WINDOW | 5 | Consecutive readings below threshold before returning |
| EXTDN_IP | 192.168.70.135 | ext-DN container IP |
| VIDEO_PORT | 5006 | UDP video port |

## File Structure

```
ztan-dual-upf/
├── README.md
├── gNB/
│   ├── run_core_gnb.sh              # Start core + gNB
│   ├── exporter_4_ox_9.py           # ZTAN controller
│   └── tc_cycle_1.py                # Congestion generator
├── UE/
│   └── server_live_streaming_ox.py  # UE terminal launcher
├── configs/
│   ├── gnb-dualupf.conf            # gNB config (band 78)
│   └── ue-rfsim.conf               # UE config (2 PDU sessions)
└── dashboard/
    └── video_grafana_3.json         # Grafana dashboard
```
