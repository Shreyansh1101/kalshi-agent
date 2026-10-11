#!/usr/bin/env bash
# One-command flip from dry-run to LIVE (real money).
#
# Usage:  sudo ./go-live.sh <KALSHI_KEY_ID>
#
# The Key ID comes from kalshi.com -> Settings -> API, after you upload the
# public key. Get the public key with:
#     cat /home/ubuntu/.kalshi/kalshi-public.pem
# (it was generated automatically when the instance booted)
#
# Safety rails stay on in live mode: $4/trade, 3 orders/day, $20 daily
# stop-loss, one position per event, 9AM-9PM ET, live orders re-check the
# quote and expire after 120s.
set -euo pipefail

KEY_ID="${1:?Usage: sudo ./go-live.sh <KALSHI_KEY_ID>}"
KEY_PATH="/home/ubuntu/.kalshi/kalshi-private.pem"

[ "$(id -u)" = "0" ] || { echo "ERROR: run with sudo: sudo ./go-live.sh <KALSHI_KEY_ID>"; exit 1; }
[ -f "$KEY_PATH" ] || { echo "ERROR: private key not found at $KEY_PATH"; exit 1; }

mkdir -p /etc/systemd/system/kalshi-bot.service.d
cat > /etc/systemd/system/kalshi-bot.service.d/live.conf <<EOF
[Service]
Environment=KALSHI_KEY_ID=$KEY_ID
Environment=DRY_RUN=false
EOF
chmod 600 /etc/systemd/system/kalshi-bot.service.d/live.conf

systemctl daemon-reload
systemctl restart kalshi-bot
sleep 4

echo "--- bot mode ---"
journalctl -u kalshi-bot -n 12 --no-pager | grep -E "MODE" || journalctl -u kalshi-bot -n 6 --no-pager
echo
echo "If MODE shows LIVE above, the bot is placing REAL orders with REAL money."
echo "Back to dry-run any time: sudo ./go-dry-run.sh"
