#!/usr/bin/env python3
"""Paper-only SPY iron-condor and iron-butterfly engine.

This module is intentionally additive to V7.4.23. It never submits orders and
keeps its evidence identity, risk limits, state, and ledger separate from the
existing single-leg V7 strategy.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import v7_scalp_paper as v7

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
SYMBOL = "SPY"
STRATEGY_VERSION = "7.4.23-spy-credit-neutral-paper"
STRATEGY_ID = "v7423-spy-credit-neutral-paper"
LIVE_ORDERS_ENABLED = False
PROMOTION_MIN_TRADES = 10
MAX_RISK_PER_TRADE = 40.0
MAX_DAILY_LOSS = 50.0
MAX_OPEN_POSITIONS = 1
MAX_QUOTE_AGE_SECONDS = 15.0
MAX_LEG_TIMESTAMP_SPREAD_SECONDS = 3.0
DEFAULT_DTE = 1
BRIDGE_MAX_STRIKE_DISTANCE = 5.0
SIGNAL_RECHECK_SECONDS = 300.0
# User-approved Pacific-time schedule: 06:30–13:00 PDT.
# Stored in venue time because all option-session predicates use ET.
# Entry window is 09:30–16:00 ET per user spec.  A separate entry guard
# in scan_once rejects new entries when fewer than MIN_HOLD_MINUTES remain
# before hard close, so a late entry is not instantly hard-closed.
ENTRY_START_ET = time(9, 30)
ENTRY_END_ET = time(16, 0)
HARD_CLOSE_ET = time(16, 0)
MIN_TIME_TO_CLOSE_MINUTES = 30.0

LEG_KEYS = ("long_put", "short_put", "short_call", "long_call")
STRUCTURES = ("iron_condor", "iron_butterfly")
PROFIT_TARGET_FRACTION = {
    "iron_condor": 0.50,
    "iron_butterfly": 0.35,
}
STOP_MAX_RISK_FRACTION = {
    "iron_condor": 0.50,
    "iron_butterfly": 0.50,
}


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _number(value: Any, default: float | None = None) -> float | None:
    return float(value) if _finite(value) else default


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _now_et(value: datetime | None = None) -> datetime:
    current = value or datetime.now(tz=ET)
    if current.tzinfo is None:
        current = current.replace(tzinfo=ET)
    return current.astimezone(ET)


def _hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def state_path() -> Path:
    return _hermes_home() / "state" / "v7_spy_credit_spreads_state.json"


def ledger_path() -> Path:
    return _hermes_home() / "state" / "v7_spy_credit_spreads_trades.jsonl"


def _get(mapping: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _quote_identity(quote: Mapping[str, Any], key: str) -> tuple[str, str, float, str] | None:
    symbol = str(_get(quote, "symbol", default="")).upper()
    option_type = str(_get(quote, "optionType", "option_type", "type", default="")).lower()
    strike = _number(_get(quote, "strike"))
    expiry = str(_get(quote, "expiry", "expirationDate", "expiration", default=""))
    if symbol != SYMBOL or option_type not in {"put", "call"} or strike is None or not expiry:
        return None
    return symbol, option_type, strike, expiry


def validate_leg_quote(quote: Mapping[str, Any], key: str, *, now: datetime | None = None) -> tuple[bool, str]:
    identity = _quote_identity(quote, key)
    if identity is None:
        return False, f"{key}:invalid_identity"
    bid = _number(_get(quote, "bid"))
    ask = _number(_get(quote, "ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return False, f"{key}:invalid_bid_ask"
    if not _get(quote, "instrumentId", "instrument_id", "occSymbol", "occ_symbol"):
        return False, f"{key}:missing_instrument_identity"
    updated = _as_datetime(_get(quote, "updatedAt", "updated_at", "quoteTime", "quote_time"))
    if updated is None:
        return False, f"{key}:missing_timestamp"
    if now is not None:
        age = (_now_et(now).astimezone(UTC) - updated.astimezone(UTC)).total_seconds()
        if age < -2 or age > MAX_QUOTE_AGE_SECONDS:
            return False, f"{key}:stale_quote"
    return True, "ok"


def validate_quote_bundle(legs: Mapping[str, Mapping[str, Any]], *, now: datetime | None = None) -> tuple[bool, list[str]]:
    errors: list[str] = []
    expected_types = {
        "long_put": "put", "short_put": "put",
        "short_call": "call", "long_call": "call",
    }
    for key in LEG_KEYS:
        quote = legs.get(key)
        if not isinstance(quote, Mapping):
            errors.append(f"{key}:missing")
            continue
        valid, reason = validate_leg_quote(quote, key, now=now)
        if not valid:
            errors.append(reason)
        actual_type = str(_get(quote, "optionType", "option_type", "type", default="")).lower()
        if actual_type != expected_types[key]:
            errors.append(f"{key}:option_type_mismatch")
    if errors:
        return False, errors
    identities = [_quote_identity(legs[key], key) for key in LEG_KEYS]
    assert all(identity is not None for identity in identities)
    expiries = {identity[3] for identity in identities if identity is not None}
    if len(expiries) != 1:
        errors.append("legs:expiry_mismatch")
    if len({str(_get(legs[key], "symbol", default="")).upper() for key in LEG_KEYS}) != 1:
        errors.append("legs:symbol_mismatch")
    timestamps = [
        _as_datetime(_get(legs[key], "updatedAt", "updated_at", "quoteTime", "quote_time"))
        for key in LEG_KEYS
    ]
    if all(value is not None for value in timestamps):
        observed = [value.astimezone(UTC) for value in timestamps if value is not None]
        if (max(observed) - min(observed)).total_seconds() > MAX_LEG_TIMESTAMP_SPREAD_SECONDS:
            errors.append("legs:timestamp_spread_too_wide")
    return not errors, errors


def _entry_credit(legs: Mapping[str, Mapping[str, Any]]) -> float:
    return (
        float(legs["short_put"]["bid"])
        + float(legs["short_call"]["bid"])
        - float(legs["long_put"]["ask"])
        - float(legs["long_call"]["ask"])
    )


def _exit_debit(legs: Mapping[str, Mapping[str, Any]]) -> float:
    return (
        float(legs["short_put"]["ask"])
        + float(legs["short_call"]["ask"])
        - float(legs["long_put"]["bid"])
        - float(legs["long_call"]["bid"])
    )


def _widths(legs: Mapping[str, Mapping[str, Any]]) -> tuple[float, float]:
    put_width = float(legs["short_put"]["strike"]) - float(legs["long_put"]["strike"])
    call_width = float(legs["long_call"]["strike"]) - float(legs["short_call"]["strike"])
    return put_width, call_width


def _serializable_legs(legs: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {key: copy.deepcopy(dict(legs[key])) for key in LEG_KEYS}


def build_position(
    structure: str,
    legs: Mapping[str, Mapping[str, Any]],
    *,
    underlying: float,
    setup: Mapping[str, Any],
    entered_at: datetime,
    max_risk: float = MAX_RISK_PER_TRADE,
    quote_now: datetime | None = None,
) -> dict[str, Any] | None:
    """Build a paper position using executable entry-side prices only."""
    if structure not in STRUCTURES or not _finite(underlying) or underlying <= 0:
        return None
    valid, _ = validate_quote_bundle(legs, now=quote_now)
    if not valid:
        return None
    long_put = float(legs["long_put"]["strike"])
    short_put = float(legs["short_put"]["strike"])
    short_call = float(legs["short_call"]["strike"])
    long_call = float(legs["long_call"]["strike"])
    put_width, call_width = _widths(legs)
    if not (long_put < short_put < underlying < short_call < long_call):
        if structure == "iron_butterfly":
            if not (long_put < short_put == short_call < long_call):
                return None
        else:
            return None
    if min(put_width, call_width) <= 0:
        return None
    if structure == "iron_butterfly" and abs(put_width - call_width) > 0.01:
        return None
    credit = _entry_credit(legs)
    if credit <= 0:
        return None
    max_profit = round(credit * 100.0, 2)
    max_loss = round(max(max(put_width, call_width) * 100.0 - max_profit, 0.0), 2)
    if max_loss <= 0 or max_loss > float(max_risk) + 1e-9:
        return None
    entered = _now_et(entered_at)
    body_strike = short_put if structure == "iron_butterfly" else None
    position = {
        "positionId": f"{STRATEGY_ID}-{uuid.uuid4().hex[:12]}",
        "strategyId": STRATEGY_ID,
        "strategyVersion": STRATEGY_VERSION,
        "symbol": SYMBOL,
        "structure": structure,
        "mode": "paper",
        "liveOrdersEnabled": False,
        "enteredAt": entered.isoformat(),
        "expiry": str(legs["short_put"].get("expiry", "")),
        "initialUnderlying": float(underlying),
        "entryCredit": credit,
        "entryCreditDollars": max_profit,
        "maxProfit": max_profit,
        "maxLoss": max_loss,
        "putWingWidth": put_width,
        "callWingWidth": call_width,
        "bodyStrike": body_strike,
        "profitTargetDollars": max_profit * PROFIT_TARGET_FRACTION[structure],
        "stopLossDollars": max_loss * STOP_MAX_RISK_FRACTION[structure],
        "breakEvenLower": short_put - credit,
        "breakEvenUpper": short_call + credit,
        "entryLegs": _serializable_legs(legs),
        "lastQuoteAt": max(
            (_as_datetime(_get(legs[key], "updatedAt", "updated_at")) or entered)
            for key in LEG_KEYS
        ).isoformat(),
        "v7Setup": copy.deepcopy(dict(setup)),
    }
    return position


def position_metrics(position: Mapping[str, Any], current_legs: Mapping[str, Mapping[str, Any]]) -> dict[str, float]:
    valid, errors = validate_quote_bundle(current_legs)
    if not valid:
        raise ValueError("invalid current spread quote: " + ",".join(errors))
    exit_debit = _exit_debit(current_legs)
    entry_credit = float(position["entryCredit"])
    pnl = (entry_credit - exit_debit) * 100.0
    return {
        "exitDebit": exit_debit,
        "pnl": pnl,
        "profitTargetDollars": float(position["profitTargetDollars"]),
        "stopLossDollars": float(position["stopLossDollars"]),
    }


def _directional_veto(setup: Mapping[str, Any]) -> list[str]:
    vetoes: list[str] = []
    direction = _get(setup, "direction", default=None)
    raw_direction = _get(setup, "rawDirection", "raw_direction", default=None)
    bull = _number(_get(setup, "bullScore", "bull_score", default=0), 0.0) or 0.0
    bear = _number(_get(setup, "bearScore", "bear_score", default=0), 0.0) or 0.0
    if direction in {"bullish", "bearish"}:
        vetoes.append("v7_qualified_direction")
    if raw_direction in {"bullish", "bearish"} and abs(bull - bear) >= 2:
        vetoes.append("v7_raw_directional_edge")
    if abs(bull - bear) > 1:
        vetoes.append("v7_score_imbalance")
    if _get(setup, "premarketBreakout", "premarket_breakout", default=False):
        vetoes.append("premarket_breakout")
    if _get(setup, "openingContinuation", "opening_continuation", default=False):
        vetoes.append("opening_continuation")
    turtle = _get(setup, "turtle", default={}) or {}
    if _get(turtle, "longBreakout", "long_breakout", default=False):
        vetoes.append("turtle_long_breakout")
    if _get(turtle, "shortBreakout", "short_breakout", default=False):
        vetoes.append("turtle_short_breakout")
    if _get(setup, "crashSignal", "crash_signal", default=False):
        vetoes.append("crash_detector")
    if _get(setup, "pumpSignal", "pump_signal", default=False):
        vetoes.append("pump_detector")
    return vetoes


def range_regime(setup: Mapping[str, Any]) -> dict[str, Any]:
    """Apply every V7.4.23 signal family as a neutral-range gate."""
    vetoes = _directional_veto(setup)
    reasons: list[str] = []
    close = _number(_get(setup, "close"))
    vwap = _number(_get(setup, "vwap"))
    vwap_upper = _number(_get(setup, "vwapUpper", "vwap_upper"))
    vwap_lower = _number(_get(setup, "vwapLower", "vwap_lower"))
    rsi = _number(_get(setup, "rsi"))
    relative_volume = _number(_get(setup, "relativeVolume", "relative_volume"), 0.0) or 0.0
    ema9 = _number(_get(setup, "ema9"))
    ema21 = _number(_get(setup, "ema21"))
    supertrend = str(_get(setup, "supertrend", default="")).lower()
    macd_hist = _number(_get(setup, "macdHist", "macd_hist"), 0.0) or 0.0
    momentum_pct = _number(_get(setup, "momentumPct", "momentum_pct"), 0.0) or 0.0
    turtle = _get(setup, "turtle", default={}) or {}
    atr = _number(_get(turtle, "n", "atr"), 0.0) or 0.0

    inside_vwap_band = (
        close is not None
        and vwap is not None
        and vwap_lower is not None
        and vwap_upper is not None
        and vwap_lower <= close <= vwap_upper
    )
    if not inside_vwap_band:
        vetoes.append("outside_vwap_band")
    else:
        reasons.append("inside_vwap_band")
    if rsi is None or not 40.0 <= rsi <= 60.0:
        vetoes.append("rsi_not_neutral")
    else:
        reasons.append("rsi_neutral")
    if relative_volume > 1.50:
        vetoes.append("relative_volume_expansion")
    else:
        reasons.append("relative_volume_not_expanded")
    if ema9 is not None and ema21 is not None:
        ema_up = ema9 > ema21
        ema_down = ema9 < ema21
        supertrend_up = supertrend in {"up", "bullish", "long"}
        supertrend_down = supertrend in {"down", "bearish", "short"}
        if (ema_up and supertrend_up) or (ema_down and supertrend_down):
            vetoes.append("ema_supertrend_alignment")
        else:
            reasons.append("ema_supertrend_not_aligned")
    if atr > 0:
        if abs(macd_hist) > atr * 0.50:
            vetoes.append("macd_expansion")
        else:
            reasons.append("macd_near_neutral")
        if close is not None and abs(close * momentum_pct / 100.0) > atr * 0.50:
            vetoes.append("momentum_expansion")
        else:
            reasons.append("momentum_near_neutral")
    elif abs(macd_hist) > 0.10 or abs(momentum_pct) > 0.10:
        vetoes.append("momentum_inputs_missing_atr")
    if not vetoes:
        reasons.append("v7_scores_balanced")
    indicators = {
        "close": close,
        "vwap": vwap,
        "vwapUpper": vwap_upper,
        "vwapLower": vwap_lower,
        "rsi": rsi,
        "relativeVolume": relative_volume,
        "ema9": ema9,
        "ema21": ema21,
        "supertrend": supertrend,
        "macdHist": macd_hist,
        "momentumPct": momentum_pct,
        "atr": atr,
        "bullScore": _number(_get(setup, "bullScore", "bull_score"), 0.0) or 0.0,
        "bearScore": _number(_get(setup, "bearScore", "bear_score"), 0.0) or 0.0,
        "turtle": copy.deepcopy(dict(turtle)) if isinstance(turtle, Mapping) else {},
    }
    return {
        "eligible": not vetoes,
        "vetoes": vetoes,
        "reasons": reasons,
        "indicators": indicators,
    }


def _expected_move(payload: Mapping[str, Any], setup: Mapping[str, Any], underlying: float) -> float:
    supplied = _number(_get(payload, "expectedMove", "expected_move"))
    if supplied is not None and supplied > 0:
        return supplied
    turtle = _get(setup, "turtle", default={}) or {}
    atr = _number(_get(turtle, "n", "atr"))
    if atr is not None and atr > 0:
        return atr
    return max(underlying * 0.0015, 0.50)


def _contracts(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = _get(payload, "contracts", "legs", default=[])
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _delta_allowed(contract: Mapping[str, Any], maximum: float = 0.30) -> bool:
    delta = _number(_get(contract, "delta"))
    return delta is None or abs(delta) <= maximum


def _candidate_rank(position: Mapping[str, Any], *, expected_move: float, underlying: float) -> tuple[float, float, float]:
    credit = float(position["entryCreditDollars"])
    risk = float(position["maxLoss"])
    short_put = float(position["entryLegs"]["short_put"]["strike"])
    short_call = float(position["entryLegs"]["short_call"]["strike"])
    outside_distance = min(underlying - short_put, short_call - underlying)
    return (credit / max(risk, 0.01), outside_distance / max(expected_move, 0.01), credit)


def _select_condor(
    contracts: list[dict[str, Any]],
    *,
    underlying: float,
    expected_move: float,
    setup: Mapping[str, Any],
    max_risk: float,
    entered_at: datetime,
    quote_now: datetime | None,
) -> dict[str, Any] | None:
    puts = [item for item in contracts if str(_get(item, "optionType", "option_type", "type", default="")).lower() == "put"]
    calls = [item for item in contracts if str(_get(item, "optionType", "option_type", "type", default="")).lower() == "call"]
    buffer = max(0.25, expected_move * 0.10)
    short_puts = [
        item for item in puts
        if _number(_get(item, "strike")) is not None
        and float(item["strike"]) <= underlying - expected_move - buffer
        and _delta_allowed(item)
    ]
    short_calls = [
        item for item in calls
        if _number(_get(item, "strike")) is not None
        and float(item["strike"]) >= underlying + expected_move + buffer
        and _delta_allowed(item)
    ]
    best: dict[str, Any] | None = None
    for short_put in short_puts:
        for short_call in short_calls:
            if not float(short_put["strike"]) < float(short_call["strike"]):
                continue
            long_puts = [item for item in puts if float(item.get("strike", 0)) < float(short_put["strike"])]
            long_calls = [item for item in calls if float(item.get("strike", 0)) > float(short_call["strike"])]
            for long_put in long_puts:
                for long_call in long_calls:
                    legs = {
                        "long_put": long_put,
                        "short_put": short_put,
                        "short_call": short_call,
                        "long_call": long_call,
                    }
                    position = build_position(
                        "iron_condor", legs, underlying=underlying, setup=setup,
                        entered_at=entered_at, max_risk=max_risk, quote_now=quote_now,
                    )
                    if position is None:
                        continue
                    if best is None or _candidate_rank(position, expected_move=expected_move, underlying=underlying) > _candidate_rank(best, expected_move=expected_move, underlying=underlying):
                        best = position
    return best


def _select_butterfly(
    contracts: list[dict[str, Any]],
    *,
    underlying: float,
    expected_move: float,
    setup: Mapping[str, Any],
    max_risk: float,
    entered_at: datetime,
    quote_now: datetime | None,
) -> dict[str, Any] | None:
    puts = {float(item["strike"]): item for item in contracts if str(_get(item, "optionType", "option_type", "type", default="")).lower() == "put" and _number(_get(item, "strike")) is not None}
    calls = {float(item["strike"]): item for item in contracts if str(_get(item, "optionType", "option_type", "type", default="")).lower() == "call" and _number(_get(item, "strike")) is not None}
    bodies = sorted(set(puts) & set(calls), key=lambda strike: abs(strike - underlying))
    best: dict[str, Any] | None = None
    best_rank: tuple[float, float] | None = None
    for body in bodies:
        if abs(body - underlying) > max(expected_move * 0.50, 1.0):
            continue
        widths = sorted({round(body - strike, 2) for strike in puts if strike < body} & {round(strike - body, 2) for strike in calls if strike > body})
        for width in widths:
            if width <= 0:
                continue
            long_put = puts.get(round(body - width, 2))
            long_call = calls.get(round(body + width, 2))
            if long_put is None or long_call is None:
                continue
            legs = {
                "long_put": long_put,
                "short_put": puts[body],
                "short_call": calls[body],
                "long_call": long_call,
            }
            position = build_position(
                "iron_butterfly", legs, underlying=underlying, setup=setup,
                entered_at=entered_at, max_risk=max_risk, quote_now=quote_now,
            )
            if position is None:
                continue
            rank = (-abs(body - underlying), float(position["entryCreditDollars"]) / max(float(position["maxLoss"]), 0.01))
            if best is None or best_rank is None or rank > best_rank:
                best = position
                best_rank = rank
    return best


def select_candidates(
    payload: Mapping[str, Any],
    setup: Mapping[str, Any],
    *,
    underlying: float,
    now: datetime | None = None,
    max_risk: float = MAX_RISK_PER_TRADE,
) -> list[dict[str, Any]]:
    """Select at most one condor and one butterfly from fresh quoted contracts."""
    if str(_get(payload, "symbol", default=SYMBOL)).upper() != SYMBOL:
        return []
    regime = range_regime(setup)
    if not regime["eligible"]:
        return []
    contracts = _contracts(payload)
    expected_move = _expected_move(payload, setup, underlying)
    entered_at = _now_et(now)
    quote_now = entered_at if now is not None else None
    candidates: list[dict[str, Any]] = []
    condor = _select_condor(
        contracts, underlying=underlying, expected_move=expected_move,
        setup=setup, max_risk=max_risk, entered_at=entered_at, quote_now=quote_now,
    )
    butterfly = _select_butterfly(
        contracts, underlying=underlying, expected_move=expected_move,
        setup=setup, max_risk=max_risk, entered_at=entered_at, quote_now=quote_now,
    )
    for candidate in (condor, butterfly):
        if candidate is None:
            continue
        candidate["expectedMove"] = expected_move
        candidate["rangeRegime"] = regime
        candidate["quoteBacked"] = True
        candidates.append(candidate)
    return candidates


def exit_reason(
    position: Mapping[str, Any],
    current_legs: Mapping[str, Mapping[str, Any]],
    *,
    underlying: float,
    now: datetime,
    v7_setup: Mapping[str, Any] | None,
) -> str | None:
    """Return the first fail-closed exit rule that is satisfied."""
    current = _now_et(now)
    if current.time() >= HARD_CLOSE_ET:
        return "hard_close"
    if not _finite(underlying):
        return None
    short_put = float(position["entryLegs"]["short_put"]["strike"])
    short_call = float(position["entryLegs"]["short_call"]["strike"])
    structure = str(position.get("structure", ""))
    if structure == "iron_butterfly":
        # For a butterfly, short_put == short_call (the body strike), so the
        # condor's short-strike breach check would fire for every underlying
        # price.  Use the break-even range instead: the position is structurally
        # threatened only when the underlying moves beyond break-even.
        break_lower = float(position.get("breakEvenLower", short_put))
        break_upper = float(position.get("breakEvenUpper", short_call))
        if underlying <= break_lower or underlying >= break_upper:
            return "underlying_breakeven_breach"
    else:
        if underlying <= short_put or underlying >= short_call:
            return "underlying_short_strike_breach"
    if v7_setup is not None:
        regime = range_regime(v7_setup)
        if not regime["eligible"]:
            return "v7_directional_reversal"
    try:
        metrics = position_metrics(position, current_legs)
    except ValueError:
        return None
    if metrics["pnl"] <= -metrics["stopLossDollars"]:
        return "stop_loss"
    if metrics["pnl"] >= metrics["profitTargetDollars"]:
        return "profit_target"
    if position["structure"] == "iron_butterfly":
        body = float(position["bodyStrike"])
        if abs(underlying - body) >= max(0.50, float(position["putWingWidth"]) * 0.50):
            return "butterfly_body_drift"
    return None


def promotion_summary(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Count only this strategy's exact entry/exit pairs for live promotion."""
    entries: dict[str, Mapping[str, Any]] = {}
    exits: dict[str, Mapping[str, Any]] = {}
    for event in events:
        if event.get("strategyId") != STRATEGY_ID:
            continue
        position_id = event.get("positionId")
        if not position_id:
            continue
        if event.get("event") == "SPREAD_PAPER_ENTRY":
            entries[str(position_id)] = event
        elif event.get("event") == "SPREAD_PAPER_EXIT":
            exits[str(position_id)] = event
    closed: list[float] = []
    for position_id, event in exits.items():
        if position_id not in entries:
            continue
        pnl = _number(event.get("pnl"))
        if pnl is None:
            continue
        if event.get("liveOrdersEnabled", False):
            continue
        if event.get("quoteBacked", True) is False:
            continue
        closed.append(pnl)
    pnl = sum(closed)
    wins = sum(1 for value in closed if value > 0)
    return {
        "strategyId": STRATEGY_ID,
        "strategyVersion": STRATEGY_VERSION,
        "closedTrades": len(closed),
        "wins": wins,
        "losses": sum(1 for value in closed if value <= 0),
        "pnl": round(pnl, 2),
        "remainingTrades": max(0, PROMOTION_MIN_TRADES - len(closed)),
        "gateMet": len(closed) >= PROMOTION_MIN_TRADES and pnl > 0,
        "liveOrdersEnabled": False,
    }


