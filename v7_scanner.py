#!/usr/bin/env python3
"""
V7.3 Scanner — 1DTE SPY debit-spread scalping for $500 account.

Honest architecture after backtesting:
- $50/trade budget cannot buy meaningful 0DTE ATM options (real cost $250-400/contract).
- 1DTE $2-wide debit spreads fit the $50 budget, cap risk, and give real delta exposure.
- Free signal layers: EMA cross + VWAP ±1σ bands + Supertrend + VIX9D/VIX/VIX3M term structure.
- Premium feeds (TICK, ADD, VOLD, GEX) are NOT used because they require CBOE/vendor data.

Position: 1 contract per signal, max 2 trades/day, daily loss cap $50.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from scipy.stats import norm
except ImportError:
    def norm_cdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
    class _Norm:
        def cdf(self, x): return norm_cdf(x)
    norm = _Norm()

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")

# PATHS
STATE_DIR = Path("/path/to/project/.hermes/state")
STATE_FILE = STATE_DIR / "v7_state.json"
TRADES_FILE = STATE_DIR / "v7_trades.jsonl"
KILL_SWITCH = STATE_DIR / "v7_kill_switch.json"
DISCORD_HELPER = Path("/path/to/project/.hermes/scripts/discord-send-message.mjs")
DISCORD_TRADE_LOG = "1519239671224336465"

# V7.3 CONFIG
SYMBOLS = ["SPY"]
DTE = 1                    # 1DTE debit spreads
SPREAD_WIDTH = 2           # $2-wide spread (e.g. 720/722 call)
MAX_PREMIUM = 50.0         # Net debit per spread, $50 budget
MAX_SPREAD_COST = 50.0
MIN_SPREAD_COST = 5.0
SPREAD_PCT = 0.02          # round-trip slippage
COMMISSION = 0.0
RISK_FREE_RATE = 0.045
IV_MULT = 1.2

GATES = {
    "minScore": 6,            # Tuned from backtest: 6 gives best frequency/edge
    "minVolumeRatio": 0.85,
    "minMomentumAbs": 0.12,
    "minDayMoveAbs": 0.12,
    "avoidRsiHigh": 78,
    "avoidRsiLow": 22,
    "veryThinVolFloor": 0.50,
    "flatTapeFloor": 0.02,
    "dayMoveAgainstThreshold": 0.20,
    "avoidLunchChop": True,
    "afternoonMomentumMin": 0.12,
    "skipFlatRegime": True,
    "flatRegimeRange": 0.30,
    "skipFridayPM": True,
    "skipMacroEvents": True,
    "minVix": 0.15,
    "maxVix": 0.35,
    "requireVix9dBack": True, # Only trade when short vol > spot (backwardation)
}

RISK = {
    "stopLossPct": 50.0,      # lose half the debit
    "hardStopPct": 100.0,
    "profitTargetPct": 50.0,  # make half the debit
    "trailingStartPct": 25.0,
    "trailingStopPct": 15.0,
    "maxHoldBars": 36,        # 3 hours max
    "minHoldBars": 3,
    "staleBars": 18,
    "staleThresholdPct": 8.0,
}

MAX_TRADES_PER_DAY = 2
MAX_DAILY_LOSS = 50.0
MIN_BUYING_POWER = 500.0
SESSION_START_HOUR = 6
SESSION_START_MIN = 40
ENTRY_END_HOUR = 13
ENTRY_END_MIN = 45
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

MACRO_EVENTS = {"FOMC", "CPI", "NFP"}
MACRO_DATES_2026 = {
    "2026-04-29": "FOMC", "2026-04-30": "FOMC",
    "2026-06-17": "FOMC", "2026-06-18": "FOMC",
    "2026-07-29": "FOMC", "2026-07-30": "FOMC",
    "2026-04-15": "CPI", "2026-05-14": "CPI", "2026-06-12": "CPI", "2026-07-11": "CPI",
    "2026-04-03": "NFP", "2026-05-01": "NFP", "2026-06-05": "NFP", "2026-07-03": "NFP",
}

# BSM

def bsm_price(S, K, T, r, sigma, option_type="call"):
    if T <= 0 or sigma <= 0:
        return max(0, S - K) if option_type == "call" else max(0, K - S)
    d1 = (math.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "call":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

# DATA

def fetch_bars(symbol, interval="5m", days=5):
    end_ts = int(datetime.now().timestamp())
    start_ts = int((datetime.now() - timedelta(days=days)).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&period1={start_ts}&period2={end_ts}"
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"ERROR fetching {symbol}: {e}")
        return []
    result = data["chart"]["result"][0]
    ts = result["timestamp"]; q = result["indicators"]["quote"][0]
    bars = []
    for i in range(len(ts)):
        try:
            o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
            if any(x is None or math.isnan(x) for x in [o, h, l, c, v]): continue
            dt = datetime.fromtimestamp(ts[i], tz=ET)
            bars.append({"ts": ts[i], "dt": dt, "date": dt.strftime("%Y-%m-%d"),
                "time_num": dt.hour * 100 + dt.minute, "weekday": dt.weekday(),
                "open": float(o), "high": float(h), "low": float(l),
                "close": float(c), "volume": float(v)})
        except (TypeError, IndexError): continue
    return bars


def fetch_vix() -> float:
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=5d"
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        for c in reversed(closes):
            if c is not None and not math.isnan(c):
                return c / 100
    except Exception: pass
    return 0.15


def fetch_vix_term(days=5):
    """Return dict date -> {VIX9D, VIX, VIX3M}."""
    out = {}
    for sym in ["^VIX9D", "^VIX", "^VIX3M"]:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range={days}d"
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            res = data["chart"]["result"][0]
            ts = res["timestamp"]; cl = res["indicators"]["quote"][0]["close"]
            key = sym.replace("^", "")
            for i, t in enumerate(ts):
                if cl[i] is not None:
                    d = datetime.fromtimestamp(t, tz=ET).strftime("%Y-%m-%d")
                    out.setdefault(d, {})[key] = cl[i] / 100
        except Exception: pass
    return out

# INDICATORS

def calc_ema(values, period):
    if len(values) < period:
        return [values[-1]] * len(values) if values else []
    k = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return [ema[0]] * (len(values) - len(ema)) + ema


def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d for d in deltas[-period:] if d > 0]
    losses = [-d for d in deltas[-period:] if d < 0]
    avg_g = sum(gains) / period if gains else 0.001
    avg_l = sum(losses) / period if losses else 0.001
    return 100 - 100 / (1 + avg_g / avg_l)


def calc_vwap_bands(bars):
    if not bars: return (0, 0, 0)
    cum_vp = sum(b["close"] * b["volume"] for b in bars)
    cum_v = sum(b["volume"] for b in bars)
    vwap = cum_vp / cum_v if cum_v > 0 else bars[-1]["close"]
    var = sum((b["close"] - vwap) ** 2 * b["volume"] for b in bars) / cum_v if cum_v else 0
    sd = math.sqrt(var)
    return (vwap, vwap + sd, vwap - sd)


def calc_volume_ratio(bars, lookback=20):
    if len(bars) < lookback: return 1.0
    recent = sum(b["volume"] for b in bars[-5:]) / 5
    avg = sum(b["volume"] for b in bars[-lookback:]) / lookback
    return recent / avg if avg > 0 else 0


def calc_atr(bars, period=10):
    if len(bars) < period + 1: return 0
    trs = []
    for i in range(1, len(bars)):
        trs.append(max(bars[i]["high"] - bars[i]["low"],
                       abs(bars[i]["high"] - bars[i - 1]["close"]),
                       abs(bars[i]["low"] - bars[i - 1]["close"])))
    return sum(trs[-period:]) / period


def calc_supertrend(bars, period=10, mult=3.0):
    if len(bars) < period + 1: return None
    a = calc_atr(bars, period)
    hl2 = [(b["high"] + b["low"]) / 2 for b in bars]
    upper = [hl2[i] + mult * a for i in range(len(bars))]
    lower = [hl2[i] - mult * a for i in range(len(bars))]
    st = [0] * len(bars); direction = ["up"] * len(bars)
    st[0] = upper[0]
    for i in range(1, len(bars)):
        if direction[i - 1] == "up":
            st[i] = max(lower[i], st[i - 1])
            if bars[i]["close"] < st[i]:
                direction[i] = "down"; st[i] = upper[i]
        else:
            st[i] = min(upper[i], st[i - 1])
            if bars[i]["close"] > st[i]:
                direction[i] = "up"; st[i] = lower[i]
    return direction[-1]


# SIGNAL

def score_signal(closes, bars, symbol, vix_term):
    if len(closes) < 25:
        return {"score": 0, "direction": None, "reasons": [], "hard_block": "insufficient_data"}

    current = closes[-1]; prev_close = closes[-2]
    ema9 = calc_ema(closes, 9); ema21 = calc_ema(closes, 21)
    rsi = calc_rsi(closes)
    vwap, vwap_up, vwap_dn = calc_vwap_bands(bars[-20:])
    vol_ratio = calc_volume_ratio(bars)
    st = calc_supertrend(bars[-30:])
    day_change_pct = (current - prev_close) / prev_close * 100
    momentum_pct = (current - closes[-4]) / closes[-4] * 100 if len(closes) >= 4 else 0

    v9d = vix_term.get("VIX9D", 0.15); v = vix_term.get("VIX", 0.175); v3m = vix_term.get("VIX3M", 0.18)
    back9d = v9d > v; steep_contango = v3m > v * 1.15

    indicators = {
        "price": round(current, 2), "prevClose": round(prev_close, 2),
        "ema9": round(ema9[-1], 4), "ema21": round(ema21[-1], 4),
        "vwap": round(vwap, 4), "vwapUp": round(vwap_up, 4), "vwapDn": round(vwap_dn, 4),
        "rsi": round(rsi, 1), "dayChangePct": round(day_change_pct, 4),
        "momentumPct": round(momentum_pct, 4), "volumeRatio": round(vol_ratio, 4),
        "supertrend": st, "vix9d": round(v9d, 4), "vix": round(v, 4), "vix3m": round(v3m, 4),
        "back9d": back9d, "steep_contango": steep_contango,
    }

    hard_blocks = []
    if rsi > GATES["avoidRsiHigh"]: hard_blocks.append(f"rsi_overbought({rsi:.0f})")
    if rsi < GATES["avoidRsiLow"]: hard_blocks.append(f"rsi_oversold({rsi:.0f})")
    if vol_ratio < GATES["veryThinVolFloor"]: hard_blocks.append(f"very_thin_volume({vol_ratio:.2f})")
    if abs(momentum_pct) < GATES["flatTapeFloor"]: hard_blocks.append(f"flat_tape({momentum_pct:.3f}%)")
    if v < GATES["minVix"]: hard_blocks.append(f"vix_too_low({v*100:.1f}%)")
    if v > GATES["maxVix"]: hard_blocks.append(f"vix_too_high({v*100:.1f}%)")
    if GATES["requireVix9dBack"] and not back9d:
        hard_blocks.append(f"vix9d_not_back({v9d*100:.1f}% < {v*100:.1f}%)")
    if hard_blocks:
        return {"score": 0, "direction": None, "reasons": hard_blocks,
                "indicators": indicators, "hard_block": ", ".join(hard_blocks)}

    if ema9[-1] > ema21[-1] and day_change_pct > 0 and st == "up":
        direction = "bullish"
    elif ema9[-1] < ema21[-1] and day_change_pct < 0 and st == "down":
        direction = "bearish"
    else:
        return {"score": 0, "direction": None, "reasons": ["no_trend"],
                "indicators": indicators, "hard_block": None}

    if direction == "bullish" and day_change_pct < -GATES["dayMoveAgainstThreshold"]:
        return {"score": 0, "direction": None, "reasons": ["day_move_against"], "indicators": indicators, "hard_block": None}
    if direction == "bearish" and day_change_pct > GATES["dayMoveAgainstThreshold"]:
        return {"score": 0, "direction": None, "reasons": ["day_move_against"], "indicators": indicators, "hard_block": None}

    score = 0; reasons = []
    if direction == "bullish": reasons.append("bullish_trend"); score += 1
    else: reasons.append("bearish_trend"); score += 1
    if abs(day_change_pct) > GATES["minDayMoveAbs"]: score += 1; reasons.append("day_move_aligned")
    if abs(momentum_pct) > GATES["minMomentumAbs"]: score += 1; reasons.append("momentum_confirmed")
    if direction == "bullish" and current > vwap: score += 1; reasons.append("vwap_aligned")
    elif direction == "bearish" and current < vwap: score += 1; reasons.append("vwap_aligned")
    if direction == "bullish" and current > vwap_up and not steep_contango: score += 1; reasons.append("vwap_breakout")
    elif direction == "bearish" and current < vwap_dn and not steep_contango: score += 1; reasons.append("vwap_breakdown")
    if vol_ratio >= GATES["minVolumeRatio"]: score += 1; reasons.append("volume_ok")
    if GATES["avoidRsiLow"] < rsi < GATES["avoidRsiHigh"]: score += 1; reasons.append("rsi_ok")
    if symbol in SYMBOLS: score += 1; reasons.append("liquid_underlying")
    if back9d: score += 1; reasons.append("vix9d_back")

    return {"score": score, "direction": direction, "reasons": reasons,
            "indicators": indicators, "hard_block": None}


# STATE

def load_state() -> dict:
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text())
        except Exception: pass
    return {
        "version": "7.3", "mode": "dry_run",
        "dailyPnl": 0.0, "dailyTrades": 0, "dailyLoss": 0.0,
        "lastResetDate": "", "maxDailyLoss": MAX_DAILY_LOSS,
        "maxDailyTrades": MAX_TRADES_PER_DAY, "activePositions": [],
    }


def save_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def check_kill_switch() -> bool: return KILL_SWITCH.exists()


def log_trade(trade: dict):
    TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TRADES_FILE, "a") as f: f.write(json.dumps(trade) + "\n")


def discord_post(channel_id: str, content: str) -> bool:
    if not DISCORD_HELPER.exists(): return False
    env_file = DISCORD_HELPER.parent / ".env"; env = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); env[k.strip()] = v.strip().strip("'\"")
    proc = subprocess.run(["/usr/local/bin/node", str(DISCORD_HELPER), channel_id, content],
        cwd=str(DISCORD_HELPER.parent), text=True, capture_output=True, timeout=30, check=False,
        env={**os.environ, **env})
    return proc.returncode == 0


def reset_daily_if_needed(state: dict) -> dict:
    today = datetime.now(PT).strftime("%Y-%m-%d")
    if state.get("lastResetDate") != today:
        state["dailyPnl"] = 0.0; state["dailyTrades"] = 0; state["dailyLoss"] = 0.0; state["lastResetDate"] = today
    return state


# ROBINHOOD MCP STUBS (Hermes runtime injects real MCP tools)

def get_buying_power() -> float: return 500.0


def review_option_order(symbol, long_strike, short_strike, option_type, expiry, quantity=1, side="buy", price=None):
    print(f"   📋 Review: {quantity}x {symbol} ${long_strike}/${short_strike} {option_type} spread {expiry} @ ${price or 'market'}")
    return True


def place_option_order(symbol, long_strike, short_strike, option_type, expiry, quantity=1, side="buy", price=None):
    print(f"   📤 Place: {quantity}x {symbol} ${long_strike}/${short_strike} {option_type} spread {expiry} @ ${price or 'market'}")
    return {"id": "dry_run", "status": "filled", "price": price or 0}


# SPREAD SELECTION

def pick_spread(S, direction, T_entry, iv, spread_width, max_premium):
    option_type = "call" if direction == "bullish" else "put"
    step = 1 if option_type == "call" else -1
    Sint = int(round(S))
    for off in range(0, 30):
        K_long = Sint + off * step
        K_short = K_long + spread_width * step
        if K_long <= 0 or K_short <= 0: continue
        P_long = bsm_price(S, K_long, T_entry, RISK_FREE_RATE, iv, option_type)
        P_short = bsm_price(S, K_short, T_entry, RISK_FREE_RATE, iv, option_type)
        net_debit = P_long - P_short
        if net_debit <= 0: continue
        cost = net_debit * 100
        if cost <= max_premium and cost >= MIN_SPREAD_COST:
            return K_long, K_short, net_debit, cost
    return None, None, None, None


# MAIN

def main():
    parser = argparse.ArgumentParser(description="V7.3 Scanner — 1DTE SPY debit spreads")
    parser.add_argument("--symbol", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    now = datetime.now(PT); now_et = datetime.now(ET)
    time_num = now_et.hour * 100 + now_et.minute
    print(f"🕐 V7.3 Scanner — {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"   Config: 1DTE SPY ${SPREAD_WIDTH}-wide debit spreads, ${MAX_PREMIUM}/trade max")

    if check_kill_switch():
        print("🛑 KILL SWITCH ACTIVE")
        return 0

    state = load_state(); state = reset_daily_if_needed(state)
    is_dry_run = args.dry_run or state.get("mode", "dry_run") != "live"
    if is_dry_run:
        print("🔒 DRY RUN MODE — no real orders")
    else:
        arm_file = STATE_DIR / "v7_live_armed.json"
        if not arm_file.exists():
            print("🛑 LIVE armed file missing — falling back to dry-run")
            is_dry_run = True
        else:
            print("🟢 LIVE MODE")

    if state["dailyTrades"] >= MAX_TRADES_PER_DAY and not args.force:
        print(f"⛔ Daily trade limit")
        save_state(state); return 0
    if state["dailyLoss"] >= MAX_DAILY_LOSS and not args.force:
        print(f"⛔ Daily loss limit")
        save_state(state); return 0

    session_start = now.replace(hour=SESSION_START_HOUR, minute=SESSION_START_MIN)
    session_end = now.replace(hour=ENTRY_END_HOUR, minute=ENTRY_END_MIN)
    if now < session_start or now > session_end:
        print(f"⏰ Outside entry window ({session_start.strftime('%H:%M')}-{session_end.strftime('%H:%M')} PT)")
        return 0

    today_str = now_et.strftime("%Y-%m-%d")
    if GATES.get("skipMacroEvents") and MACRO_DATES_2026.get(today_str) in MACRO_EVENTS:
        print(f"📅 Macro event — skipping")
        save_state(state); return 0
    if GATES.get("skipFridayPM") and now_et.weekday() == 4 and time_num > 1200:
        print("⏭️ Friday PM — skipping")
        save_state(state); return 0

    if not is_dry_run:
        bp = get_buying_power()
        if bp < MIN_BUYING_POWER:
            print(f"💰 Buying power ${bp:.2f} below ${MIN_BUYING_POWER}")
            save_state(state); return 0
        print(f"💰 Buying power: ${bp:.2f}")

    vix = fetch_vix()
    iv = vix * IV_MULT
    vix_term_today = fetch_vix_term(days=5).get(today_str, {"VIX9D": vix, "VIX": vix, "VIX3M": vix})
    print(f"📊 VIX: {vix*100:.1f}% → IV: {iv*100:.1f}%  VIX9D: {vix_term_today.get('VIX9D',0)*100:.1f}%")

    symbols_to_scan = [args.symbol] if args.symbol else SYMBOLS
    for symbol in symbols_to_scan:
        today_positions = [p for p in state.get("activePositions", []) if p.get("symbol") == symbol]
        if today_positions and not args.force:
            print(f"📊 Already in {symbol}")
            continue

        bars = fetch_bars(symbol, interval="5m", days=5)
        if not bars:
            print(f"❌ No data for {symbol}")
            continue

        if GATES.get("skipFlatRegime"):
            today_bars = [b for b in bars if b["date"] == today_str]
            if len(today_bars) >= 25:
                early = today_bars[:25]
                er = (max(b["high"] for b in early) - min(b["low"] for b in early)) / early[0]["open"] * 100
                if er < GATES["flatRegimeRange"]:
                    print(f"   ⏭️ Flat regime: early range {er:.2f}%")
                    continue

        signal = score_signal([b["close"] for b in bars], bars, symbol, vix_term_today)
        print(f"\n📊 {symbol} Signal: score={signal['score']}, direction={signal['direction']}")
        if signal.get("indicators"):
            ind = signal["indicators"]
            print(f"   Price={ind.get('price')} RSI={ind.get('rsi')} DayChg={ind.get('dayChangePct')}% Vol={ind.get('volumeRatio')}")
            print(f"   VWAP={ind.get('vwap')} [±1σ={ind.get('vwapUp')}/{ind.get('vwapDn')}]  Supertrend={ind.get('supertrend')}")
            print(f"   VIX9D={ind.get('vix9d')*100:.1f}% VIX={ind.get('vix')*100:.1f}% VIX3M={ind.get('vix3m')*100:.1f}% back9d={ind.get('back9d')}")

        if signal["hard_block"]:
            print(f"   🛑 Hard block: {signal['hard_block']}"); continue

        if GATES.get("avoidLunchChop") and 1130 <= time_num <= 1300:
            mom = abs(signal.get("indicators", {}).get("momentumPct", 0))
            if mom < 0.05:
                print(f"   ⏭️ Lunch chop: momentum {mom:.3f}%"); continue

        if GATES.get("afternoonMomentumMin") and time_num > 1230:
            mom = abs(signal.get("indicators", {}).get("momentumPct", 0))
            if mom < GATES["afternoonMomentumMin"]:
                print(f"   ⏭️ Afternoon: momentum {mom:.3f}%"); continue

        if signal["score"] < GATES["minScore"]:
            print(f"   ❌ Score {signal['score']} below {GATES['minScore']}"); continue
        if not signal["direction"]:
            print("   ❌ No direction"); continue

        # Entry
        direction = signal["direction"]; option_type = "call" if direction == "bullish" else "put"
        entry_price = signal["indicators"]["price"]
        entry_hour = now_et.hour + now_et.minute / 60
        T_entry = (DTE + (16 - entry_hour) / 24) / 365

        K_long, K_short, net_debit, cost = pick_spread(entry_price, direction, T_entry, iv, SPREAD_WIDTH, MAX_PREMIUM)
        if not K_long:
            print("   ❌ No affordable spread found"); continue

        expiry_date = (now_et + timedelta(days=DTE)).strftime("%Y-%m-%d")
        print(f"   ✅ ENTRY: {symbol} {direction.upper()} ${K_long}/{K_short} {option_type} spread")
        print(f"   Net debit: ${cost:.2f}  Max risk: ${cost:.2f}  Max reward: ${SPREAD_WIDTH*100 - cost:.2f}")
        print(f"   Reasons: {', '.join(signal['reasons'])}")

        if is_dry_run:
            # Paper entry: persist a monitorable spread position. This is deliberately
            # distinct from a real fill; BSM debit is only a proxy until MCP quotes are wired.
            position = {
                "symbol": symbol, "direction": direction,
                "long_strike": K_long, "short_strike": K_short,
                "option_type": option_type, "expiry": expiry_date,
                "entry_underlying": entry_price, "debit": round(cost, 2),
                "entry_time": now.isoformat(), "score": signal["score"],
                "reasons": signal["reasons"], "entry_iv": round(iv, 4),
                "dte": DTE, "quantity": 1, "paper": True,
                "high_water_mark": round(cost, 2),
            }
            state.setdefault("activePositions", []).append(position)
            state["dailyTrades"] = state.get("dailyTrades", 0) + 1
            log_trade({"ts": now.isoformat(), "event": "PAPER_ENTRY", **position})
            print(f"   📝 Paper position opened and logged to {TRADES_FILE}")
        else:
            if not review_option_order(symbol, K_long, K_short, option_type, expiry_date, quantity=1, price=cost):
                print("   ❌ Review failed"); continue
            result = place_option_order(symbol, K_long, K_short, option_type, expiry_date, quantity=1, price=cost)
            position = {
                "symbol": symbol, "direction": direction,
                "long_strike": K_long, "short_strike": K_short,
                "option_type": option_type, "expiry": expiry_date,
                "entry_underlying": entry_price, "debit": round(cost, 2),
                "entry_time": now.isoformat(), "score": signal["score"],
                "entry_iv": round(iv, 4), "order_id": result.get("id", "unknown"),
            }
            state.setdefault("activePositions", []).append(position)
            state["dailyTrades"] += 1
            log_trade({"ts": now.isoformat(), "event": "ENTRY", **position})
            discord_post(DISCORD_TRADE_LOG,
                f"🎯 V7.3 ENTRY: {symbol} {direction.upper()} ${K_long}/${K_short} {option_type} "
                f"score={signal['score']} debit=${cost:.2f} expiry={expiry_date}")

        save_state(state)

    print("\n✅ Scan complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
