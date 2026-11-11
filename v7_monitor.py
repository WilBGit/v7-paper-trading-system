#!/usr/bin/env python3
"""V7.3 paper monitor for 1DTE SPY $2-wide debit spreads.

This monitor deliberately supports paper positions only. Live debit-spread execution
needs verified multi-leg Robinhood MCP order/quote schemas; it is not inferred.
"""
from __future__ import annotations

import argparse
import json
import math
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from scipy.stats import norm
except ImportError:
    class _Norm:
        @staticmethod
        def cdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
    norm = _Norm()

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")
STATE_DIR = Path("/path/to/project/.hermes/state")
STATE_FILE = STATE_DIR / "v7_state.json"
TRADES_FILE = STATE_DIR / "v7_trades.jsonl"
KILL_SWITCH = STATE_DIR / "v7_kill_switch.json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
RISK_FREE_RATE = 0.045
SPREAD_PCT = 0.02
MAX_DAILY_LOSS = 50.0
HARD_CLOSE_HOUR = 15
HARD_CLOSE_MIN = 45
RISK = {
    "stopLossPct": 50.0,
    "hardStopPct": 100.0,
    "profitTargetPct": 50.0,
    "trailingStartPct": 25.0,
    "trailingStopPct": 15.0,
    "staleBars": 18,
    "staleThresholdPct": 8.0,
    "minHoldBars": 3,
    "maxHoldBars": 36,
}


def bsm_price(S, K, T, r, sigma, option_type="call"):
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if option_type == "call" else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "call":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def load_state():
    try: return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"version": "7.3", "mode": "dry_run", "dailyPnl": 0.0,
                "dailyTrades": 0, "dailyLoss": 0.0, "activePositions": []}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["updatedAt"] = datetime.now(PT).isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def log_trade(event):
    TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TRADES_FILE, "a") as f: f.write(json.dumps(event) + "\n")


def reset_daily_if_needed(state):
    today = datetime.now(PT).strftime("%Y-%m-%d")
    if state.get("lastResetDate") != today:
        state["dailyPnl"] = 0.0
        state["dailyTrades"] = 0
        state["dailyLoss"] = 0.0
        state["lastResetDate"] = today
    return state


