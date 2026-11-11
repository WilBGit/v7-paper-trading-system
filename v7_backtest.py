#!/usr/bin/env python3
"""
V7.3 Backtest — ORB A/B comparison with BSM pricing.
Tests whether ORB improves directional accuracy from ~52% baseline.

Two passes:
  1. Without ORB (orbLookbackBars=0, no ORB bonus)
  2. With ORB (orbLookbackBars=6, +1 score for breakout alignment)

Metrics: win rate, directional accuracy, P&L (after spread), expectancy, max DD.
"""
from __future__ import annotations

import json
import math
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Import scanner functions (avoid duplication)
sys.path.insert(0, str(Path(__file__).parent))
from v7_scanner import (
    bsm_price, bsm_delta, select_strike, calc_ema, calc_rsi,
    calc_vwap, calc_volume_ratio, calc_orb, score_signal,
    fetch_bars,
    GATES, RISK, DTE, SPREAD_PCT, COMMISSION, RISK_FREE_RATE,
    TARGET_DELTA, MAX_PREMIUM, SYMBOLS, MACRO_DATES_2026,
    ET, PT,
)

PT_TZ = PT
ET_TZ = ET

# ═══════════════════════════════════════════════════════════════
# DATA FETCH — uses scanner's fetch_bars() (proven working)
# ═══════════════════════════════════════════════════════════════

def fetch_5min(symbol: str, days: int = 60) -> list[dict]:
    """Wrapper around scanner's fetch_bars — converts format + filters to RTH."""
    import time as _time
    raw = fetch_bars(symbol, interval="5m", days=min(days, 60))
    if not raw:
        _time.sleep(2)
        raw = fetch_bars(symbol, interval="5m", days=min(days, 60))
    if not raw:
        return []
    # Convert scanner format → backtest format, filter to regular session
    converted = []
    for b in raw:
        dt = b["dt"]  # scanner stores datetime with tz
        # RTH only: 9:30 AM - 4:00 PM ET
        if dt.hour < 9 or (dt.hour == 9 and dt.minute < 30):
            continue
        if dt.hour >= 16:
            continue
        converted.append({
            "ts": dt.isoformat(),
            "open": b["open"], "high": b["high"], "low": b["low"],
            "close": b["close"], "volume": b["volume"],
        })
    return converted


# ═══════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════

def split_into_days(bars: list[dict]) -> list[list[dict]]:
    """Split bars into per-day lists."""
    days = []
    current_day = []
    current_date = None
    for bar in bars:
        dt = datetime.fromisoformat(bar["ts"])
        bar_date = dt.date()
        if current_date is None:
            current_date = bar_date
        if bar_date != current_date:
            if current_day:
                days.append(current_day)
            current_day = []
            current_date = bar_date
        current_day.append(bar)
    if current_day:
        days.append(current_day)
    return days


