#!/usr/bin/env python3
"""
Kalshi auto-trading bot.

Polls the divergence strategy every POLL_SECONDS (default 60s):
each watchlist pair is priced on Kalshi vs its Polymarket reference, and a
divergence >= MIN_DIVERGENCE_CENTS cents triggers an order — subject to the
risk rules below.

    DRY-RUN BY DEFAULT. With DRY_RUN=true (the default) the bot only logs
    what it WOULD trade (ledger entries with status "dry-run") and never
    touches the Kalshi API with credentials. Set DRY_RUN=false to place real
    orders with real money via the kalshi-python SDK (RSA-PSS request
    signing; private key read from KALSHI_KEY_PATH, never from this repo).

Strategy, fee math and reference-price logic are shared with the
paper-signal engine (signal_engine.py, vendored from
~/workspace/kalshi-signals/scanner.py). Only the execution layer is new.
"""
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)          # signal_engine lives next to this file
import signal_engine as eng       # noqa: E402  (vendored paper engine)

# ---------------- configuration (env-overridable) ----------------
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() not in ("0", "false", "no")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
MAX_STAKE_C = float(os.environ.get("MAX_STAKE_PER_TRADE", "1000"))      # $10.00
MAX_SIGNALS_PER_DAY = int(os.environ.get("MAX_SIGNALS_PER_DAY", "0"))   # 0 = no cap
DAILY_STOP_LOSS_C = float(os.environ.get("DAILY_STOP_LOSS_CENTS", "2000"))  # $20.00
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", "0.30"))  # 30% of stake
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", "0.30"))      # 30% of stake
# Exit veto: the reference market must confirm at least this fraction of the
# breached threshold, or the exit is skipped for this scan. Guards against
# trading on stale/bad Kalshi quotes (e.g. post-game prints that haven't
# caught up with the final whistle).
VETO_FRACTION = 0.5
MIN_DIVERGENCE_C = float(os.environ.get("MIN_DIVERGENCE_CENTS", "2"))
SCAN_START_H, SCAN_END_H = 9, 21          # ET trading window, same as paper
STALE_TOLERANCE_C = 2.0                   # live: skip if price moved >2c

# ---------------- push notifications (ntfy.sh + Telegram) ----------------
# ntfy: set NTFY_TOPIC on the instance to get a phone push on every signal/order.
# Topic is a secret — keep it out of the repo (env var, not code).
# Install the ntfy app and subscribe to the topic to receive pushes.
#
# Telegram (recommended for iOS — far more reliable than ntfy push):
#   1. In Telegram, message @BotFather -> /newbot -> copy the token.
#   2. Open a chat with your new bot and send it any message ("hi").
#   3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates and copy your
#      chat id from "chat":{"id": ...}.
#   4. On the instance, write ~/.kalshi/notify.env with:
#        TELEGRAM_BOT_TOKEN=<token from BotFather>
#        TELEGRAM_CHAT_ID=<your chat id>
#      then: chmod 600 ~/.kalshi/notify.env && sudo systemctl restart kalshi-bot
# Either channel is a silent no-op when unset; both can run at once.
def _load_file_env(path):
    """Load KEY=VALUE lines into os.environ without overriding. Missing file OK."""
    try:
        with open(os.path.expanduser(path)) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


_load_file_env("~/.kalshi/notify.env")

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
NOTIFY_DRY_RUN = os.environ.get("NOTIFY_DRY_RUN", "true").lower() not in ("0", "false", "no")

WATCHLIST = os.path.join(HERE, "watchlist.json")
LEDGER = os.path.join(HERE, "ledger.json")
STATE = os.path.join(HERE, "state.json")

# Point the shared engine at the bot's risk parameters.
eng.DIVERGENCE_THRESHOLD_C = MIN_DIVERGENCE_C
eng.MAX_STAKE_C = MAX_STAKE_C

_client = None


def log(msg):
    print(f"[{eng.now_et().isoformat(timespec='seconds')}] {msg}", flush=True)