def entry_window_open(now: datetime) -> bool:
    current = _now_et(now)
    # The end is exclusive so a 13:00 PDT hard-close tick cannot create a
    # position that is immediately due for closure.
    return ENTRY_START_ET <= current.time() < ENTRY_END_ET


def hard_close_due(now: datetime) -> bool:
    return _now_et(now).time() >= HARD_CLOSE_ET


V7_INDICATORS_USED = (
    "ema9_ema21",
    "vwap_bands",
    "macd_histogram",
    "rsi",
    "momentum_pct",
    "relative_volume",
    "supertrend",
    "turtle_breakout",
    "premarket_breakout",
    "opening_continuation",
    "crash_detector",
    "pump_detector",
)


def entry_block_reason(now: datetime) -> str | None:
    current = _now_et(now)
    if current.weekday() >= 5 or v7.is_market_holiday(current.date()):
        return "market_closed"
    if not entry_window_open(current):
        return "outside_entry_window"
    calendar_reason = v7.calendar_block(current)
    if calendar_reason:
        return calendar_reason
    return None


def build_v7423_setup(now: datetime | None = None) -> dict[str, Any]:
    """Run the active V7.4.23 completed-bar signal path for SPY."""
    current = _now_et(now)
    try:
        bars = v7.fetch_bars(SYMBOL, days=5)
        day = v7.completed_day_bars(bars, now=current, symbol=SYMBOL)
        fresh, freshness_reason = v7.validate_completed_bar_freshness(
            day, now=current, symbol=SYMBOL,
        )
        if not fresh:
            return {"ok": False, "reason": freshness_reason, "setup": None}
        warmup, relative_volume, relative_volume_source, premarket = v7.session_signal_inputs(
            bars, day, now=current, symbol=SYMBOL,
        )
        setup = v7.detect_setup_for_recheck(
            day,
            warmup_bars=warmup,
            relative_volume=relative_volume,
            premarket_context=premarket,
            symbol=SYMBOL,
        )
        setup = dict(setup)
        setup["relativeVolumeSource"] = relative_volume_source
        setup["v7423IndicatorsUsed"] = list(V7_INDICATORS_USED)
        setup["v7423SignalBarEnd"] = (
            (day[-1]["dt"] + timedelta(minutes=5)).isoformat()
            if day and isinstance(day[-1].get("dt"), datetime)
            else None
        )
        regime = range_regime(setup)
        return {
            "ok": True,
            "reason": "ok",
            "setup": setup,
            "regime": regime,
            "completedBarFreshness": freshness_reason,
        }
    except Exception as exc:
        return {"ok": False, "reason": f"v7_setup_error:{type(exc).__name__}", "setup": None}


