#!/usr/bin/env bash
# Flip back to dry-run (log only, no real orders).
#
# Usage:  sudo ./go-dry-run.sh
set -euo pipefail

[ "$(id -u)" = "0" ] || { echo "ERROR: run with sudo: sudo ./go-dry-run.sh"; exit 1; }

rm -f /etc/systemd/system/kalshi-bot.service.d/live.conf
systemctl daemon-reload
systemctl restart kalshi-bot
sleep 3

echo "--- bot mode ---"
journalctl -u kalshi-bot -n 8 --no-pager | grep -E "MODE" || true
echo "Bot is back in DRY-RUN mode (no real orders)."