def check_exit(
    position: dict,
    current_option_price: float,
    current_underlying: float,
    mock_now: datetime,
    bars_held: int,
) -> str | None:
    """Check V7.3 exit conditions. Returns reason string or None."""
    entry_price = position["entry_price_per_share"]  # per-share, includes spread
    pnl_pct = (current_option_price - entry_price) / entry_price * 100 if entry_price > 0 else 0

    # 1. Hard stop (40%)
    if pnl_pct <= -RISK["hardStopPct"]:
        return f"hard_stop ({pnl_pct:.1f}%)"

    # 2. Stop loss (30%) — with min hold
    if pnl_pct <= -RISK["stopLossPct"] and bars_held >= RISK["minHoldBars"]:
        return f"stop_loss ({pnl_pct:.1f}%)"

    # 3. Profit target (50%)
    if pnl_pct >= RISK["profitTargetPct"]:
        return f"profit_target ({pnl_pct:.1f}%)"

    # 4. Trailing stop (15% start, 10% trail)
    hwm = max(position["high_water_mark"], current_option_price)
    position["high_water_mark"] = hwm
    if pnl_pct >= RISK["trailingStartPct"]:
        hwm_pnl = (hwm - entry_price) / entry_price * 100 if entry_price > 0 else 0
        dd = hwm_pnl - pnl_pct
        if dd >= RISK["trailingStopPct"]:
            return f"trailing_stop ({pnl_pct:.1f}%, dd={dd:.1f}%)"

    # 5. Stale exit
    if bars_held >= RISK["staleBars"] and abs(pnl_pct) < RISK["staleThresholdPct"]:
        return f"stale_exit ({pnl_pct:.1f}%)"

    # 6. Underlying stop (0.60%)
    direction = position["direction"]
    entry_underlying = position["entry_underlying"]
    if entry_underlying > 0:
        u_move = (current_underlying - entry_underlying) / entry_underlying * 100
        if direction == "bullish" and u_move <= -RISK["underlyingStopPct"] and bars_held >= RISK["minHoldBars"]:
            return f"underlying_stop (u={u_move:.2f}%)"
        if direction == "bearish" and u_move >= RISK["underlyingStopPct"] and bars_held >= RISK["minHoldBars"]:
            return f"underlying_stop (u={u_move:.2f}%)"

    # 7. Max hold (36 bars = 3 hours)
    if bars_held >= RISK["maxHoldBars"]:
        return f"max_hold ({pnl_pct:.1f}%)"

    # 8. Hard close at 3:45 PM PT (15:45)
    if mock_now.hour >= 15 and mock_now.minute >= 45:
        return f"time_stop ({mock_now.strftime('%H:%M')} PT, pnl={pnl_pct:.1f}%)"
    if mock_now.hour >= 16:
        return f"time_stop ({mock_now.strftime('%H:%M')} PT)"

    return None


IV_MAP = {"SPY": 0.13, "QQQ": 0.19}  # Realistic per-symbol IV