def _underlying_from_quote_payload(payload: Mapping[str, Any]) -> float:
    quote = _get(payload, "quote", default={})
    if not isinstance(quote, Mapping):
        quote = _get(payload, "underlyingQuote", default={})
    if isinstance(quote, Mapping) and isinstance(quote.get("quote"), Mapping):
        quote = quote["quote"]
    bid = _number(_get(quote, "bid_price", "bid")) if isinstance(quote, Mapping) else None
    ask = _number(_get(quote, "ask_price", "ask")) if isinstance(quote, Mapping) else None
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        return (bid + ask) / 2.0
    last = _number(_get(quote, "last_non_reg_trade_price", "last_trade_price", "last")) if isinstance(quote, Mapping) else None
    if last is not None and last > 0:
        return last
    underlying = _number(_get(payload, "underlying"))
    if underlying is not None and underlying > 0:
        return underlying
    raise ValueError("missing_executable_underlying")


def _prepare_session_state(state: dict[str, Any], current: datetime) -> None:
    session_date = current.date().isoformat()
    if state.get("sessionDate") == session_date:
        return
    state["sessionDate"] = session_date
    state["dailyTradeCount"] = 0
    state["dailyPnl"] = 0.0
    state["dailyGrossLoss"] = 0.0
    state["lastSkipReason"] = None