def telegram_notify(text):
    """Phone push via Telegram Bot API. Silent no-op when token/chat id unset."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception as ex:
        log(f"telegram notify failed: {type(ex).__name__}: {ex}")


def notify(title, msg, priority="default"):
    """Phone push via ntfy.sh and/or Telegram. Each channel is a silent
    no-op when unset, so either (or both) can be configured."""
    if NTFY_TOPIC:
        try:
            import requests
            requests.post(
                f"{NTFY_SERVER}/{NTFY_TOPIC}",
                data=msg.encode("utf-8"),
                headers={"Title": title, "Priority": priority, "Tags": "robot"},
                timeout=10,
            )
        except Exception as ex:
            log(f"notify failed: {type(ex).__name__}: {ex}")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        telegram_notify(f"{title}\n{msg}" if title else msg)


# ---------------- Kalshi authenticated client (live only) ----------------
def get_client():
    """Build the kalshi-python client from env credentials. Live mode only."""
    global _client
    if _client is not None:
        return _client
    try:
        from kalshi_python import Configuration, KalshiClient
    except ImportError:
        raise SystemExit("kalshi-python is not installed. Run: pip install -r requirements.txt")
    key_id = os.environ.get("KALSHI_KEY_ID")
    key_path = os.path.expanduser(os.environ.get("KALSHI_KEY_PATH", ""))
    if not key_id or not key_path:
        raise SystemExit("LIVE mode needs KALSHI_KEY_ID and KALSHI_KEY_PATH env vars. "
                         "Generate a key pair with ./generate_keys.sh and upload the "
                         "public key at kalshi.com -> Settings -> API.")
    with open(key_path) as f:
        private_pem = f.read()
    config = Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
    config.api_key_id = key_id
    config.private_key_pem = private_pem
    _client = KalshiClient(config)
    log("Kalshi authenticated client ready (RSA-PSS signing).")
    return _client


def place_live_order(client, signal, limit_c):
    """Place a real limit order. Returns the SDK Order object."""
    side = "yes" if signal["side"] == "YES" else "no"
    price_c = int(round(limit_c))
    kwargs = dict(
        ticker=signal["ticker"],
        client_order_id="kalshi-bot-" + uuid.uuid4().hex[:16],  # idempotency key
        side=side,
        action="buy",
        type="limit",
        count=signal["contracts"],
        expiration_ts=int(time.time()) + 120,  # don't leave stale resting orders
    )
    if side == "yes":
        kwargs["yes_price"] = price_c
    else:
        kwargs["no_price"] = price_c
    resp = client.create_order(**kwargs)   # KalshiClient forwards to PortfolioApi
    return resp.order if resp else None


def place_live_sell(client, entry, bid_c):
    """Sell an open position to lock in profit. Returns the SDK Order object."""
    side = "yes" if entry["side"] == "YES" else "no"
    price_c = int(round(bid_c))
    kwargs = dict(
        ticker=entry["ticker"],
        client_order_id="kalshi-bot-exit-" + uuid.uuid4().hex[:16],  # idempotency key
        side=side,
        action="sell",
        type="limit",
        count=entry["contracts"],
        expiration_ts=int(time.time()) + 120,  # don't leave stale resting orders
    )
    if side == "yes":
        kwargs["yes_price"] = price_c
    else:
        kwargs["no_price"] = price_c
    resp = client.create_order(**kwargs)
    return resp.order if resp else None


def fresh_quote(ticker):
    """Bypass the engine's per-scan market cache to get a live quote."""
    eng._market_cache.pop(ticker, None)
    m = eng.kalshi_market(ticker)
    return eng.kalshi_quote(m) if m else None


# ---------------- bot ledger: settle + P&L (mirrors paper engine) ----------------
OPEN_STATUSES = ("dry-run", "live-open")


