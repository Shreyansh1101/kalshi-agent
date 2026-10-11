# Vendored snapshot of ~/workspace/kalshi-signals/scanner.py (2026-10-11).
# The paper-signal engine is the source of truth for strategy/math;
# re-copy this file if the strategy changes.

#!/usr/bin/env python3
"""
Kalshi paper-signal scanner -- PAPER ONLY. Never places real orders.

One full scan:
  1. Enforce scan window (09:00-21:00 America/New_York); outside it, exit quietly.
  2. Settle/mark open paper positions from ledger.json (Kalshi expiration_value).
  3. Load watchlist.json; evaluate each pair (Kalshi quote vs Polymarket reference).
  4. Apply divergence rule, sizing, fees, daily caps, daily stop-loss.
  5. Append new signals to ledger.json; update state.json.
  6. Print JSON summary to stdout; ALWAYS exit 0 (fail soft on bad data).

Approved parameters (2026-10-09; updated 2026-10-10 per user: $4.00 stake, 2c threshold):
  paper bankroll $50 | max $4.00 stake/signal | max 3 signals/day (ET)
  $20 daily stop-loss on paper P&L (realized today + open marked at Kalshi mid)
  divergence = Kalshi YES mid - reference YES; BUY YES if <= -2c, BUY NO if >= +2c
  contracts = floor(400 / ask_cents) of side bought; taker fee = ceil(0.07*C*P*(1-P)) cents
  paper fill assumed immediate at quoted ask; signal valid 15 min / void if ask moves >2c
"""
import json, math, os, re, sys, time, urllib.request, urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BASE = os.path.expanduser("~/workspace/kalshi-signals")
WATCHLIST = os.path.join(BASE, "watchlist.json")
LEDGER = os.path.join(BASE, "ledger.json")
STATE = os.path.join(BASE, "state.json")

SCAN_START_H, SCAN_END_H = 9, 21          # ET scan window
DIVERGENCE_THRESHOLD_C = 2.0             # cents (lowered from 4.0 per user 2026-10-10; fee-drag zone, paper will judge)
MAX_STAKE_C = 400.0                      # $4.00
MAX_SIGNALS_PER_DAY = 3
DAILY_STOP_LOSS_C = 2000.0               # $20.00
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0"}
_last_call = 0.0

def now_et():
    return datetime.now(ET)

def http_get(url, timeout=15, retries=1):
    """Polite GET: 0.35s spacing, browser UA, one retry. Returns parsed JSON or None."""
    global _last_call
    for attempt in range(retries + 1):
        try:
            gap = time.time() - _last_call
            if gap < 0.35:
                time.sleep(0.35 - gap)
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read().decode("utf-8", "replace")
            _last_call = time.time()
            return json.loads(data)
        except Exception:
            _last_call = time.time()
            if attempt < retries:
                time.sleep(2)
    return None

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)

def c(dollars):
    """dollars -> cents float; None-safe."""
    if dollars is None:
        return None
    try:
        return float(dollars) * 100.0
    except (TypeError, ValueError):
        return None

def taker_fee_c(contracts, price_dollars):
    """Kalshi taker fee, rounded UP to the cent. Returns cents (int)."""
def taker_fee_c(contracts, price_dollars):
    """Kalshi taker fee, rounded UP to the cent. Returns cents (int).
    Uses Decimal so exact table values (e.g. 1c @ 50c -> $0.02) aren't
    overshot by float dust before the ceiling."""
    from decimal import Decimal, ROUND_CEILING
    p = Decimal(str(price_dollars))
    fee_c = Decimal("0.07") * contracts * p * (1 - p) * 100
    return int(fee_c.to_integral_value(rounding=ROUND_CEILING))

def fmt_money(cents):
    return f"${cents/100:.2f}"

# ---------------- Kalshi helpers (public, no auth) ----------------
_market_cache = {}