def _skip(state: dict[str, Any], reason: str, *, state_file: Path | None = None) -> dict[str, Any]:
    state["lastSkipReason"] = reason
    save_state(state, state_file)
    return {"action": "skip", "reason": reason, "state": state}


def scan_once(
    *,
    now: datetime | None = None,
    dte: int = DEFAULT_DTE,
    state: dict[str, Any] | None = None,
    state_file: Path | None = None,
    ledger_file: Path | None = None,
    bridge_fn=None,
) -> dict[str, Any]:
    """Scan once and create at most one isolated paper position."""
    current = _now_et(now)
    state = state if state is not None else load_state(state_file)
    _prepare_session_state(state, current)
    if state.get("activePosition") is not None:
        return _skip(state, "position_already_open", state_file=state_file)
    if int(state.get("dailyTradeCount", 0)) >= 1:
        return _skip(state, "daily_trade_limit", state_file=state_file)
    if float(state.get("dailyGrossLoss", 0.0)) >= MAX_DAILY_LOSS:
        return _skip(state, "daily_loss_limit", state_file=state_file)
    block = entry_block_reason(current)
    if block:
        return _skip(state, block, state_file=state_file)
    # Reject new entries when fewer than MIN_TIME_TO_CLOSE_MINUTES remain
    # before hard close, so a position is not instantly hard-closed.
    close_dt = current.replace(hour=HARD_CLOSE_ET.hour, minute=HARD_CLOSE_ET.minute, second=0, microsecond=0)
    minutes_to_close = (close_dt - current).total_seconds() / 60.0
    if minutes_to_close < MIN_TIME_TO_CLOSE_MINUTES:
        return _skip(state, "insufficient_time_to_close", state_file=state_file)
    setup_result = build_v7423_setup(current)
    if not setup_result.get("ok"):
        return _skip(state, str(setup_result.get("reason", "v7_setup_unavailable")), state_file=state_file)
    regime = setup_result.get("regime", {})
    if not regime.get("eligible"):
        return _skip(state, "v7_range_gate:" + ",".join(regime.get("vetoes", [])), state_file=state_file)
    bridge_fn = bridge_fn or v7.bridge
    try:
        chain = bridge_fn(
            "--spread-candidates", SYMBOL,
            "--dte", str(int(dte)),
            "--max-strike-distance", str(BRIDGE_MAX_STRIKE_DISTANCE),
        )
        underlying = _underlying_from_quote_payload(chain)
        candidates = select_candidates(
            chain, setup_result["setup"], underlying=underlying, now=current,
        )
    except Exception as exc:
        return _skip(state, f"spread_quote_error:{type(exc).__name__}", state_file=state_file)
    if not candidates:
        return _skip(state, "no_risk_capped_spread_candidate", state_file=state_file)
    order = {"iron_condor": 0, "iron_butterfly": 1}
    position = sorted(candidates, key=lambda item: order.get(item["structure"], 99))[0]
    position["dteTarget"] = int(dte)
    position["quoteSnapshotTimestamp"] = chain.get("timestamp")
    position["underlyingQuote"] = chain.get("underlyingQuote")
    state["activePosition"] = position
    state["dailyTradeCount"] = int(state.get("dailyTradeCount", 0)) + 1
    state["lastScanAt"] = current.isoformat()
    state["lastSkipReason"] = None
    append_event({
        "event": "SPREAD_PAPER_ENTRY",
        "positionId": position["positionId"],
        "structure": position["structure"],
        "symbol": SYMBOL,
        "entryCredit": position["entryCredit"],
        "entryCreditDollars": position["entryCreditDollars"],
        "maxLoss": position["maxLoss"],
        "maxProfit": position["maxProfit"],
        "legs": position["entryLegs"],
        "quoteSnapshotTimestamp": position["quoteSnapshotTimestamp"],
        "quoteBacked": True,
        "v7Setup": position["v7Setup"],
        "rangeRegime": position["rangeRegime"],
    }, ledger_file)
    save_state(state, state_file)
    return {"action": "paper_entry", "position": position, "state": state}