def settle_bot(ledger, errors):
    """Settle finished positions and refresh marks. Mirrors
    signal_engine.settle_and_mark, but for bot statuses ("dry-run",
    "live-open"). Dry-run entries settle hypothetically so the dry-run
    P&L shows what the bot WOULD have made."""
    settled = []
    t = eng.now_et().isoformat()
    for e in ledger:
        if e.get("status") not in OPEN_STATUSES:
            continue
        m = eng.kalshi_market(e["ticker"])
        if not m:
            errors.append(f"{e['ticker']}: market fetch failed during settlement check")
            continue
        q = eng.kalshi_quote(m)
        if q:
            mid = q["yes_mid"] if e["side"] == "YES" else q["no_mid"]
            e["last_mark_c"] = round(e["contracts"] * mid, 2)
            e["last_mark_time"] = t
        if m.get("status") in ("settled", "finalized"):
            # Kalshi marks finished sports markets "finalized"; the result
            # field ("yes"/"no") is authoritative — expiration_value can be a
            # team name (e.g. "Jacksonville") which carries no side info.
            ev = str(m.get("result", "") or m.get("expiration_value", "")).lower()
            win = ((e["side"] == "YES" and ev in ("yes", "1", "true")) or
                   (e["side"] == "NO" and ev in ("no", "0", "false")))
            if ev not in ("yes", "no", "1", "0", "true", "false", ""):
                errors.append(f"{e['ticker']}: unclear result {ev!r} - left open")
                continue
            payout_c = e["contracts"] * 100 if win else 0
            e["status"] = "settled"
            e["settled_at_et"] = t
            e["payout_c"] = payout_c
            e["pnl_c"] = round(payout_c - e["stake_c"] - e["fee_c"], 2)
            settled.append({"id": e["id"], "ticker": e["ticker"], "side": e["side"],
                            "mode": e.get("mode"), "payout_c": payout_c, "pnl_c": e["pnl_c"]})
    return settled


