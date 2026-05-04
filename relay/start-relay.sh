#!/usr/bin/env bash
set -euo pipefail

# These come from compose env (DOCKER_HOST=ssh:// passes them through to
# the Pi-side daemon). An empty value would write a half-broken
# UartEndpoint config and mavlink-router would error opaquely on stdin.
# Fail fast with a clear message instead.
: "${SERIAL_DEVICE:?SERIAL_DEVICE must be set (passed from TUI/compose)}"
: "${SERIAL_BAUD:?SERIAL_BAUD must be set (passed from TUI/compose)}"

CONFIG="/tmp/lexaire-mlr.conf"

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