def monitor_once(
    *,
    now: datetime | None = None,
    state: dict[str, Any] | None = None,
    state_file: Path | None = None,
    ledger_file: Path | None = None,
    bridge_fn=None,
) -> dict[str, Any]:
    """Refresh one four-leg quote package and apply the exit policy."""
    current = _now_et(now)
    state = state if state is not None else load_state(state_file)
    _prepare_session_state(state, current)
    position = state.get("activePosition")
    if not isinstance(position, Mapping):
        return _skip(state, "no_open_position", state_file=state_file)
    bridge_fn = bridge_fn or v7.bridge
    try:
        quote_payload = bridge_fn(
            "--spread-position",
            json.dumps(position["entryLegs"], separators=(",", ":")),
        )
        current_legs = quote_payload.get("legs")
        underlying_payload = bridge_fn("--equity", SYMBOL)
        underlying = _underlying_from_quote_payload(underlying_payload)
    except Exception as exc:
        state["lastError"] = f"spread_monitor_quote_error:{type(exc).__name__}"
        save_state(state, state_file)
        return {"action": "quote_failure", "reason": state["lastError"], "state": state}
    v7_setup = None
    last_check = _as_datetime(state.get("lastSignalCheckAt"))
    if last_check is None or (current.astimezone(UTC) - last_check.astimezone(UTC)).total_seconds() >= SIGNAL_RECHECK_SECONDS:
        setup_result = build_v7423_setup(current)
        v7_setup = setup_result.get("setup") if setup_result.get("ok") else None
        state["lastSignalCheckAt"] = current.isoformat()
    reason = exit_reason(
        position, current_legs, underlying=underlying, now=current, v7_setup=v7_setup,
    )
    if reason is None:
        state["lastQuoteAt"] = quote_payload.get("timestamp")
        save_state(state, state_file)
        return {"action": "hold", "position": position, "state": state}
    metrics = position_metrics(position, current_legs)
    pnl = round(metrics["pnl"], 2)
    state["activePosition"] = None
    state["dailyPnl"] = round(float(state.get("dailyPnl", 0.0)) + pnl, 2)
    state["dailyGrossLoss"] = round(float(state.get("dailyGrossLoss", 0.0)) + max(-pnl, 0.0), 2)
    state["lastExitReason"] = reason
    state["lastQuoteAt"] = quote_payload.get("timestamp")
    append_event({
        "event": "SPREAD_PAPER_EXIT",
        "positionId": position["positionId"],
        "structure": position["structure"],
        "symbol": SYMBOL,
        "exitReason": reason,
        "exitDebit": metrics["exitDebit"],
        "pnl": pnl,
        "exitLegs": current_legs,
        "quoteBacked": True,
        "liveOrdersEnabled": False,
    }, ledger_file)
    save_state(state, state_file)
    return {"action": "paper_exit", "reason": reason, "pnl": pnl, "state": state}