def check_exits(ledger, errors, ref_map=None):
    """Early exits: close any open position whose net-at-bid P&L reaches
    +TAKE_PROFIT_PCT of its stake (take-profit) or -STOP_LOSS_PCT (stop-loss).
    Net means after entry fee, exit fee and spread — the bid is what a real
    sell would actually get. Runs every scan, even when the daily stop has
    halted new entries: exits only reduce risk. In dry-run the exit is
    simulated at the bid; live it places a real sell order.

    Reference veto: the Polymarket reference must confirm at least
    VETO_FRACTION of the breached threshold, or the exit is skipped for this
    scan. This stops the bot trading on stale/bad Kalshi quotes — e.g. a
    post-game print still showing the pre-final-whistle state. Missing
    reference fails open (exit allowed, previous behavior).
    Returns the list of exited entries."""
    if TAKE_PROFIT_PCT <= 0 and STOP_LOSS_PCT <= 0:
        return []
    exited = []
    t = eng.now_et().isoformat()
    for e in ledger:
        if e.get("status") not in OPEN_STATUSES:
            continue
        m = eng.kalshi_market(e["ticker"])  # scan cache: fetched moments ago by settle_bot
        if not m:
            errors.append(f"{e['ticker']}: market fetch failed during exit check")
            continue
        if m.get("status") != "open":
            # Only ever trade on live quotes. Finished ("settled"/"finalized")
            # markets have dead quotes — settle_bot handles them.
            continue
        q = eng.kalshi_quote(m)
        if not q:
            continue
        bid = q["yes_bid"] if e["side"] == "YES" else q["no_bid"]
        stake_c = e.get("stake_c", 0)
        exit_value_c = round(e["contracts"] * bid, 2)
        exit_fee_c = eng.taker_fee_c(e["contracts"], bid / 100.0)
        net_c = exit_value_c - stake_c - e.get("fee_c", 0) - exit_fee_c
        reason = None
        if TAKE_PROFIT_PCT > 0 and net_c >= TAKE_PROFIT_PCT * stake_c:
            reason = "take-profit"
        elif STOP_LOSS_PCT > 0 and net_c <= -STOP_LOSS_PCT * stake_c:
            reason = "stop-loss"
        if not reason:
            continue
        # Reference veto: skip the exit this scan if the reference market
        # doesn't confirm the move (stale/bad Kalshi quote).
        ref_yes_c = (ref_map or {}).get(e["ticker"])
        if ref_yes_c is not None:
            ref_value_c = round(e["contracts"] * (ref_yes_c if e["side"] == "YES"
                                                  else 100.0 - ref_yes_c), 2)
            ref_net_c = ref_value_c - stake_c - e.get("fee_c", 0) - exit_fee_c
            pct = TAKE_PROFIT_PCT if reason == "take-profit" else STOP_LOSS_PCT
            need_c = VETO_FRACTION * pct * stake_c
            confirmed = ref_net_c >= need_c if reason == "take-profit" else ref_net_c <= -need_c
            if not confirmed:
                log(f"exit VETOED ({reason}): {e['ticker']} kalshi net "
                    f"{eng.fmt_money(net_c)} but reference implies "
                    f"{eng.fmt_money(ref_net_c)} — skipping this scan")
                continue
        # --- exit it ---
        e["status"] = "exited"
        e["exited_at_et"] = t
        e["exit_price_c"] = round(bid, 2)
        e["exit_value_c"] = exit_value_c
        e["exit_fee_c"] = exit_fee_c
        e["pnl_c"] = round(net_c, 2)
        e["exit_reason"] = reason
        tag = "take-profit" if reason == "take-profit" else "STOP-LOSS"
        if DRY_RUN:
            msg = (f"DRY-RUN {tag} exit: {e['side']} {e['contracts']}x {e['ticker']} "
                   f"@ {bid:.0f}c, {eng.fmt_money(net_c)} (net of fees)")
            log(msg)
            if NOTIFY_DRY_RUN:
                notify(f"Kalshi bot (dry-run {tag})", msg)
            exited.append(e)
            continue
        # LIVE: real money sell
        try:
            order = place_live_sell(get_client(), e, bid)
        except Exception as ex:
            e["status"] = "live-open"  # revert; keep the position
            for k in ("exited_at_et", "exit_price_c", "exit_value_c",
                      "exit_fee_c", "pnl_c", "exit_reason"):
                e.pop(k, None)
            log(f"LIVE sell FAILED {e['ticker']}: {type(ex).__name__}: {ex}")
            notify("Kalshi bot: SELL FAILED",
                   f"{e['side']} {e['contracts']}x {e['ticker']}: {type(ex).__name__}",
                   priority="high")
            continue
        if order is not None:
            e["exit_order_id"] = getattr(order, "order_id", None)
        msg = (f"LIVE {tag} SELL: {e['side']} {e['contracts']}x {e['ticker']} "
               f"@ {bid:.0f}c, {eng.fmt_money(net_c)} (net of fees)")
        log(msg)
        notify(f"Kalshi bot: {tag} SELL (real money)", msg, priority="high")
        exited.append(e)
    return exited


def bot_daily_pnl(ledger, today):
    """Realized today + unrealized on open bot positions. Mirrors
    signal_engine.daily_pnl_c for bot statuses."""
    pnl = 0.0
    for e in ledger:
        if e.get("status") == "settled" and str(e.get("settled_at_et", ""))[:10] == today:
            pnl += e.get("pnl_c", 0)
        elif e.get("status") == "exited" and str(e.get("exited_at_et", ""))[:10] == today:
            pnl += e.get("pnl_c", 0)
        elif e.get("status") in OPEN_STATUSES:
            pnl += e.get("last_mark_c", e.get("stake_c", 0)) - e.get("stake_c", 0) - e.get("fee_c", 0)
    return round(pnl, 2)


# ---------------- scan ----------------
def evaluate_pairs(errors):
    wl = eng.load_json(WATCHLIST, None)
    if not wl:
        return None
    pairs = []
    team_names = wl.get("team_names", {})
    slug_map = wl.get("slug_code_map", {})
    for cfg in wl.get("dynamic_universes", []):
        try:
            pairs.extend(eng.eval_sports_universe(cfg, team_names, slug_map, errors))
        except Exception as ex:
            errors.append(f"{cfg.get('id')}: {type(ex).__name__}: {ex}")
    for cfg in wl.get("fed_pairs", []):
        try:
            pairs.extend(eng.eval_fed_pair(cfg, errors))
        except Exception as ex:
            errors.append(f"{cfg.get('id')}: {type(ex).__name__}: {ex}")
    return pairs