def kalshi_markets(params, max_pages=4):
    """GET /markets with params; follows cursor. Returns list (may be partial)."""
    out, cursor, pages = [], None, 0
    while pages < max_pages:
        q = dict(params)
        q["limit"] = q.get("limit", 200)
        if cursor:
            q["cursor"] = cursor
        url = KALSHI_API + "/markets?" + urllib.parse.urlencode(q)
        d = http_get(url)
        if not d or "markets" not in d:
            break
        out.extend(d["markets"])
        cursor = d.get("cursor")
        pages += 1
        if not cursor:
            break
    return out

def kalshi_market(ticker):
    """Single market by ticker, cached per scan. Returns dict or None."""
    if ticker in _market_cache:
        return _market_cache[ticker]
    d = http_get(KALSHI_API + "/markets/" + urllib.parse.quote(ticker, safe=""))
    m = (d or {}).get("market")
    _market_cache[ticker] = m
    return m

def kalshi_quote(m):
    """Extract cent quotes from a Kalshi market dict. Returns dict or None."""
    yb, ya = c(m.get("yes_bid_dollars")), c(m.get("yes_ask_dollars"))
    if yb is None or ya is None or ya <= 0:
        return None
    nb = c(m.get("no_bid_dollars"))
    na = c(m.get("no_ask_dollars"))
    if na is None or na <= 0:
        na = 100.0 - yb  # binary complement fallback
    if nb is None:
        nb = 100.0 - ya
    return {"yes_bid": yb, "yes_ask": ya, "yes_mid": (yb + ya) / 2.0,
            "no_bid": nb, "no_ask": na, "no_mid": (nb + na) / 2.0,
            "status": m.get("status"), "title": m.get("title", ""),
            "expiration_time": m.get("expiration_time", "")}

# ---------------- Polymarket helpers (public, no auth) ----------------
def poly_event_by_slug(slug):
    d = http_get(GAMMA_API + "/events?slug=" + urllib.parse.quote(slug, safe=""))
    if isinstance(d, list) and d:
        return d[0]
    return None

def poly_search_events(query, year_hint=None):
    d = http_get(GAMMA_API + "/public-search?q=" + urllib.parse.quote(query))
    evs = []
    if isinstance(d, dict):
        evs = d.get("events", [])
    elif isinstance(d, list):
        evs = d
    if year_hint:
        dated = [e for e in evs if str(e.get("endDate", ""))[:4] == str(year_hint)]
        if dated:
            return dated
    return evs