def run_once(
    *,
    now: datetime | None = None,
    dte: int = DEFAULT_DTE,
    state_file: Path | None = None,
    ledger_file: Path | None = None,
    bridge_fn=None,
) -> dict[str, Any]:
    state = load_state(state_file)
    if state.get("activePosition") is not None:
        return monitor_once(
            now=now, state=state, state_file=state_file,
            ledger_file=ledger_file, bridge_fn=bridge_fn,
        )
    return scan_once(
        now=now, dte=dte, state=state, state_file=state_file,
        ledger_file=ledger_file, bridge_fn=bridge_fn,
    )


def default_state() -> dict[str, Any]:
    return {
        "strategyId": STRATEGY_ID,
        "strategyVersion": STRATEGY_VERSION,
        "mode": "paper",
        "liveOrdersEnabled": False,
        "symbol": SYMBOL,
        "activePosition": None,
        "dailyTradeCount": 0,
        "dailyPnl": 0.0,
        "dailyGrossLoss": 0.0,
        "sessionDate": None,
        "lastScanAt": None,
        "lastSkipReason": None,
    }


def load_state(path: Path | None = None) -> dict[str, Any]:
    target = path or state_path()
    try:
        payload = json.loads(target.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default_state()
    state = default_state()
    state.update(payload if isinstance(payload, dict) else {})
    state["liveOrdersEnabled"] = False
    state["strategyId"] = STRATEGY_ID
    state["strategyVersion"] = STRATEGY_VERSION
    return state


def save_state(state: Mapping[str, Any], path: Path | None = None) -> None:
    target = path or state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(state), indent=2, sort_keys=True) + "\n")
    temporary.replace(target)