def handle_signal(s):
    """Execute one signal. Returns the ledger entry, or None to skip."""
    if DRY_RUN:
        e = dict(s)
        e["mode"] = "dry-run"
        e["status"] = "dry-run"
        e["message"] = s["message"].replace(
            "Kalshi signal (paper)", "Kalshi bot signal (DRY-RUN \u2014 no order placed)")
        log(f"DRY-RUN would trade: {s['side']} {s['contracts']}x {s['ticker']} "
            f"@ {s['entry_price_c']:.0f}c (div {s['divergence_c']:+.1f}c)")
        if NOTIFY_DRY_RUN:
            notify("Kalshi bot (dry-run)",
                   f"DRY-RUN signal: {s['side']} {s['contracts']}x {s['ticker']} "
                   f"@ {s['entry_price_c']:.0f}c (div {s['divergence_c']:+.1f}c)")
        return e
    # ---- LIVE: real money ----
    q = fresh_quote(s["ticker"])
    if not q:
        log(f"LIVE skip {s['ticker']}: could not re-fetch quote")
        return None
    ask_now = q["yes_ask"] if s["side"] == "YES" else q["no_ask"]
    if abs(ask_now - s["entry_price_c"]) > STALE_TOLERANCE_C:
        log(f"LIVE skip {s['ticker']}: price moved "
            f"{s['entry_price_c']:.0f}c -> {ask_now:.0f}c (>2c tolerance)")
        return None
    try:
        order = place_live_order(get_client(), s, ask_now)
    except Exception as ex:
        log(f"LIVE order FAILED {s['ticker']}: {type(ex).__name__}: {ex}")
        notify("Kalshi bot: ORDER FAILED",
               f"{s['side']} {s['contracts']}x {s['ticker']}: {type(ex).__name__}",
               priority="high")
        return None
    e = dict(s)
    e["mode"] = "live"
    e["status"] = "live-open"
    e["entry_price_c"] = round(ask_now, 2)
    e["stake_c"] = round(s["contracts"] * ask_now, 2)
    e["fee_c"] = eng.taker_fee_c(s["contracts"], ask_now / 100.0)
    if order is not None:
        e["order_id"] = getattr(order, "order_id", None)
        e["order_status"] = getattr(order, "status", None)
        e["remaining_count"] = getattr(order, "remaining_count", None)
    log(f"LIVE order placed: {s['side']} {s['contracts']}x {s['ticker']} @ {ask_now:.0f}c "
        f"order_id={e.get('order_id')}")
    notify("Kalshi bot: LIVE ORDER (real money)",
           f"Bought {s['side']} {s['contracts']}x {s['ticker']} @ {ask_now:.0f}c",
           priority="high")
    return e


