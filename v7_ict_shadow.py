#!/usr/bin/env python3
"""Paper-only ICT shadow overlay for the V7 SPY/QQQ engine.

The overlay is intentionally isolated from ``v7_scalp_paper.py``. It reads the
same completed Yahoo 5-minute bars but never changes V7 state, entries, exits,
release identity, or broker paths. A candidate requires four auditable pieces:

1. liquidity sweep of a confirmed swing high/low;
2. displacement through the opposing structure level (MSS);
3. an unfilled fair value gap created by the displacement;
4. a premium/discount location check on that FVG.

This is a research/shadow signal, not a trade instruction or live executor.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
SCRIPTS = Path(__file__).resolve().parent
ROOT = Path("/path/to/project/.hermes")

ICT_SWING_LEFT = 2
ICT_SWING_RIGHT = 2
ICT_SWEEP_LOOKBACK_BARS = 8
ICT_RANGE_LOOKBACK_SWINGS = 20
ICT_ATR_LOOKBACK = 14
ICT_MIN_DISPLACEMENT_ATR = 0.80
ICT_BAR_INTERVAL_MINUTES = 5
ICT_MIN_REPLAY_BARS = 8
REGULAR_SESSION_START = 930
REGULAR_SESSION_END = 1555


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _bar_datetime(bar: dict[str, Any]) -> datetime | None:
    value = bar.get("dt")
    if isinstance(value, datetime):
        return value.astimezone(ET) if value.tzinfo else value.replace(tzinfo=ET)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.astimezone(ET) if parsed.tzinfo else parsed.replace(tzinfo=ET)
        except ValueError:
            return None
    return None


def _bar_time(bar: dict[str, Any]) -> str | None:
    value = _bar_datetime(bar)
    return value.isoformat() if value else None


def _valid_bar(bar: dict[str, Any]) -> bool:
    return all(_finite(bar.get(key)) for key in ("open", "high", "low", "close")) and float(bar["high"]) >= float(bar["low"])


def _true_range(current: dict[str, Any], previous: dict[str, Any] | None) -> float:
    high = float(current["high"])
    low = float(current["low"])
    if previous is None:
        return max(0.0, high - low)
    prev_close = float(previous["close"])
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def _atr(bars: list[dict[str, Any]], end_index: int, period: int = ICT_ATR_LOOKBACK) -> float:
    if end_index < 0 or not bars:
        return 0.0
    start = max(0, end_index - period + 1)
    ranges = [
        _true_range(bars[i], bars[i - 1] if i else None)
        for i in range(start, end_index + 1)
        if _valid_bar(bars[i])
    ]
    return sum(ranges) / len(ranges) if ranges else 0.0


def _base_result(bars: list[dict[str, Any]], symbol: str) -> dict[str, Any]:
    latest = bars[-1] if bars else {}
    return {
        "symbol": symbol.upper(),
        "signal": False,
        "direction": None,
        "reason": "insufficient_bars",
        "barTime": _bar_time(latest) if latest else None,
        "latestClose": float(latest["close"]) if _finite(latest.get("close")) else None,
        "paperOnly": True,
        "coreV7Unchanged": True,
        "strategy": "ICT liquidity sweep + MSS/displacement + FVG + premium_discount",
    }


def confirmed_swings(
    bars: list[dict[str, Any]], *, left: int = ICT_SWING_LEFT, right: int = ICT_SWING_RIGHT,
) -> list[dict[str, Any]]:
    """Return strict, right-confirmed swing highs and lows without lookahead."""
    swings: list[dict[str, Any]] = []
    if len(bars) < left + right + 1:
        return swings
    for index in range(left, len(bars) - right):
        if not _valid_bar(bars[index]):
            continue
        high = float(bars[index]["high"])
        low = float(bars[index]["low"])
        neighbors = bars[index - left:index] + bars[index + 1:index + right + 1]
        if all(_valid_bar(bar) for bar in neighbors):
            if high > max(float(bar["high"]) for bar in neighbors):
                swings.append({
                    "kind": "high",
                    "index": index,
                    "price": high,
                    "barTime": _bar_time(bars[index]),
                })
            if low < min(float(bar["low"]) for bar in neighbors):
                swings.append({
                    "kind": "low",
                    "index": index,
                    "price": low,
                    "barTime": _bar_time(bars[index]),
                })
    return sorted(swings, key=lambda item: (item["index"], item["kind"]))


def detect_liquidity_sweep(
    bars: list[dict[str, Any]], swings: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Find the latest confirmed sell-side or buy-side liquidity sweep.

    Bullish means a wick below a confirmed swing low followed by a close back
    above it. Bearish is the mirror image above a confirmed swing high.
    """
    if len(bars) < ICT_SWING_LEFT + ICT_SWING_RIGHT + 2:
        return None
    confirmed = swings if swings is not None else confirmed_swings(bars)
    start = max(0, len(bars) - ICT_SWEEP_LOOKBACK_BARS)
    candidates: list[dict[str, Any]] = []
    for index in range(start, len(bars)):
        bar = bars[index]
        if not _valid_bar(bar):
            continue
        prior = [item for item in confirmed if item["index"] < index]
        lows = [item for item in prior if item["kind"] == "low"]
        highs = [item for item in prior if item["kind"] == "high"]
        if lows and float(bar["low"]) < float(lows[-1]["price"]) and float(bar["close"]) > float(lows[-1]["price"]):
            level = lows[-1]
            candidates.append({
                "direction": "bullish",
                "side": "sell_side",
                "index": index,
                "level": float(level["price"]),
                "levelIndex": level["index"],
                "wickExtreme": float(bar["low"]),
                "close": float(bar["close"]),
                "barTime": _bar_time(bar),
            })
        if highs and float(bar["high"]) > float(highs[-1]["price"]) and float(bar["close"]) < float(highs[-1]["price"]):
            level = highs[-1]
            candidates.append({
                "direction": "bearish",
                "side": "buy_side",
                "index": index,
                "level": float(level["price"]),
                "levelIndex": level["index"],
                "wickExtreme": float(bar["high"]),
                "close": float(bar["close"]),
                "barTime": _bar_time(bar),
            })
    return max(candidates, key=lambda item: item["index"]) if candidates else None