def get_equity_quote(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1m"
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r: data = json.loads(r.read().decode())
        return float(data["chart"]["result"][0]["meta"].get("regularMarketPrice", 0)) or None
    except Exception:
        return None


def remaining_time(expiry):
    try:
        expiry_dt = datetime.strptime(expiry, "%Y-%m-%d").replace(hour=16, tzinfo=ET)
        return max(0.0, (expiry_dt - datetime.now(ET)).total_seconds() / (365.25 * 24 * 3600))
    except Exception:
        return 1 / 365.25


def estimate_spread_value(position, underlying):
    """Return net liquidation value per contract after conservative half-spread haircut."""
    long_strike = float(position["long_strike"])
    short_strike = float(position["short_strike"])
    option_type = position["option_type"]
    iv = float(position.get("entry_iv", 0.20))
    T = remaining_time(position["expiry"])
    long_value = bsm_price(underlying, long_strike, T, RISK_FREE_RATE, iv, option_type)
    short_value = bsm_price(underlying, short_strike, T, RISK_FREE_RATE, iv, option_type)
    return max(0.0, (long_value - short_value) * 100 * (1 - SPREAD_PCT / 2))


def check_exit(position, current_value, entry_debit, underlying, high_water_mark):
    now = datetime.now(PT)
    pnl_pct = (current_value - entry_debit) / entry_debit * 100 if entry_debit else 0
    hwm = max(high_water_mark, current_value)
    try:
        held = (now - datetime.fromisoformat(position["entry_time"])).total_seconds() / 300
    except Exception:
        held = 999
    if pnl_pct <= -RISK["hardStopPct"]: return f"hard_stop ({pnl_pct:.1f}%)", hwm
    if pnl_pct <= -RISK["stopLossPct"] and held >= RISK["minHoldBars"]: return f"stop_loss ({pnl_pct:.1f}%)", hwm
    if pnl_pct >= RISK["profitTargetPct"]: return f"profit_target ({pnl_pct:.1f}%)", hwm
    hwm_pct = (hwm - entry_debit) / entry_debit * 100 if entry_debit else 0
    if hwm_pct >= RISK["trailingStartPct"] and hwm_pct - pnl_pct >= RISK["trailingStopPct"]:
        return f"trailing_stop ({pnl_pct:.1f}%, peak={hwm_pct:.1f}%)", hwm
    if held >= RISK["staleBars"] and abs(pnl_pct) < RISK["staleThresholdPct"]:
        return f"stale_exit ({pnl_pct:.1f}% after {held:.0f} bars)", hwm
    if now.hour > HARD_CLOSE_HOUR or (now.hour == HARD_CLOSE_HOUR and now.minute >= HARD_CLOSE_MIN):
        return f"time_stop ({now.strftime('%H:%M')} PT)", hwm
    if held >= RISK["maxHoldBars"]: return f"max_hold ({held:.0f} bars)", hwm
    return None, hwm


def self_test():
    pos = {"long_strike": 720, "short_strike": 722, "option_type": "call",
           "expiry": (datetime.now(ET)).strftime("%Y-%m-%d"), "entry_iv": 0.22,
           "entry_time": (datetime.now(PT) - timedelta(hours=1)).isoformat()}
    # ITM long / short leg valuation must be positive and capped below width.
    value = estimate_spread_value(pos, 723.0)
    assert 0 < value <= 200, value
    reason, _ = check_exit(pos, 75.0, 40.0, 723.0, 40.0)
    assert reason and reason.startswith("profit_target"), reason
    reason, _ = check_exit(pos, 15.0, 40.0, 719.0, 40.0)
    assert reason and reason.startswith("stop_loss"), reason
    print(f"SELF_TEST_OK: value=${value:.2f}; profit and stop exits both validated")


def main():
    ap = argparse.ArgumentParser(description="V7.3 1DTE debit-spread paper monitor")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--force-close", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test(); return 0
    now = datetime.now(PT)
    print(f"🕐 V7.3 Spread Monitor — {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    state = reset_daily_if_needed(load_state())
    if state.get("mode") == "live":
        print("🛑 LIVE spread monitoring is disabled pending verified Robinhood multi-leg MCP schemas")
        save_state(state); return 1
    positions = state.get("activePositions", [])
    print(f"🔒 PAPER MODE — {len(positions)} open spread position(s)")
    close_all = args.force_close or KILL_SWITCH.exists()
    removed = []
    for idx, pos in enumerate(positions):
        required = {"long_strike", "short_strike", "option_type", "expiry", "debit", "entry_time", "symbol"}
        if not required.issubset(pos):
            print(f"⚠️ Skipping legacy non-spread position #{idx}")
            continue
        underlying = get_equity_quote(pos["symbol"])
        if underlying is None:
            print(f"⚠️ No quote for {pos['symbol']}"); continue
        debit = float(pos["debit"]); value = estimate_spread_value(pos, underlying)
        pnl = value - debit; pnl_pct = pnl / debit * 100 if debit else 0
        hwm = float(pos.get("high_water_mark", debit))
        reason, new_hwm = check_exit(pos, value, debit, underlying, hwm)
        pos["high_water_mark"] = round(new_hwm, 2)
        print(f"📊 {pos['symbol']} {pos['direction'].upper()} {pos['long_strike']}/{pos['short_strike']} {pos['option_type']}: debit=${debit:.2f} value=${value:.2f} P&L=${pnl:+.2f} ({pnl_pct:+.1f}%)")
        if close_all: reason = "force_close" if args.force_close else "kill_switch"
        if reason:
            print(f"   🔴 PAPER EXIT: {reason}")
            state["dailyPnl"] = state.get("dailyPnl", 0) + pnl
            if pnl < 0: state["dailyLoss"] = state.get("dailyLoss", 0) + abs(pnl)
            log_trade({"ts": now.isoformat(), "event": "PAPER_EXIT", "symbol": pos["symbol"],
                       "direction": pos["direction"], "long_strike": pos["long_strike"],
                       "short_strike": pos["short_strike"], "debit": debit, "exit_value": round(value, 2),
                       "pnl_usd": round(pnl, 2), "pnl_pct": round(pnl_pct, 1), "exit_reason": reason})
            removed.append(idx)
    for idx in reversed(removed): state["activePositions"].pop(idx)
    save_state(state)
    print(f"✅ Monitor complete: closed={len(removed)}, daily P&L=${state.get('dailyPnl', 0):+.2f}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
