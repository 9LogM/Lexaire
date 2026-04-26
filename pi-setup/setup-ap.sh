#!/usr/bin/env bash
# Configure the Pi's wlan0 as a WPA2 access point that the ground station
# connects to directly, no router in between. Uses NetworkManager (standard
# on Pi OS Bookworm and newer). Idempotent — re-running replaces the
# existing connection with updated settings.
#
# Runs on the Pi, not on the ground station. Prefer invoking it over
# ethernet or from a console session, because enabling AP mode on wlan0
# drops any existing WiFi association — if you're SSH'd in over WiFi,
# the shell dies mid-run.
#
# Usage:
#     sudo ./pi-setup/setup-ap.sh
#     SSID=my-drone PASSWORD=my-secret sudo -E ./pi-setup/setup-ap.sh
#
# One-liner from the ground station (pipes the script to the Pi's shell,
# no pre-copy required):
#     ssh orbis@drone.lan "sudo bash -s" < pi-setup/setup-ap.sh
#
# After a successful run, join the printed SSID from the laptop and set
# the drone hostname in common/config.yaml to the printed Pi IP.

set -euo pipefail

DEFAULT_PASSWORD="lexaire-drone"

SSID="${SSID:-drone-ap}"
PASSWORD="${PASSWORD:-$DEFAULT_PASSWORD}"
CON_NAME="${CON_NAME:-drone-ap}"
CHANNEL="${CHANNEL:-6}"
IFACE="${IFACE:-wlan0}"

if [[ $EUID -ne 0 ]]; then
    echo "Must run as root: sudo $0" >&2
    exit 1
fi

if [[ "$PASSWORD" == "$DEFAULT_PASSWORD" && "${FORCE:-}" != "1" ]]; then
    echo "Refusing to bring up the AP with the documented default password." >&2
    echo "Set PASSWORD=<your-secret> or FORCE=1 to override." >&2
    echo "Example: PASSWORD=correct-horse-battery-staple sudo -E $0" >&2
    exit 2
fi

if ! command -v nmcli >/dev/null 2>&1; then
    echo "nmcli not found. This script requires NetworkManager (Pi OS Bookworm+)." >&2
    echo "On older Pi OS with dhcpcd+wpa_supplicant, use a different setup path." >&2
    exit 1
fi

if ! iw dev "$IFACE" info >/dev/null 2>&1; then
    echo "Interface '$IFACE' not found or not a wireless device." >&2
    exit 1
fi

# Delete any prior connection with the same name so re-runs pick up new args.
nmcli connection delete "$CON_NAME" >/dev/null 2>&1 || true

nmcli connection add \
    type wifi \
    ifname "$IFACE" \
    con-name "$CON_NAME" \
    autoconnect yes \
    ssid "$SSID"

# ipv4.method=shared makes NetworkManager run its built-in dnsmasq: the AP
# hands out IPs on 10.42.0.0/24 by default with the Pi at 10.42.0.1.
nmcli connection modify "$CON_NAME" \
    802-11-wireless.mode ap \
    802-11-wireless.band bg \
    802-11-wireless.channel "$CHANNEL" \
    ipv4.method shared \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.proto rsn \
    wifi-sec.pairwise ccmp \
    wifi-sec.group ccmp \
    wifi-sec.psk "$PASSWORD"

nmcli connection up "$CON_NAME"

ip_cidr="$(nmcli -g IP4.ADDRESS connection show "$CON_NAME" | head -1)"
ip_only="${ip_cidr%/*}"

echo
echo "AP up on $IFACE."
echo "  SSID:     $SSID"
echo "  Password: $PASSWORD"
echo "  Pi IP:    ${ip_only:-<none yet>}"
echo
echo "Ground station: join SSID '$SSID', then set Lexaire's drone hostname"
echo "to the IP above in common/config.yaml."