def append_event(event: Mapping[str, Any], path: Path | None = None) -> None:
    target = path or ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    record = dict(event)
    record.setdefault("strategyId", STRATEGY_ID)
    record.setdefault("strategyVersion", STRATEGY_VERSION)
    record.setdefault("liveOrdersEnabled", False)
    record.setdefault("eventTime", datetime.now(tz=UTC).isoformat())
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def read_events(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or ledger_path()
    if not target.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in target.read_text().splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _fixture_command(path: str) -> int:
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, dict):
        raise SystemExit("fixture must be a JSON object")
    if payload.get("command") == "promotion":
        print(json.dumps(promotion_summary(payload.get("events", [])), indent=2, sort_keys=True))
        return 0
    setup = payload.get("setup", {})
    print(json.dumps(range_regime(setup), indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", help="evaluate a deterministic JSON fixture")
    parser.add_argument("--promotion", action="store_true", help="print this strategy's promotion summary")
    parser.add_argument("--scan", action="store_true", help="run one paper entry scan")
    parser.add_argument("--monitor", action="store_true", help="run one paper position monitor")
    parser.add_argument("--once", action="store_true", help="monitor an open position or scan for a new one")
    parser.add_argument("--dte", type=int, default=DEFAULT_DTE, choices=range(0, 46))
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--ledger-file", type=Path)
    args = parser.parse_args(argv)
    if args.fixture:
        return _fixture_command(args.fixture)
    if args.promotion:
        print(json.dumps(promotion_summary(read_events(args.ledger_file)), indent=2, sort_keys=True))
        return 0
    modes = sum(bool(value) for value in (args.scan, args.monitor, args.once))
    if modes != 1:
        parser.error("choose exactly one of --scan, --monitor, or --once")
    if args.scan:
        result = scan_once(dte=args.dte, state_file=args.state_file, ledger_file=args.ledger_file)
    elif args.monitor:
        result = monitor_once(state_file=args.state_file, ledger_file=args.ledger_file)
    else:
        result = run_once(dte=args.dte, state_file=args.state_file, ledger_file=args.ledger_file)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