def run_backtest(bars_all: list[dict], symbol: str, use_orb: bool, iv: float | None = None) -> list[dict]:
    """Run backtest for one symbol. Returns list of trade results."""
    days = split_into_days(bars_all)
    trades = []
    max_trades_per_day = 2
    max_same_symbol = 1

    if iv is None:
        iv = IV_MAP.get(symbol, 0.15)

    for day_bars in days:
        if len(day_bars) < 25:
            continue

        # Skip macro event days
        dt = datetime.fromisoformat(day_bars[0]["ts"])
        date_str = dt.strftime("%Y-%m-%d")
        if date_str in MACRO_DATES_2026:
            continue

        # Skip Friday PM (after 12:00 PT = 15:00 ET)
        is_friday = dt.weekday() == 4

        trades_today = 0
        same_symbol_count = 0
        position = None

        # Entry window: 6:40 AM - 1:45 PM PT
        # ORB lookback: first 6 bars (9:30 - 10:00 ET) — hardcoded, not read from GATES
        orb_lookback = 6 if use_orb else 0

        for i in range(len(day_bars)):
            bar = day_bars[i]
            bar_dt = datetime.fromisoformat(bar["ts"])

            # Convert to PT for entry window check
            bar_pt = bar_dt.astimezone(PT_TZ)

            # Skip Friday PM entries (after 12:00 PT = 15:00 ET)
            if is_friday and bar_pt.hour >= 12:
                if position:
                    # Still manage exits on Friday PM
                    pass
                else:
                    continue

            # Entry window: 6:40 AM - 1:45 PM PT
            in_entry_window = (
                (bar_pt.hour > 6 or (bar_pt.hour == 6 and bar_pt.minute >= 40))
                and (bar_pt.hour < 13 or (bar_pt.hour == 13 and bar_pt.minute <= 45))
            )

            # Manage open position
            if position:
                bars_held = i - position["entry_bar_idx"]
                current_underlying = bar["close"]

                # BSM price with remaining time to expiry
                T_remaining = max(0, position["T_entry"] - bars_held * 5 / (365.25 * 24 * 60))
                opt_type = position["option_type"]
                current_option_price = bsm_price(
                    current_underlying, position["strike"],
                    T_remaining, RISK_FREE_RATE, iv, opt_type
                )
                # Apply half spread on exit (we get worse fills)
                current_option_price *= (1 - SPREAD_PCT / 2)
                current_option_price = max(0.01, current_option_price)

                reason = check_exit(
                    position, current_option_price, current_underlying,
                    bar_pt, bars_held
                )

                if reason:
                    # Close position
                    entry_cost_per_share = position["entry_price_per_share"]
                    pnl_usd = (current_option_price - entry_cost_per_share) * 100  # per contract
                    pnl_pct = (current_option_price - entry_cost_per_share) / entry_cost_per_share * 100

                    # Directional accuracy: did the direction match the outcome?
                    direction_correct = (pnl_usd > 0)

                    trades.append({
                        "symbol": symbol,
                        "date": date_str,
                        "direction": position["direction"],
                        "entry_bar": position["entry_bar_idx"],
                        "exit_bar": i,
                        "bars_held": bars_held,
                        "entry_underlying": position["entry_underlying"],
                        "exit_underlying": current_underlying,
                        "strike": position["strike"],
                        "option_type": position["option_type"],
                        "entry_price": entry_cost_per_share,
                        "exit_price": current_option_price,
                        "pnl_usd": round(pnl_usd, 2),
                        "pnl_pct": round(pnl_pct, 1),
                        "direction_correct": direction_correct,
                        "exit_reason": reason,
                        "score": position["score"],
                        "use_orb": use_orb,
                    })
                    position = None

            # Try entry (if no position and in window)
            if position is None and in_entry_window and trades_today < max_trades_per_day:
                if same_symbol_count >= max_same_symbol:
                    continue
                if i < max(25, orb_lookback + 1):
                    continue  # need enough data for signal

                # Build data up to current bar (no look-ahead!)
                closes = [b["close"] for b in day_bars[:i+1]]
                volumes = [b["volume"] for b in day_bars[:i+1]]
                bars_so_far = day_bars[:i+1]

                # ORB: only pass day_bars if use_orb and we have enough bars
                day_bars_for_orb = bars_so_far if use_orb else None

                # Temporarily modify GATES for ORB on/off
                original_orb = GATES.get("orbLookbackBars", 6)
                try:
                    if use_orb:
                        GATES["orbLookbackBars"] = 6
                    else:
                        GATES["orbLookbackBars"] = 9999  # skip ORB (guard fails cleanly)

                    signal = score_signal(closes, volumes, bars_so_far, symbol, day_bars=day_bars_for_orb)
                finally:
                    GATES["orbLookbackBars"] = original_orb  # always restore

                if signal.get("hard_block"):
                    continue
                if signal["score"] < GATES["minScore"]:
                    continue
                if signal["direction"] is None:
                    continue

                direction = signal["direction"]
                entry_underlying = bar["close"]

                # BSM pricing
                option_type = "call" if direction == "bullish" else "put"
                T_entry = DTE / 365.25  # 2 days to expiry in years

                strike = select_strike(
                    entry_underlying, direction, T_entry,
                    iv, RISK_FREE_RATE, TARGET_DELTA, MAX_PREMIUM
                )
                if strike is None:
                    continue

                entry_premium = bsm_price(entry_underlying, strike, T_entry, RISK_FREE_RATE, iv, option_type)
                entry_premium_per_contract = entry_premium * 100

                if entry_premium_per_contract > MAX_PREMIUM:
                    continue
                if entry_premium_per_contract < 1:
                    continue

                # Entry cost includes half spread + commission
                entry_cost = entry_premium * (1 + SPREAD_PCT / 2)
                entry_cost_per_share = entry_cost
                entry_cost_per_contract = entry_cost * 100 + COMMISSION

                # Create position
                position = {
                    "symbol": symbol,
                    "direction": direction,
                    "strike": strike,
                    "option_type": option_type,
                    "entry_underlying": entry_underlying,
                    "entry_price_per_share": entry_cost_per_share,
                    "entry_cost_per_contract": entry_cost_per_contract,
                    "entry_bar_idx": i,
                    "T_entry": T_entry,
                    "score": signal["score"],
                    "high_water_mark": entry_cost_per_share,
                }
                trades_today += 1
                same_symbol_count += 1

        # End of day: force close any open position
        if position:
            bar = day_bars[-1]
            current_underlying = bar["close"]
            bars_held = len(day_bars) - 1 - position["entry_bar_idx"]
            T_remaining = max(0, position["T_entry"] - bars_held * 5 / (365.25 * 24 * 60))
            current_option_price = bsm_price(
                current_underlying, position["strike"],
                T_remaining, RISK_FREE_RATE, iv, position["option_type"]
            )
            current_option_price *= (1 - SPREAD_PCT / 2)
            current_option_price = max(0.01, current_option_price)

            entry_cost_per_share = position["entry_price_per_share"]
            pnl_usd = (current_option_price - entry_cost_per_share) * 100
            pnl_pct = (current_option_price - entry_cost_per_share) / entry_cost_per_share * 100

            trades.append({
                "symbol": symbol,
                "date": date_str,
                "direction": position["direction"],
                "entry_bar": position["entry_bar_idx"],
                "exit_bar": len(day_bars) - 1,
                "bars_held": bars_held,
                "entry_underlying": position["entry_underlying"],
                "exit_underlying": current_underlying,
                "strike": position["strike"],
                "option_type": position["option_type"],
                "entry_price": entry_cost_per_share,
                "exit_price": current_option_price,
                "pnl_usd": round(pnl_usd, 2),
                "pnl_pct": round(pnl_pct, 1),
                "direction_correct": pnl_usd > 0,
                "exit_reason": "EOD_close",
                "score": position["score"],
                "use_orb": use_orb,
            })
            position = None

    return trades


