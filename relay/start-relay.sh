#!/usr/bin/env bash
set -e

SERIAL_DEVICE="${SERIAL_DEVICE}"
SERIAL_BAUD="${SERIAL_BAUD}"
CONFIG="/tmp/orbis-mlr.conf"

cat > "$CONFIG" <<EOF
[General]
TcpServerPort=0

[UartEndpoint Pixhawk]
Device=$SERIAL_DEVICE
Baud=$SERIAL_BAUD

[UdpEndpoint QGC]
Mode=Server
Address=0.0.0.0
Port=14550

[UdpEndpoint MAVSDK]
Mode=Server
Address=0.0.0.0
Port=14551
EOF

exec mavlink-routerd -c "$CONFIG"
