# Kalshi Auto-Trading Bot

Polls the divergence strategy every 60 seconds and trades it automatically.
Strategy, fee math, and reference prices are shared with the paper-signal
engine (`signal_engine.py`, vendored from the paper scanner) — only the
execution layer is new.

> **DRY-RUN BY DEFAULT.** Out of the box the bot only *logs* what it would
> trade (`status: "dry-run"` in `ledger.json`) and never places a real order.
> Flipping to live is a deliberate manual step (see §6). Paper-first until
> the Oct 30 review.

## 0. Cost: $0 on your Oracle Cloud

This is designed for a **second VM.Standard.E2.1.Micro** instance. Oracle's
Always Free tier includes **2× E2.1.Micro** (1 OCPU / 1 GB RAM each), so with
one already running, the second one stays free — nothing about your current
instance's pricing changes.

It also fits the storage allowance: 2 instances × 50 GB default boot volume
= 100 GB, inside the 200 GB Always Free block-volume allowance.

(We use E2.1.Micro rather than the ARM A1.Flex shape on purpose: A1 capacity
is frequently unavailable, and its Always Free allowance was halved in June
2026 to 2 OCPU / 12 GB total. The bot needs almost nothing — 1 GB RAM is
plenty. The 60-second polling also keeps the instance active, so there is no
idle-reclaim risk.)

## 1. Code location

The code is already pushed to your repo: `https://github.com/Shreyansh1101/kalshi-agent`.

There are **no secrets in this repo by design** — the Kalshi private key is
placed on the instance out-of-band (§4). `.gitignore` excludes `*.pem`,
`.venv/`, and the runtime `ledger.json`/`state.json`.

## 2. Create the instance (self-installing) — the whole deploy

1. Oracle console → Compute → Create instance:
   - **Shape:** `VM.Standard.E2.1.Micro` (Always Free)
   - **Image:** Ubuntu 24.04
   - **Networking:** your VCN, public subnet, *assign a public IPv4 address*,
     add your SSH public key
   - **Management → Initialization script:** paste the whole `cloud-init.yaml`
2. Create, wait ~3–5 minutes for first boot.

That's the entire deploy. The bot starts in **DRY-RUN** (logs what it would
trade, places no orders — dry-run needs no API keys), and an API key pair is
generated automatically at boot (private key never leaves the instance).

## 3. Verify (optional)

```bash
ssh ubuntu@<instance-ip>
journalctl -u kalshi-bot -f
```

You should see `MODE: DRY-RUN (no real orders)` and a `scan done:` line every
60 seconds between 9 AM and 9 PM ET. Outside that window it sleeps.

```bash
cat ~/kalshi-bot/ledger.json   # dry-run entries accumulate here
```

## 4. Phone notifications (Telegram — recommended)

The bot pushes to your phone on every signal (dry-run) and on every live
order or order failure. Telegram is the reliable path on iOS (ntfy.sh push
proved unreliable). One-time setup:

1. In Telegram, message **@BotFather** → `/newbot` → copy the token.
2. Open a chat with your new bot and send it any message.
3. Get your chat id: open `https://api.telegram.org/bot<TOKEN>/getUpdates`
   and copy `"chat":{"id": ...}`.
4. On the instance:
   ```bash
   printf 'TELEGRAM_BOT_TOKEN=<token>\nTELEGRAM_CHAT_ID=<chat id>\n' > ~/.kalshi/notify.env
   chmod 600 ~/.kalshi/notify.env
   sudo systemctl restart kalshi-bot
   ```
5. You'll get a "Kalshi bot started" message in Telegram within seconds —
   that confirms notifications work.

To silence dry-run pushes later (live orders always notify), add
`Environment=NOTIFY_DRY_RUN=false` to a systemd override file.

(Old ntfy.sh path: set `NTFY_TOPIC` — kept as a fallback, but not
recommended on iOS.)

## 5. Going LIVE (real money) — only when YOU decide

> ⚠️ **LIVE mode places real orders with real money.** Start small. The Oct 30
> paper/dry-run review is the decision point — do not flip this before then.
> Never trade money you can't afford to lose. You can flip back to dry-run at
> any time.

Three steps, all on the instance:

```bash
# 1. Print the public key (generated automatically at boot):
cat /home/ubuntu/.kalshi/kalshi-public.pem
```

2. Upload that public key at **kalshi.com → Settings → API → Add API key**.
   Kalshi shows a **Key ID** — copy it.