# ═══════════════════════════════════════════════════════════════
# ANALYSIS
# ═══════════════════════════════════════════════════════════════

def analyze_trades(trades: list[dict], label: str) -> dict:
    if not trades:
        print(f"\n{'='*60}")
        print(f"  {label}: NO TRADES")
        return {"label": label, "trades": 0}

    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    directional_correct = [t for t in trades if t["direction_correct"]]

    total_pnl = sum(t["pnl_usd"] for t in trades)
    avg_win = sum(t["pnl_usd"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_usd"] for t in losses) / len(losses) if losses else 0
    expectancy = total_pnl / len(trades) if trades else 0

    # Max drawdown
    running_pnl = 0
    peak = 0
    max_dd = 0
    for t in trades:
        running_pnl += t["pnl_usd"]
        peak = max(peak, running_pnl)
        dd = running_pnl - peak
        max_dd = min(max_dd, dd)

    win_rate = len(wins) / len(trades) * 100
    dir_accuracy = len(directional_correct) / len(trades) * 100

    # Exit reason breakdown
    reasons = {}
    for t in trades:
        r = t["exit_reason"].split("(")[0].strip()
        reasons[r] = reasons.get(r, 0) + 1

    # Direction breakdown
    bull_trades = [t for t in trades if t["direction"] == "bullish"]
    bear_trades = [t for t in trades if t["direction"] == "bearish"]
    bull_wins = [t for t in bull_trades if t["pnl_usd"] > 0]
    bear_wins = [t for t in bear_trades if t["pnl_usd"] > 0]

    results = {
        "label": label,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "directional_accuracy": round(dir_accuracy, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(expectancy, 2),
        "max_drawdown": round(max_dd, 2),
        "win_loss_ratio": round(avg_win / abs(avg_loss), 2) if avg_loss != 0 else 0,
        "bull_trades": len(bull_trades),
        "bull_win_rate": round(len(bull_wins) / len(bull_trades) * 100, 1) if bull_trades else 0,
        "bear_trades": len(bear_trades),
        "bear_win_rate": round(len(bear_wins) / len(bear_trades) * 100, 1) if bear_trades else 0,
        "exit_reasons": reasons,
    }

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Trades:           {len(trades)}")
    print(f"  Win Rate:          {win_rate:.1f}%")
    print(f"  Dir Accuracy:     {dir_accuracy:.1f}%")
    print(f"  Total P&L:         ${total_pnl:+.2f}")
    print(f"  Avg Win:           ${avg_win:+.2f}")
    print(f"  Avg Loss:          ${avg_loss:+.2f}")
    print(f"  Expectancy:        ${expectancy:+.2f}/trade")
    print(f"  Max Drawdown:      ${max_dd:.2f}")
    print(f"  Win/Loss Ratio:    {results['win_loss_ratio']}")
    print(f"  Bull: {len(bull_trades)} trades, {results['bull_win_rate']}% WR")
    print(f"  Bear: {len(bear_trades)} trades, {results['bear_win_rate']}% WR")
    print(f"  Exits: {reasons}")

    return results


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    print("📊 V7.3 Backtest — ORB A/B Comparison")
    print(f"   IV: per-symbol (SPY=13%, QQQ=19%) | Spread: {SPREAD_PCT*100}% | Commission: ${COMMISSION}")
    print(f"   DTE: {DTE} | Delta: {TARGET_DELTA} | Max Premium: ${MAX_PREMIUM}")
    print()

    all_results = {}
    cached_bars = {}  # Cache to avoid double-fetch

    for symbol in SYMBOLS:
        print(f"\n{'─'*60}")
        print(f"  Fetching {symbol} 5-min data (65 days)...")
        bars = fetch_5min(symbol, days=65)
        if not bars:
            print(f"  ❌ No data for {symbol} — skipping")
            continue
        print(f"  ✅ {len(bars)} bars, {bars[0]['ts'][:10]} to {bars[-1]['ts'][:10]}")
        cached_bars[symbol] = bars

        # Pass 1: Without ORB
        print(f"\n  Pass 1: WITHOUT ORB...")
        trades_no_orb = run_backtest(bars, symbol, use_orb=False)
        r1 = analyze_trades(trades_no_orb, f"{symbol} — No ORB")

        # Pass 2: With ORB
        print(f"\n  Pass 2: WITH ORB...")
        trades_orb = run_backtest(bars, symbol, use_orb=True)
        r2 = analyze_trades(trades_orb, f"{symbol} — With ORB")

        all_results[symbol] = {"no_orb": r1, "with_orb": r2}

    # Combined summary (reuse cached bars — no re-fetch)
    print(f"\n{'═'*60}")
    print(f"  COMBINED SUMMARY (SPY + QQQ)")
    print(f"{'═'*60}")

    combined_no = []
    combined_orb = []
    for symbol in SYMBOLS:
        bars = cached_bars.get(symbol)
        if bars:
            combined_no.extend(run_backtest(bars, symbol, use_orb=False))
            combined_orb.extend(run_backtest(bars, symbol, use_orb=True))

    r_combined_no = analyze_trades(combined_no, "COMBINED — No ORB")
    r_combined_orb = analyze_trades(combined_orb, "COMBINED — With ORB")

    # Delta comparison
    print(f"\n{'═'*60}")
    print(f"  ORB IMPACT (Δ)")
    print(f"{'═'*60}")
    if r_combined_no.get("trades", 0) > 0 and r_combined_orb.get("trades", 0) > 0:
        d_trades = r_combined_orb["trades"] - r_combined_no["trades"]
        d_wr = r_combined_orb["win_rate"] - r_combined_no["win_rate"]
        d_dir = r_combined_orb["directional_accuracy"] - r_combined_no["directional_accuracy"]
        d_pnl = r_combined_orb["total_pnl"] - r_combined_no["total_pnl"]
        d_exp = r_combined_orb["expectancy"] - r_combined_no["expectancy"]

        print(f"  Δ Trades:          {d_trades:+d}")
        print(f"  Δ Win Rate:        {d_wr:+.1f}pp")
        print(f"  Δ Dir Accuracy:    {d_dir:+.1f}pp")
        print(f"  Δ Total P&L:       ${d_pnl:+.2f}")
        print(f"  Δ Expectancy:      ${d_exp:+.2f}/trade")

        verdict = "ORB HELPS ✅" if (d_dir > 0 and d_pnl > 0) else "ORB DOES NOT HELP ❌" if d_dir <= 0 else "MIXED ⚠️"
        print(f"\n  Verdict: {verdict}")
    else:
        print("  Insufficient data for comparison")

    # Save results
    output = {
        "timestamp": datetime.now(PT_TZ).isoformat(),
        "config": {
            "iv": 0.20, "spread": SPREAD_PCT, "commission": COMMISSION,
            "dte": DTE, "delta": TARGET_DELTA, "max_premium": MAX_PREMIUM,
        },
        "per_symbol": all_results,
        "combined": {"no_orb": r_combined_no, "with_orb": r_combined_orb},
    }

    out_path = Path("/path/to/project/.hermes/state/v7_backtest_results.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n📁 Results saved to {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