def detect_market_structure_shift(
    bars: list[dict[str, Any]],
    sweep: dict[str, Any],
    swings: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Require post-sweep displacement through the opposing confirmed structure."""
    sweep_index = int(sweep["index"])
    direction = sweep.get("direction")
    confirmed = swings if swings is not None else confirmed_swings(bars)
    if direction == "bullish":
        levels = [item for item in confirmed if item["kind"] == "high" and item["index"] < sweep_index]
        predicate = lambda bar, level: float(bar["close"]) > float(level)
        body_predicate = lambda bar: float(bar["close"]) > float(bar["open"])
    elif direction == "bearish":
        levels = [item for item in confirmed if item["kind"] == "low" and item["index"] < sweep_index]
        predicate = lambda bar, level: float(bar["close"]) < float(level)
        body_predicate = lambda bar: float(bar["close"]) < float(bar["open"])
    else:
        return None
    if not levels:
        return None
    structure = levels[-1]
    structure_level = float(structure["price"])
    for index in range(sweep_index + 1, len(bars)):
        bar = bars[index]
        if not _valid_bar(bar) or not predicate(bar, structure_level) or not body_predicate(bar):
            continue
        body = abs(float(bar["close"]) - float(bar["open"]))
        atr_value = _atr(bars, index)
        minimum_body = max(atr_value * ICT_MIN_DISPLACEMENT_ATR, 1e-9)
        if body < minimum_body:
            continue
        return {
            "confirmed": True,
            "direction": direction,
            "index": index,
            "structureLevel": structure_level,
            "structureIndex": structure["index"],
            "close": float(bar["close"]),
            "body": body,
            "atr": atr_value,
            "bodyAtrMultiple": body / atr_value if atr_value > 0 else None,
            "barTime": _bar_time(bar),
        }
    return None


def detect_fvgs(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return three-candle FVGs and whether later bars fully filled them."""
    gaps: list[dict[str, Any]] = []
    for middle in range(1, len(bars) - 1):
        left = bars[middle - 1]
        right = bars[middle + 1]
        if not (_valid_bar(left) and _valid_bar(bars[middle]) and _valid_bar(right)):
            continue
        direction: str | None = None
        lower: float | None = None
        upper: float | None = None
        if float(left["high"]) < float(right["low"]):
            direction, lower, upper = "bullish", float(left["high"]), float(right["low"])
        elif float(left["low"]) > float(right["high"]):
            direction, lower, upper = "bearish", float(right["high"]), float(left["low"])
        if direction is None:
            continue
        later = bars[middle + 2:]
        if direction == "bullish":
            filled = any(_valid_bar(bar) and float(bar["low"]) <= lower for bar in later)
        else:
            filled = any(_valid_bar(bar) and float(bar["high"]) >= upper for bar in later)
        gaps.append({
            "direction": direction,
            "createdIndex": middle,
            "lower": lower,
            "upper": upper,
            "midpoint": (lower + upper) / 2.0,
            "filled": filled,
            "barTime": _bar_time(bars[middle]),
        })
    return gaps


def latest_unfilled_fvg(
    bars: list[dict[str, Any]], *, direction: str, after_index: int | None = None,
) -> dict[str, Any] | None:
    gaps = [
        gap for gap in detect_fvgs(bars)
        if gap["direction"] == direction
        and (after_index is None or gap["createdIndex"] >= after_index)
        and not gap["filled"]
    ]
    return max(gaps, key=lambda item: item["createdIndex"]) if gaps else None


def latest_dealing_range(
    bars: list[dict[str, Any]], swings: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    confirmed = swings if swings is not None else confirmed_swings(bars)
    confirmed = confirmed[-ICT_RANGE_LOOKBACK_SWINGS:]
    highs = [item for item in confirmed if item["kind"] == "high"]
    lows = [item for item in confirmed if item["kind"] == "low"]
    if not highs or not lows:
        return None
    high = max(highs, key=lambda item: item["index"])
    low = max(lows, key=lambda item: item["index"])
    high_price = float(high["price"])
    low_price = float(low["price"])
    if not high_price > low_price:
        return None
    equilibrium = (high_price + low_price) / 2.0
    return {
        "high": high_price,
        "highIndex": high["index"],
        "low": low_price,
        "lowIndex": low["index"],
        "equilibrium": equilibrium,
        "range": high_price - low_price,
    }


def evaluate_ict(bars: list[dict[str, Any]], *, symbol: str = "SPY") -> dict[str, Any]:
    """Evaluate the latest completed bar without mutating V7 state."""
    clean = [bar for bar in bars if isinstance(bar, dict) and _valid_bar(bar)]
    result = _base_result(clean, symbol)
    if len(clean) < ICT_MIN_REPLAY_BARS:
        return result
    swings = confirmed_swings(clean)
    result["confirmedSwings"] = len(swings)
    sweep = detect_liquidity_sweep(clean, swings)
    result["liquiditySweep"] = sweep
    if sweep is None:
        result["reason"] = "no_liquidity_sweep"
        return result
    result["direction"] = sweep["direction"]
    mss = detect_market_structure_shift(clean, sweep, swings)
    result["marketStructureShift"] = mss
    if mss is None:
        result["reason"] = "no_displacement_mss"
        return result
    result["displacement"] = mss
    all_gaps = [gap for gap in detect_fvgs(clean) if gap["direction"] == sweep["direction"] and gap["createdIndex"] >= mss["index"]]
    if not all_gaps:
        result["reason"] = "no_fvg"
        return result
    fvg = latest_unfilled_fvg(clean, direction=sweep["direction"], after_index=mss["index"])
    if fvg is None:
        result["fairValueGap"] = max(all_gaps, key=lambda item: item["createdIndex"])
        result["reason"] = "fvg_filled"
        return result
    result["fairValueGap"] = fvg
    # Anchor premium/discount to the pre-sweep dealing range. The sweep wick
    # itself is liquidity-taking behavior, not a new range boundary; including it
    # would move equilibrium in the direction of the signal and make the
    # location test self-referential.
    pre_sweep_swings = [item for item in swings if item["index"] < int(sweep["index"])]
    dealing_range = latest_dealing_range(clean, pre_sweep_swings)
    result["dealingRange"] = dealing_range
    if dealing_range is None:
        result["reason"] = "no_dealing_range"
        return result
    equilibrium = float(dealing_range["equilibrium"])
    midpoint = float(fvg["midpoint"])
    if sweep["direction"] == "bullish":
        location = "discount" if midpoint <= equilibrium else "premium"
        location_ok = location == "discount"
    else:
        location = "premium" if midpoint >= equilibrium else "discount"
        location_ok = location == "premium"
    result["location"] = location
    result["locationPrice"] = midpoint
    if not location_ok:
        result["reason"] = "premium_discount_mismatch"
        return result
    result["signal"] = True
    result["reason"] = "ict_confluence"
    result["signalBarIndex"] = len(clean) - 1
    result["sweepIndex"] = sweep["index"]
    result["mssIndex"] = mss["index"]
    return result


def apply_ict_overlay(
    setup: dict[str, Any], bars: list[dict[str, Any]], *, symbol: str = "SPY",
) -> dict[str, Any]:
    """Attach ICT context while preserving every core V7 setup field."""
    result = dict(setup)
    overlay = evaluate_ict(bars, symbol=symbol)
    result["ictOverlay"] = overlay
    result["ictPass"] = bool(overlay.get("signal"))
    result["ictDirection"] = overlay.get("direction")
    result["ictConfluence"] = bool(
        overlay.get("signal") and setup.get("direction") == overlay.get("direction")
    )
    result["ictPaperOnly"] = True
    return result


def _regular_session_bars(bars: Iterable[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    current = now.astimezone(ET) if now and now.tzinfo else now.replace(tzinfo=ET) if now else None
    result = []
    for bar in bars:
        when = _bar_datetime(bar)
        if when is None or when.weekday() >= 5:
            continue
        hhmm = when.hour * 100 + when.minute
        if not REGULAR_SESSION_START <= hhmm <= REGULAR_SESSION_END:
            continue
        if current is not None and when + timedelta(minutes=ICT_BAR_INTERVAL_MINUTES) > current:
            continue
        result.append(bar)
    return sorted(result, key=lambda bar: _bar_datetime(bar) or datetime.min.replace(tzinfo=ET))


def replay_ict(
    bars: list[dict[str, Any]], *, symbol: str = "SPY", now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Replay the overlay by session, using only bars available at each cutoff."""
    session_bars = _regular_session_bars(bars, now=now)
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bar in session_bars:
        when = _bar_datetime(bar)
        if when:
            by_session[when.date().isoformat()].append(bar)
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int, str]] = set()
    for session_date, day in sorted(by_session.items()):
        if len(day) < ICT_MIN_REPLAY_BARS:
            continue
        for cutoff in range(ICT_MIN_REPLAY_BARS, len(day) + 1):
            evaluation = evaluate_ict(day[:cutoff], symbol=symbol)
            if not evaluation.get("signal"):
                continue
            key = (
                session_date,
                int(evaluation.get("sweepIndex", -1)),
                int(evaluation.get("mssIndex", -1)),
                str(evaluation.get("direction")),
            )
            if key in seen:
                continue
            seen.add(key)
            signal_index = cutoff - 1
            close = float(day[signal_index]["close"])
            forward: dict[str, float | None] = {}
            for horizon in (1, 3, 6):
                future_index = signal_index + horizon
                forward[f"return_{horizon}bar_pct"] = (
                    round((float(day[future_index]["close"]) / close - 1.0) * 100.0, 4)
                    if future_index < len(day) and close > 0 else None
                )
            events.append({
                "eventId": f"{symbol.upper()}:{session_date}:{signal_index}:{evaluation['direction']}",
                "symbol": symbol.upper(),
                "sessionDate": session_date,
                "signalBarIndex": signal_index,
                "signalBarTime": evaluation.get("barTime"),
                "direction": evaluation.get("direction"),
                "sweepIndex": evaluation.get("sweepIndex"),
                "mssIndex": evaluation.get("mssIndex"),
                "fairValueGap": evaluation.get("fairValueGap"),
                "dealingRange": evaluation.get("dealingRange"),
                "location": evaluation.get("location"),
                **forward,
                "paperOnly": True,
            })
    return events


def replay_symbol(symbol: str, *, days: int = 30, now: datetime | None = None) -> dict[str, Any]:
    """Fetch delayed bars and produce a deterministic ICT shadow replay."""
    if symbol.upper() not in {"SPY", "QQQ"}:
        raise ValueError(f"unsupported_symbol:{symbol}")
    sys.path.insert(0, str(SCRIPTS))
    import v7_scalp_paper as engine  # import only; fetch_bars is read-only

    raw = engine.fetch_bars(symbol.upper(), days=days)
    events = replay_ict(raw, symbol=symbol.upper(), now=now)
    returns = {
        key: [event[key] for event in events if _finite(event.get(key))]
        for key in ("return_1bar_pct", "return_3bar_pct", "return_6bar_pct")
    }
    summary: dict[str, Any] = {
        "symbol": symbol.upper(),
        "daysRequested": days,
        "barsFetched": len(raw),
        "regularBarsUsed": len(_regular_session_bars(raw, now=now)),
        "sessionsWithSignals": len({event["sessionDate"] for event in events}),
        "signalCount": len(events),
        "directions": dict(Counter(event["direction"] for event in events)),
        "forwardReturnSamples": {key: len(values) for key, values in returns.items()},
        "forwardReturnMeanPct": {
            key: round(sum(values) / len(values), 4) if values else None
            for key, values in returns.items()
        },
        "source": "Yahoo Finance delayed completed 5-minute bars",
        "paperOnly": True,
        "coreV7Unchanged": True,
        "events": events,
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="V7 ICT paper-only shadow replay")
    parser.add_argument("--symbol", choices=["SPY", "QQQ", "ALL"], default="ALL")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    symbols = ("SPY", "QQQ") if args.symbol == "ALL" else (args.symbol,)
    payload = {symbol: replay_symbol(symbol, days=args.days) for symbol in symbols}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
