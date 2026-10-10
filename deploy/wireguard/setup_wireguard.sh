#!/usr/bin/env bash
# Creates a WireGuard tunnel so the phone can reach the PC from outside, and
# nothing else. Run on the PC:
#
#   sudo ./deploy/wireguard/setup_wireguard.sh <public-ip-or-dyndns-name> [port]
#
# Then forward UDP <port> (default 51820) on the router to this PC, and scan the
# QR code with the WireGuard app on the phone.
# First check that you have a public IP: compare the WAN address on the router's
# status page with what a "what is my IP" site shows. If they differ (CGNAT),
# skip this; summaries arrive by SMS and the app works on home Wi-Fi.
set -euo pipefail

ENDPOINT="${1:?usage: setup_wireguard.sh <public-ip-or-name> [port]}"
PORT="${2:-51820}"
NET=10.8.0
CONF=/etc/wireguard/wg0.conf
OUT="$(cd "$(dirname "$0")" && pwd)/phone.conf"

[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
command -v wg >/dev/null || { apt-get update && apt-get install -y wireguard qrencode; }

if [ -f "$CONF" ]; then
  echo "$CONF already exists; not overwriting."
  exit 1
fi

umask 077
PC_KEY=$(wg genkey);    PC_PUB=$(echo "$PC_KEY" | wg pubkey)
PHONE_KEY=$(wg genkey); PHONE_PUB=$(echo "$PHONE_KEY" | wg pubkey)

cat > "$CONF" <<CONF
[Interface]
Address = $NET.1/24
ListenPort = $PORT
PrivateKey = $PC_KEY

[Peer]
# Android phone
PublicKey = $PHONE_PUB
AllowedIPs = $NET.2/32
CONF

# The phone only routes traffic for the PC through the tunnel.
cat > "$OUT" <<CONF
[Interface]
Address = $NET.2/32
PrivateKey = $PHONE_KEY

[Peer]
PublicKey = $PC_PUB
Endpoint = $ENDPOINT:$PORT
AllowedIPs = $NET.1/32
PersistentKeepalive = 25
CONF

systemctl enable --now wg-quick@wg0
echo
echo "Tunnel up. PC address inside the tunnel: http://$NET.1:8765  (enter in the app)"
echo "Phone config: $OUT  (delete it after importing)"
command -v qrencode >/dev/null && qrencode -t ansiutf8 < "$OUT"
echo
echo "Now forward UDP $PORT on the router to this PC. No other port is opened."
