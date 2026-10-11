#!/usr/bin/env bash
# Generates a Kalshi API key pair (RSA 2048-bit) for request signing.
#
# RUN THIS YOURSELF on the Oracle instance (never on a machine you don't own):
#     ./generate_keys.sh
#
# Then:
#   1. Upload kalshi-public.pem at kalshi.com -> Settings -> API -> "Add API key".
#   2. Kalshi shows you a KEY ID — copy it.
#   3. export KALSHI_KEY_ID=<that key id>
#      (KALSHI_KEY_PATH is already set in kalshi-bot.service)
#
# The private key NEVER leaves this machine and is NEVER committed to git
# (.gitignore excludes *.pem).
set -euo pipefail

KEYDIR="${1:-$HOME/.kalshi}"
mkdir -p "$KEYDIR"
chmod 700 "$KEYDIR"

openssl genrsa -out "$KEYDIR/kalshi-private.pem" 2048
chmod 600 "$KEYDIR/kalshi-private.pem"
openssl rsa -in "$KEYDIR/kalshi-private.pem" -pubout -out "$KEYDIR/kalshi-public.pem"

echo
echo "Private key: $KEYDIR/kalshi-private.pem  (NEVER share, NEVER commit)"
echo "Public key:  $KEYDIR/kalshi-public.pem   -> upload at kalshi.com -> Settings -> API"
echo
echo "After Kalshi gives you the Key ID, set it for the service:"
echo "  sudo systemctl edit kalshi-bot   # add: Environment=KALSHI_KEY_ID=<key-id>"
echo "  sudo systemctl restart kalshi-bot"