def _as_list(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return v or []

def poly_moneyline_market(event):
    """First market that looks like a moneyline (not spread/total)."""
    for m in event.get("markets", []):
        git = (m.get("groupItemTitle") or "").lower()
        if "spread" in git or "total" in git or "over/under" in git:
            continue
        return m
    return None

def poly_clob_buy_c(token_id):
    d = http_get(f"{CLOB_API}/price?token_id={token_id}&side=buy")
    if d and d.get("price") is not None:
        try:
            return float(d["price"]) * 100.0
        except (TypeError, ValueError):
            return None
    return None

def poly_outcome_yes_c(event, team_full_name, city_name=None):
    """CLOB YES buy price (cents) for the moneyline outcome matching a team."""
    m = poly_moneyline_market(event)
    if not m:
        return None, "no moneyline market"
    outcomes = _as_list(m.get("outcomes"))
    tokens = _as_list(m.get("clobTokenIds"))
    want = team_full_name.lower()
    want_city = (city_name or team_full_name.split()[0]).lower()
    idx = None
    for i, o in enumerate(outcomes):
        ol = str(o).lower()
        if want in ol or ol in want or want_city == ol.split()[0]:
            idx = i
            break
    if idx is None or idx >= len(tokens):
        return None, f"outcome not found in {outcomes}"
    px = poly_clob_buy_c(tokens[idx])
    if px is None:
        return None, "no CLOB price"
    return px, None

# ---------------- Pair evaluation ----------------
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

# Team codes shared between NFL and MLB resolve to NFL names in team_names.
# For MLB series, override the ambiguous metros with their baseball clubs.
MLB_TEAM_OVERRIDES = {
    "ARI": "Arizona Diamondbacks",
    "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles",
    "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians",
    "DET": "Detroit Tigers",
    "HOU": "Houston Astros",
    "KC": "Kansas City Royals",
    "MIA": "Miami Marlins",
    "MIN": "Minnesota Twins",
    "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates",
    "SEA": "Seattle Mariners",
    "SF": "San Francisco Giants",
    "TB": "Tampa Bay Rays",
}

def parse_event_date(event_ticker, series):
    """KXNFLGAME-26OCT11PHIJAC -> '2026-10-11'. Returns None on failure."""
    rest = event_ticker[len(series) + 1:]
    m = re.match(r"^(\d{2})([A-Z]{3})(\d{2})", rest)
    if not m:
        return None
    yy, mon, dd = m.groups()
    return f"20{yy}-{MONTHS.get(mon, 0):02d}-{int(dd):02d}"

def ordered_codes(event_ticker, series, codes):
    """Team codes in event-ticker order (dict order is arbitrary).
    KXNFLGAME-26OCT11LVNE + {LV, NE} -> [LV, NE]."""
    rest = event_ticker[len(series) + 1:]
    m = re.match(r"^\d{2}[A-Z]{3}\d{2}(?:\d{4})?", rest)
    teams = rest[m.end():] if m else rest
    first = next((cd for cd in sorted(codes, key=len, reverse=True)
                  if teams.startswith(cd)), None)
    second = next((cd for cd in sorted(codes, key=len, reverse=True)
                   if cd != first and teams.endswith(cd)), None)
    if first and second:
        return [first, second]
    return list(codes)

def _shift_date(d, days):
    try:
        y, mth, dd = map(int, d.split("-"))
        from datetime import date, timedelta
        return (date(y, mth, dd) + timedelta(days=days)).isoformat()
    except Exception:
        return d

def eval_sports_universe(cfg, team_names, slug_map, errors):
    """Dynamic sports pairs: top Kalshi events by volume, matched to Polymarket."""
    pairs = []
    series, prefix = cfg["kalshi_series"], cfg["polymarket_slug_prefix"]
    names = dict(team_names)
    if series == "KXMLBGAME":
        names.update(MLB_TEAM_OVERRIDES)
    mkts = kalshi_markets({"series_ticker": series, "status": "open", "limit": 200})
    events = {}
    for m in mkts:
        if m.get("status") != "active":
            continue
        events.setdefault(m.get("event_ticker", ""), []).append(m)
    ranked = sorted(events.items(),
                    key=lambda kv: sum(float(x.get("volume_fp") or 0) for x in kv[1]),
                    reverse=True)[:cfg.get("max_events", 6)]
    for event_ticker, ev_mkts in ranked:
        if not event_ticker:
            continue
        sides = {}
        for m in ev_mkts:
            code = m["ticker"].rsplit("-", 1)[-1]
            sides[code] = m
        if len(sides) != 2:
            errors.append(f"{event_ticker}: expected 2 sides, got {len(sides)}")
            continue
        codes = ordered_codes(event_ticker, series, list(sides.keys()))
        game_date = parse_event_date(event_ticker, series)
        # Polymarket slug: ticker-order codes, JAC->jax; try exact date then +-1 day
        # (observed: BAL/ATL listed as 10-12 vs Kalshi 10-11). No search fallback:
        # gamma public-search returns irrelevant results for games.
        pevent, slug = None, None
        if game_date:
            for d in (game_date, _shift_date(game_date, 1), _shift_date(game_date, -1)):
                slug = (f"{prefix}-" + "-".join(slug_map.get(cd, cd.lower()) for cd in codes)
                        + f"-{d}")
                pevent = poly_event_by_slug(slug)
                if pevent:
                    break
        if not pevent:
            errors.append(f"{event_ticker}: no Polymarket event (tried {slug})")
            continue
        for code, m in sides.items():
            q = kalshi_quote(m)
            if not q:
                errors.append(f"{m['ticker']}: no Kalshi quote")
                continue
            full = names.get(code, code)
            opp_code = next((cd for cd in codes if cd != code), None)
            opp = names.get(opp_code, opp_code) if opp_code else None
            ref_c, err = poly_outcome_yes_c(pevent, full)
            if ref_c is None:
                errors.append(f"{m['ticker']}: poly ref failed ({err})")
                continue
            pairs.append({
                "kalshi_ticker": m["ticker"],
                "event_ticker": event_ticker,
                "event": f"{' vs '.join(names.get(cd, cd) for cd in codes)} ({series[2:5].upper()}, {game_date})",
                "subject": full,
                "opponent": opp,
                "kalshi_yes_mid_c": round(q["yes_mid"], 2),
                "kalshi_yes_ask_c": round(q["yes_ask"], 2),
                "kalshi_no_ask_c": round(q["no_ask"], 2),
                "ref_yes_c": round(ref_c, 2),
                "ref_source": "polymarket:" + pevent.get("slug", "?"),
            })
    return pairs

def eval_fed_pair(cfg, errors):
    """Kalshi Fed threshold ladder vs Polymarket hike buckets. Baseline re-derived."""
    ladder = kalshi_markets({"event_ticker": cfg["kalshi_event"], "limit": 100})
    thresh = {}
    for m in ladder:
        mt = re.search(r"-T(\d+\.\d+)$", m.get("ticker", ""))
        if not mt or m.get("status") != "active":
            continue
        q = kalshi_quote(m)
        if q:
            thresh[float(mt.group(1))] = (m, q)
    if not thresh:
        errors.append(f"{cfg['id']}: empty ladder")
        return []
    # Current upper bound = first threshold (ascending) whose mid is NOT ~certain.
    # If upper is U: P(upper > T) ~= 100c for all T < U, uncertain at T = U.
    ordered = sorted(thresh)
    baseline = next((t for t in ordered if thresh[t][1]["yes_mid"] < 97.0), None)
    if baseline is None:
        errors.append(f"{cfg['id']}: cannot derive current upper bound from ladder")
        return []
    below = [t for t in ordered if t < baseline]
    if below and thresh[max(below)][1]["yes_mid"] < 97.0:
        errors.append(f"{cfg['id']}: ladder step below baseline not certain - skipping")
        return []
    mt = re.search(r"-T(\d+\.\d+)$", cfg["kalshi_ticker"])
    pair_t = float(mt.group(1)) if mt else None
    if pair_t is None or abs(pair_t - baseline) > 1e-9:
        errors.append(f"{cfg['id']}: baseline moved (ladder says {baseline}, pair is {pair_t}) - skipping")
        return []
    m, q = thresh[pair_t]
    if not (2.0 < q["yes_mid"] < 98.0):
        errors.append(f"{cfg['id']}: pair price not uncertain (mid {q['yes_mid']:.1f}c) - skipping")
        return []
    pevent = poly_event_by_slug(cfg.get("polymarket_slug_fallback", ""))
    if not pevent:
        for cand in poly_search_events(cfg.get("polymarket_search", ""), cfg.get("polymarket_year")):
            pevent = cand
            break
    if not pevent:
        errors.append(f"{cfg['id']}: no Polymarket event")
        return []
    hike_c = 0.0
    matched = 0
    for mk in pevent.get("markets", []):
        if "increase" in (mk.get("groupItemTitle") or "").lower():
            toks = _as_list(mk.get("clobTokenIds"))
            if toks:
                px = poly_clob_buy_c(toks[0])
                if px is not None:
                    hike_c += px
                    matched += 1
    if matched == 0:
        errors.append(f"{cfg['id']}: no hike buckets on Polymarket")
        return []
    return [{
        "kalshi_ticker": m["ticker"],
        "event_ticker": cfg["kalshi_event"],
        "event": f"Fed: upper bound > {pair_t:.2f}% after meeting (P(hike), baseline {baseline:.2f}%)",
        "plain_outcome": {
            "YES": "This means: you're betting the Fed RAISES rates at the October meeting (upper bound ends above 4.00%).",
            "NO": "This means: you're betting the Fed does NOT raise rates at the October meeting (upper bound stays at or below 4.00%).",
        },
        "kalshi_yes_mid_c": round(q["yes_mid"], 2),
        "kalshi_yes_ask_c": round(q["yes_ask"], 2),
        "kalshi_no_ask_c": round(q["no_ask"], 2),
        "ref_yes_c": round(hike_c, 2),
        "ref_source": "polymarket:" + pevent.get("slug", "?"),
    }]

# ---------------- Signal logic ----------------
def decide_signal(pair):
    """Apply divergence rule + sizing + fees. Returns signal dict or None."""
    div = pair["kalshi_yes_mid_c"] - pair["ref_yes_c"]
    if div <= -DIVERGENCE_THRESHOLD_C:
        side, ask_c = "YES", pair["kalshi_yes_ask_c"]
    elif div >= DIVERGENCE_THRESHOLD_C:
        side, ask_c = "NO", pair["kalshi_no_ask_c"]
    else:
        return None
    if ask_c is None or ask_c <= 0:
        return None
    contracts = math.floor(MAX_STAKE_C / ask_c)
    if contracts < 1:
        return None
    stake_c = round(contracts * ask_c, 2)
    fee_c = taker_fee_c(contracts, ask_c / 100.0)
    fair_c = pair["ref_yes_c"] if side == "YES" else 100.0 - pair["ref_yes_c"]
    gap_c = abs(div)
    # Plain-English outcome: answer "YES/NO for which team?" before it's asked.
    subj, opp = pair.get("subject"), pair.get("opponent")
    if subj and opp:
        plain = (f"This means: you're betting the {subj} "
                 f"{'win' if side == 'YES' else 'lose'} "
                 f"(the {opp} {'lose' if side == 'YES' else 'win'}).")
    elif isinstance(pair.get("plain_outcome"), dict):
        plain = pair["plain_outcome"].get(side, "")
    else:
        plain = ""
    return {
        "ticker": pair["kalshi_ticker"],
        "event": pair["event"],
        "side": side,
        "contracts": contracts,
        "entry_price_c": round(ask_c, 2),
        "stake_c": stake_c,
        "fee_c": fee_c,
        "ref_price_c": pair["ref_yes_c"],
        "divergence_c": round(div, 2),
        "ref_source": pair["ref_source"],
        "message": (
            f"\U0001f4ca Kalshi signal (paper): {pair['event']} looks mispriced \u2014 "
            f"{side} is {gap_c:.0f}¢ cheap at {ask_c:.0f}¢ (fair value ~{fair_c:.0f}¢).\n"
            + (f"{plain}\n" if plain else "") +
            f"In the app: open \u201c{pair['event']}\u201d ({pair['kalshi_ticker']}), "
            f"buy {side} \u2014 {contracts} contracts at {ask_c:.0f}¢ "
            f"(about {fmt_money(stake_c)} + {fmt_money(fee_c)} fee). "
            f"Only if {side} is still {ask_c:.0f}\u2013{ask_c + 2:.0f}¢; "
            f"skip it if the price moved. Good for the next 15 minutes."
        ),
    }

# ---------------- Settlement & marking ----------------
def settle_and_mark(ledger, errors):
    """Settle finished positions; refresh last mark on open ones. Returns settled list."""
    settled = []
    t = now_et().isoformat()
    for e in ledger:
        if e.get("status") != "open":
            continue
        m = kalshi_market(e["ticker"])
        if not m:
            errors.append(f"{e['ticker']}: market fetch failed during settlement check")
            continue
        q = kalshi_quote(m)
        if q:
            mid = q["yes_mid"] if e["side"] == "YES" else q["no_mid"]
            e["last_mark_c"] = round(e["contracts"] * mid, 2)
            e["last_mark_time"] = t
        if m.get("status") == "settled":
            ev = str(m.get("expiration_value", "") or m.get("result", "")).lower()
            win = ((e["side"] == "YES" and ev in ("yes", "1", "true")) or
                   (e["side"] == "NO" and ev in ("no", "0", "false")))
            if ev not in ("yes", "no", "1", "0", "true", "false", ""):
                errors.append(f"{e['ticker']}: unclear expiration_value {ev!r} - left open")
                continue
            payout_c = e["contracts"] * 100 if win else 0
            e["status"] = "settled"
            e["settled_at_et"] = t
            e["payout_c"] = payout_c
            e["pnl_c"] = round(payout_c - e["stake_c"] - e["fee_c"], 2)
            settled.append({"id": e["id"], "ticker": e["ticker"], "side": e["side"],
                            "payout_c": payout_c, "pnl_c": e["pnl_c"]})
    return settled

def daily_pnl_c(ledger, today):
    """Realized today + unrealized on all open positions (marked at Kalshi mid)."""
    pnl = 0.0
    for e in ledger:
        if e.get("status") == "settled" and str(e.get("settled_at_et", ""))[:10] == today:
            pnl += e.get("pnl_c", 0)
        elif e.get("status") == "open":
            pnl += e.get("last_mark_c", e.get("stake_c", 0)) - e.get("stake_c", 0) - e.get("fee_c", 0)
    return round(pnl, 2)

# ---------------- Main ----------------
def main():
    t = now_et()
    summary = {"timestamp_et": t.isoformat(), "signals": [], "scanned": 0,
               "settled": [], "errors": [], "signals_today": 0,
               "daily_pnl_cents": 0, "halted": False}
    if not (SCAN_START_H <= t.hour < SCAN_END_H):
        summary["skipped"] = "outside scan window (09:00-21:00 ET)"
        print(json.dumps(summary))
        return 0
    wl = load_json(WATCHLIST, None)
    if not wl:
        summary["errors"].append("watchlist.json missing/unreadable")
        print(json.dumps(summary))
        return 0
    ledger = load_json(LEDGER, [])
    state = load_json(STATE, {})
    today = t.strftime("%Y-%m-%d")
    if state.get("date") != today:
        state = {"date": today, "signal_ids": []}

    errors = []
    settled = settle_and_mark(ledger, errors)

    pairs = []
    team_names = wl.get("team_names", {})
    slug_map = wl.get("slug_code_map", {})
    for cfg in wl.get("dynamic_universes", []):
        try:
            pairs.extend(eval_sports_universe(cfg, team_names, slug_map, errors))
        except Exception as ex:
            errors.append(f"{cfg.get('id')}: {type(ex).__name__}: {ex}")
    for cfg in wl.get("fed_pairs", []):
        try:
            pairs.extend(eval_fed_pair(cfg, errors))
        except Exception as ex:
            errors.append(f"{cfg.get('id')}: {type(ex).__name__}: {ex}")

    pnl = daily_pnl_c(ledger, today)
    halted = pnl <= -DAILY_STOP_LOSS_C
    new_signals = []
    if not halted and len(state["signal_ids"]) < MAX_SIGNALS_PER_DAY:
        # One position per event at a time: both sides of one game express the
        # same directional bet, so per-(ticker,side) dedupe would double exposure.
        held_events = {e.get("event_ticker") for e in ledger if e.get("status") == "open"}
        today_events = {s.get("event_ticker") for s in ledger
                        if s.get("id") in state["signal_ids"]}
        candidates = []
        for p in pairs:
            sig = decide_signal(p)
            if not sig:
                continue
            ev = p.get("event_ticker")
            if ev in held_events or ev in today_events:
                continue  # already hold or already signaled this event
            sig["event_ticker"] = ev
            candidates.append(sig)
        best = {}  # strongest edge per event
        for sig in candidates:
            ev = sig["event_ticker"]
            if ev not in best or abs(sig["divergence_c"]) > abs(best[ev]["divergence_c"]):
                best[ev] = sig
        for sig in sorted(best.values(), key=lambda s: -abs(s["divergence_c"])):
            sig["id"] = f"sig-{t.strftime('%Y%m%d-%H%M%S')}-{sig['ticker']}"
            sig["time_et"] = t.isoformat()
            sig["status"] = "open"
            new_signals.append(sig)
            ledger.append(sig)
            state["signal_ids"].append(sig["id"])
            if len(state["signal_ids"]) >= MAX_SIGNALS_PER_DAY:
                break

    save_json(LEDGER, ledger)
    save_json(STATE, state)
    summary.update({
        "signals": new_signals,
        "scanned": len(pairs),
        "settled": settled,
        "errors": errors,
        "signals_today": len(state["signal_ids"]),
        "daily_pnl_cents": pnl,
        "halted": halted,
    })
    print(json.dumps(summary, indent=1))
    return 0

if __name__ == "__main__":
    sys.exit(main())
