#!/usr/bin/env python3
import subprocess
import time

RATES = ["1800kbit", "1500kbit", "700kbit", "900kbit"]

def run_tc(cmd):
    result = subprocess.run(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:
        print("[TC] ERROR:", result.stderr.strip())

    return result.returncode


# Create TBF once
cmd = (
    "docker exec oai-upf "
    "tc qdisc replace dev eth0 root tbf "
    "rate 1800kbit burst 32k latency 100ms"
)

run_tc(cmd)
print("[TC] Initial rate: 1800kbit")

i = 0

try:
    while True:
        rate = RATES[i % len(RATES)]

        cmd = (
            "docker exec oai-upf "
            "tc qdisc change dev eth0 root tbf "
            f"rate {rate} burst 32k latency 100ms"
        )

        if run_tc(cmd) == 0:
            print("[TC] Applied:", rate)

        time.sleep(10)
        i += 1

except KeyboardInterrupt:
    print("\n[TC] Stopped.")
    print("[TC] Current TC rule remains active.")
    print("[TC] Delete with:")
    print("docker exec oai-upf tc qdisc del dev eth0 root")