```bash
# 3. One command flips the bot live:
sudo ~/kalshi-bot/go-live.sh <paste-the-Key-ID>
# confirm: MODE: *** LIVE — REAL MONEY ***
```

Back to dry-run any time: `sudo ~/kalshi-bot/go-dry-run.sh`

Safety rails that stay on in live mode: $10 max per trade, uncapped orders/day
but max $20 at risk at any moment (the stop counts open positions, so it
can't be outrun by simultaneous trades), one position per event, 9 AM–9 PM ET
window only, and every live order re-checks the quote first — if the price
moved more than 2¢ from the signal, it skips. After the $20 stop trips, the
bot keeps sending signals info-only (no orders). Live limit orders expire
after 120 seconds so nothing rests stale. Any open position whose net-at-bid
P&L reaches +30% of its stake is sold early to lock in profit; any position
down 30% net is cut early to stop the bleed (both after entry fee, exit fee
and the spread). Exits run even after the stop trips, since they only
reduce risk.

Note: the key pair lives on the instance — if you ever delete and recreate
it, repeat steps 1–2 (new key pair, new upload).

## 6. Manual install (alternative to cloud-init)

```bash
ssh ubuntu@<instance-ip>
sudo apt-get update && sudo apt-get install -y python3 python3-venv git
git clone https://github.com/Shreyansh1101/kalshi-agent.git ~/kalshi-bot
python3 -m venv ~/kalshi-bot/.venv
~/kalshi-bot/.venv/bin/pip install -r ~/kalshi-bot/requirements.txt
chmod +x ~/kalshi-bot/generate_keys.sh ~/kalshi-bot/go-live.sh ~/kalshi-bot/go-dry-run.sh
~/kalshi-bot/generate_keys.sh   # creates the API key pair on the instance
sudo cp ~/kalshi-bot/kalshi-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now kalshi-bot
```

## 7. Configuration

All via environment variables (in the service file or `systemctl edit`):

| Variable | Default | Meaning |
|---|---|---|
| `DRY_RUN` | `true` | `true` = log only; `false` = real orders |
| `POLL_SECONDS` | `60` | seconds between scans |
| `MAX_STAKE_PER_TRADE` | `1000` | cents ($10.00) max per order |
| `MAX_SIGNALS_PER_DAY` | `0` | max orders per day (ET); `0` = uncapped |
| `DAILY_STOP_LOSS_CENTS` | `2000` | cents ($20) — counts open exposure too, so max $20 is ever at risk; after halt, signals continue info-only |
| `TAKE_PROFIT_PCT` | `0.30` | fraction of stake (30%) — sell any open position early once its net-at-bid profit (after entry fee, exit fee and spread) reaches this; runs every scan, even when halted |
| `STOP_LOSS_PCT` | `0.30` | fraction of stake (30%) — cut any open position early once its net-at-bid loss reaches this; runs every scan, even when halted |
| `MIN_DIVERGENCE_CENTS` | `2` | min Kalshi-vs-reference gap to trade |
| `KALSHI_KEY_ID` | — | from kalshi.com after uploading public key |
| `KALSHI_KEY_PATH` | `~/.kalshi/kalshi-private.pem` | private key file (600) |

## 8. How it works

Each scan: settle/mark open positions from Kalshi → evaluate every watchlist
pair (Kalshi quote vs Polymarket reference, same math as the paper engine) →
strongest divergence per event wins → risk checks (daily cap, stop-loss,
one-position-per-event) → dry-run: append a `"dry-run"` ledger entry;
live: re-fetch the quote, place a limit order at the ask only if it's still
within 2¢ of the signal price, record the order id/fill.

## 9. Troubleshooting

- `journalctl -u kalshi-bot` — everything the bot prints lands here.
- Service won't start: `systemctl status kalshi-bot`; usual cause is a bad
  path in `kalshi-bot.service` if you cloned somewhere other than
  `/home/ubuntu/kalshi-bot`.
- `KALSHI_KEY_ID` / key errors in live mode: re-check §4; the private key
  file must be readable by the `ubuntu` user (`chmod 600`).
- Oracle says "out of capacity" for E2.1.Micro: retry in another availability
  domain of your home region; E2 capacity is usually fine.
- To update the bot: `cd ~/kalshi-bot && git pull && sudo systemctl restart kalshi-bot`.
  If the paper strategy changes, update `signal_engine.py` in the repo
  (it is a vendored copy of the paper scanner — keep the provenance header
  at the top), commit, push, then `git pull` on the instance.
