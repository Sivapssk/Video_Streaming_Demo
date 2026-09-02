#!/bin/bash
# Dual-UPF gNB launcher (USRP B210) - new OAI build
# Conf: gnb-dualupf.conf (min_rxtxtime=6, do_SRS="periodic", enable_sdap=0)

# --------------------------------------------------
# 1. Start 5G Core
# --------------------------------------------------
echo "Starting 5G Core..."

cd ~/oai-cn5g
sudo docker compose up -d

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to start 5G Core"
    exit 1
fi

echo "5G Core started."
sleep 5

# --------------------------------------------------
# 2. Add routes
# --------------------------------------------------
echo "Adding routes..."

echo 12345 | sudo -S ip route add 10.0.0.0/24 via 192.168.70.134 dev oai-cn5g 2>/dev/null
echo 12345 | sudo -S ip route add 10.0.2.0/24 via 192.168.70.140 dev oai-cn5g 2>/dev/null

# --------------------------------------------------
# 3. Kill any existing gNB
# --------------------------------------------------
echo "Stopping existing nr-softmodem..."

sudo pkill -9 nr-softmodem 2>/dev/null
sleep 2

# --------------------------------------------------
# 4. Start gNB
# --------------------------------------------------
echo "Starting gNB..."

cd ~/nr-dualupf

sudo ./nr-softmodem \
    -O gnb-dualupf.conf \
    --gNBs.[0].min_rxtxtime 6 \
    -E \
    --continuous-tx