def scan_once():
    t = eng.now_et()
    ledger = eng.load_json(LEDGER, [])
    state = eng.load_json(STATE, {})
    today = t.strftime("%Y-%m-%d")
    if state.get("date") != today:
        state = {"date": today, "signal_ids": [], "info_ids": []}

    errors = []
    settled = settle_bot(ledger, errors)
    pairs = evaluate_pairs(errors)
    if pairs is None:
        log("ERROR: watchlist.json missing/unreadable")
        return
    # Reference prices for the exit veto: never exit on a Kalshi quote the
    # reference market contradicts (stale post-game prints).
    ref_map = {p["kalshi_ticker"]: p["ref_yes_c"] for p in pairs
               if p.get("ref_yes_c") is not None}
    # Exits run before the halt math so freed risk counts this scan.
    exited = check_exits(ledger, errors, ref_map)

    pnl = bot_daily_pnl(ledger, today)
    # Exposure-aware stop: halt when worst case (everything open goes to zero)
    # would breach -$20. This keeps "stop-loss $20" true even with $10 trades
    # and no daily cap — max $20 at risk at any moment, unlimited orders/day
    # as positions settle.
    open_marks = sum(e.get("last_mark_c", e.get("stake_c", 0))
                     for e in ledger if e.get("status") in OPEN_STATUSES)
    worst_case = pnl - open_marks
    halted = worst_case <= -DAILY_STOP_LOSS_C
    if halted:
        log(f"daily stop-loss hit (worst-case {worst_case / 100:+.2f}); info-only mode")

    uncapped = MAX_SIGNALS_PER_DAY == 0
    placed = []
    if halted:
        # Info-only: keep surfacing new signals, act on nothing.
        held = {e.get("event_ticker") for e in ledger if e.get("status") in OPEN_STATUSES}
        info_done = set(state.get("info_ids", []))
        for p in pairs:
            s = eng.decide_signal(p)
            if not s:
                continue
            ev = p.get("event_ticker")
            if ev in held or ev in info_done:
                continue
            msg = (f"INFO-ONLY (daily stop hit): {s['side']} {s['contracts']}x "
                   f"{s['ticker']} @ {s['entry_price_c']:.0f}c "
                   f"(div {s['divergence_c']:+.1f}c)")
            log(msg)
            notify("Kalshi bot (info-only)", msg)
            info_done.add(ev)
        state["info_ids"] = sorted(info_done)
    elif uncapped or len(state["signal_ids"]) < MAX_SIGNALS_PER_DAY:
        # One position per event: both sides of one game are the same bet.
        held = {e.get("event_ticker") for e in ledger if e.get("status") in OPEN_STATUSES}
        today_ev = {x.get("event_ticker") for x in ledger if x.get("id") in state["signal_ids"]}
        best = {}
        for p in pairs:
            s = eng.decide_signal(p)
            if not s:
                continue
            ev = p.get("event_ticker")
            if ev in held or ev in today_ev:
                continue
            s["event_ticker"] = ev
            if ev not in best or abs(s["divergence_c"]) > abs(best[ev]["divergence_c"]):
                best[ev] = s
        for s in sorted(best.values(), key=lambda x: -abs(x["divergence_c"])):
            s["id"] = f"bot-{t.strftime('%Y%m%d-%H%M%S')}-{s['ticker']}"
            s["time_et"] = t.isoformat()
            entry = handle_signal(s)
            if entry:
                ledger.append(entry)
                state["signal_ids"].append(entry["id"])
                placed.append(entry["id"])
            if not uncapped and len(state["signal_ids"]) >= MAX_SIGNALS_PER_DAY:
                break

    eng.save_json(LEDGER, ledger)
    eng.save_json(STATE, state)
    if errors:
        for er in errors[:5]:
            log(f"scan warning: {er}")
    log(f"scan done: pairs={len(pairs)} acted={len(placed)} settled={len(settled)} "
        f"exited={len(exited)} pnl_today={pnl / 100:+.2f}")


def main():
    mode = "DRY-RUN (no real orders)" if DRY_RUN else "*** LIVE — REAL MONEY ***"
    log("=" * 60)
    log(f"Kalshi bot starting \u2014 MODE: {mode}")
    cap = "uncapped" if MAX_SIGNALS_PER_DAY == 0 else MAX_SIGNALS_PER_DAY
    log(f"poll={POLL_SECONDS}s stake={MAX_STAKE_C / 100:.2f} max/day={cap} "
        f"stop={DAILY_STOP_LOSS_C / 100:.2f} take_profit={TAKE_PROFIT_PCT * 100:.0f}% "
        f"stop_loss={STOP_LOSS_PCT * 100:.0f}% min_div={MIN_DIVERGENCE_C:.0f}c window=9-21 ET")
    if not DRY_RUN and (not os.environ.get("KALSHI_KEY_ID")
                        or not os.environ.get("KALSHI_KEY_PATH")):
        raise SystemExit("LIVE mode needs KALSHI_KEY_ID and KALSHI_KEY_PATH. Refusing to start.")
    log("=" * 60)
    notify("Kalshi bot started", f"MODE: {mode} — notifications working")
    while True:
        try:
            t = eng.now_et()
            if SCAN_START_H <= t.hour < SCAN_END_H:
                scan_once()
        except Exception as ex:   # never let one bad scan kill the loop; systemd restarts worst case
            log(f"ERROR in scan loop: {type(ex).__name__}: {ex}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped by user")
