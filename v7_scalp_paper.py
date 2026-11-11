#!/usr/bin/env python3
"""V7.5 — Multi-symbol 1DTE naked-option scalp, PAPER ONLY.

This program never calls a broker order tool.  It uses free Yahoo 5-minute bars
for deterministic signals, then invokes v7_rh_quote_bridge.py only for a real
Robinhood option quote when a signal appears or an open paper position needs an
exit quote. Paper entries use Robinhood ask; paper exits use Robinhood bid.

A target of 3 trades/day is a ceiling, not a quota: no setup means no trade.
Supports SPY and QQQ with per-symbol parameter tuning.

Each symbol maintains its own state file, trade log, and lock file:
  state:  v7_scalp_state_{SYMBOL}.json
  log:    v7_scalp_trades_{SYMBOL}.jsonl
  lock:   v7_scalp_state_{SYMBOL}.lock
  Shared: kill switch, macro calendar, scan attempts log.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import time
import sys
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, pstdev
from typing import Any
from zoneinfo import ZoneInfo

import v7_release
from v7_rh_quote_bridge import is_market_holiday

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")
ROOT = Path("/path/to/project/.hermes")
def state_path(symbol: str) -> Path:
    """Return the per-symbol state file path."""
    return ROOT / "state" / f"v7_scalp_state_{symbol}.json"

def state_lock_path(symbol: str) -> Path:
    """Return the per-symbol lock file path."""
    return ROOT / "state" / f"v7_scalp_state_{symbol}.lock"

def log_path(symbol: str) -> Path:
    """Return the per-symbol trade log path."""
    return ROOT / "state" / f"v7_scalp_trades_{symbol}.jsonl"

# Legacy paths for migration
_STATE_PATH_LEGACY = ROOT / "state" / "v7_scalp_state.json"
_LOG_PATH_LEGACY = ROOT / "state" / "v7_scalp_trades.jsonl"
BRIDGE = ROOT / "scripts" / "v7_rh_quote_bridge.py"
KILL_SWITCH = ROOT / "state" / "v7_kill_switch.json"


def kill_switch_active() -> bool:
    """Return whether the paper engine must block new entries.

    Paper operation may continue with no control file or an explicit inactive
    object. Any present but malformed or non-inactive control state blocks.
    Live readiness remains stricter and treats a missing file as active.
    """
    if not KILL_SWITCH.exists():
        return False
    try:
        payload = json.loads(KILL_SWITCH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return not isinstance(payload, dict) or payload.get("active") is not False
CALENDAR_PATH = ROOT / "state" / "v7_macro_calendar.json"
def premarket_path(symbol: str) -> Path:
    """Return the per-symbol premarket price-target snapshot path."""
    return ROOT / "state" / f"v7_scalp_premarket_{symbol}.json"

UA = {"User-Agent": "Mozilla/5.0 (V7.4 paper scalp)"}

# Yahoo's chart service occasionally stalls during TLS setup on one edge. Keep
# the market-data path read-only and fail over to a separate chart hostname
# before surfacing a data error to the strategy. The bounded budget prevents a
# broken data feed from blocking the daemon indefinitely.
YAHOO_CHART_HOSTS = (
    "query1.finance.yahoo.com",
    "query2.finance.yahoo.com",
)
YAHOO_CHART_TIMEOUT_SECONDS = 8.0
YAHOO_CHART_MAX_ATTEMPTS = 3
YAHOO_CHART_RETRY_BACKOFF_SECONDS = 0.25

# Explicitly paper-only; there is intentionally no live-order flag.
SYMBOL = "SPY"  # Backward compat default; active symbols come from SYMBOLS
MAX_TRADES = 5  # V7.5.2: raised from 3 to allow 4+ trades/day (user goal)
MAX_QUOTE_SPREAD_PCT = 5.0
MAX_SPREAD_VS_TARGET_PCT = 30.0  # Reject if spread > 30% of target distance in dollars
MAX_ENTRY_QUOTE_AGE_SECONDS = 15  # V7.5.4: near-live entry pricing (was 60)
MAX_EXIT_QUOTE_AGE_SECONDS = 15  # V7.5.4: near-live exit pricing (was 30)
# A stale provider timestamp gets one bounded fresh-worker retry. The retry is
# cooldown-limited so a frozen venue timestamp cannot restart the MCP worker on
# every monitor tick; the quote remains fail-closed if it is still stale.
STALE_EXIT_QUOTE_REFRESH_COOLDOWN_SECONDS = 30.0
MAX_LIVE_EQUITY_QUOTE_AGE_SECONDS = 5
# XSP candidate spot is a broker-native put-call-parity composite. The bridge
# serializes multiple option-book reads, so its oldest leg can be several seconds
# older than the candidate option quote even when both are from the same snapshot.
# Keep the direct live-trigger quote at 5s; allow only this composite backing spot
# the measured assembly window, while the candidate option quote remains subject
# to its own 60s freshness gate.
MAX_INDEX_CANDIDATE_UNDERLYING_QUOTE_AGE_SECONDS = 15
BAR_INTERVAL_MINUTES = 5
MAX_COMPLETED_BAR_AGE_SECONDS = 300  # V7.5.4: freshest completed bar only (was 600)
MAX_INDEX_BAR_AGE_SECONDS = 1800  # CBOE overnight/extended bars are sparser
MAX_CALENDAR_AGE_DAYS = 7
ENTRY_RESERVATION_TTL_SECONDS = 300
OPTION_DTE_TARGET = 1
MAX_DAILY_LOSS = 40.0
TRAIL_START_PCT = 6.0   # V7.5: bank winners sooner (was 8.0)
TRAIL_GIVEBACK_PCT = 3.5  # V7.5: tighter giveback (was 5.0)
MAX_HOLD_MINUTES = 25
MIN_HOLD_MINUTES = 5
BREAKEVEN_HOLD_MINUTES = 12  # V7.5: give winners more room (was 8)
RSI_OVERBOUGHT = 72  # V7.4.12: kept at 72 — data showed RSI 80 was a winner, tightening rejects good trades
RSI_OVERSOLD = 28    # V7.4.12: kept at 28 — data showed RSI 8 was a loser, tightening doesn't help
MIN_DIRECTION_EDGE = 2
MIN_ENTRY_SCORE = 7  # Normal entry floor; acceleration is not allowed to inflate this score
MIN_RELATIVE_VOLUME = 0.75  # Breakout capture floor; below this remains weak confirmation
BEARISH_HIGH_VOLUME_REL_VOL = 2.0
BULLISH_HIGH_VOLUME_REL_VOL = 2.0   # New: mirror bearish high-volume substitute (was missing)
BULLISH_ANTI_CHASE_SIGMA = 2.0
BEARISH_ANTI_CHASE_SIGMA = 3.0
MIN_SCORE_AFTER_LOSS = 8  # After two losses, normal setups need one point more than the base floor
CONSECUTIVE_LOSS_COOLDOWN = 2  # Cooldown threshold: after 2 consecutive losses
# --- V7.5.3 re-entry risk policy (operator-approved 2026-10-12) ---
# After a losing exit, same-direction re-entries are blocked for this many
# minutes. Evidence: same-direction re-entries within 5 minutes of a loss went
# 1W/2L for -$16 in the exact-release cohort, including the 2026-10-12 QQQ
# chain that re-bought the same put within ~65 seconds of each exit.
POST_LOSS_SAME_DIRECTION_DELAY_MINUTES = 5.0
MIN_VWAP_DEVIATION_PCT = 0.05  # Equity VWAP no-conviction floor (fixed %; not ATR-inflated)
MIN_VWAP_DEVIATION_PCT_INDEX = 0.01  # Legacy index-option floor (retained for compatibility)
LATE_ENTRY_SCORE_BUMP = 1  # After 14:30 ET, require score + this bump (e.g. 7→8)
# --- V7.5 option liquidity gates ---
MIN_OPTION_OPEN_INTEREST = 10  # Low floor — volume is primary; OI lags on fresh 1DTE
MIN_OPTION_VOLUME = 50  # Require active trading in the contract today
# --- V7.4.4 indicator normalization constants ---
# 1. Time-of-day normalized RVOL: compare current volume to the typical volume
#    at the same time of day, not the session average (which is biased by bar count).
#    Build a per-bar-of-day expected volume from historical sessions.
RVOL_TIME_NORMALIZATION_LOOKBACK_DAYS = 10  # Use last 10 sessions for baseline
# 2. ATR-normalized thresholds for momentum / fresh-trigger only.
#    VWAP no-conviction is FIXED at MIN_VWAP_DEVIATION_PCT (restored 2026-08-24).
#    The old 3.0×ATR% VWAP gate rejected 71% of setups that already cleared 0.05%.
MOMENTUM_ATR_FRACTION = 2.5   # 2.5 × ATR% replaces fixed 0.04% (slightly tighter)
VWAP_DEVIATION_ATR_FRACTION = 0.0  # Disabled — fixed % floor only (was 3.0, over-tight)
FRESH_TRIGGER_ATR_FRACTION = 0.05  # Already in place; kept for documentation
# 3. Evidence categories replace flat 8-vote score:
#    Category A (Trend): EMA9/21 + Supertrend → 0-2 points
#    Category B (Mean-reversion): VWAP position + VWAP deviation magnitude → 0-2 points
#    Category C (Momentum): MACD histogram sign + momentum magnitude (ATR-normalized) → 0-2 points
#    Category D (Flow): time-normalized RVOL + RSI regime → 0-2 points
#    Min entry: total ≥5/8 with ≥1 in each category (no category can be zero)
EVIDENCE_MIN_TOTAL = 5  # V7.5: kept at 5 — 2 new components help qualify, don't tighten the gate
EVIDENCE_MIN_PER_CATEGORY = 1
# 4. Dynamic profit target: replace fixed 15% with underlying-driven exit.
#    Target is ATR-based: when underlying moves N * ATR in direction, exit.
#    Strong signals get wider targets (let winners run); weak signals get tighter.
DYNAMIC_TARGET_ATR_MULT_MIN = 1.0   # Weak entry: 1.0 × ATR underlying move → exit
DYNAMIC_TARGET_ATR_MULT_MAX = 2.5   # Strong entry: 2.5 × ATR underlying move → exit
# Cap the option-side target at 30% to avoid holding through IV crush.
DYNAMIC_TARGET_PCT_CAP = 30.0
FAST_PROFIT_TARGET_PCT = 8.0  # Nonstructural setups bank smaller wins quickly.
# 5. Delta-aware contract selection: prefer delta 0.30-0.50, reject delta < 0.15.
PREFERRED_DELTA_MIN = 0.15
PREFERRED_DELTA_TARGET = 0.35
# Portfolio-wide daily risk cap across all symbols (was per-symbol only).
PORTFOLIO_DAILY_LOSS_CAP = 80.0  # $80 total across SPY+QQQ
MIN_CONFIRMATION_TICKS = 2  # Soft exit triggers (stop, trail, breakeven) require this many consecutive ticks
# Early thesis-reversal checks use the existing completed 5-minute bar feed.
# Each independent confirmation needs two consecutive completed bars.
SIGNAL_REVERSAL_CONFIRM_BARS = 2
SIGNAL_REVERSAL_VWAP_BUFFER_PCT = 0.12
SIGNAL_REVERSAL_MOMENTUM_LOOKBACK_BARS = 4
# Hard triggers (profit_target, time_stop, hard_close, vwap_invalidation, turtle_opposite_breakout)
# fire immediately — they are structural, not noise-sensitive.
SOFT_EXIT_REASONS = {
    "stop_loss", "breakeven_stop", "trailing_stop", "breakeven_exit",
}
ORB_ENABLED = False  # Legacy standalone ORB detector remains disabled after A/B harm.
# The stricter path below uses ORB only as one structural component of a full
# EMA/MACD/volume/momentum/VWAP trend confluence. It is not the old standalone
# ORB detector and cannot qualify without the other independent checks.
TREND_ORB_CONFLUENCE_ENABLED = True
TREND_CONTINUATION_MIN_SCORE = 6
TREND_ORB_MIN_RANGE_PCT = 0.15
TREND_ORB_BREAKOUT_PCT = 0.10
SQUEEZE_BREAKOUT_ENABLED = True  # BB squeeze remains scored structural evidence; acceleration is telemetry only
PREMARKET_WATCH_START = 400  # ET; Yahoo extended-hours bars begin at 01:00 PT
MARKET_OPEN = 930
ENTRY_START = 935  # ET; 06:35 PT, after the first completed regular-session 5m bar
ENTRY_END = 1530   # ET; 12:30 PT; latest safe entry leaves 25 min before hard close
HARD_CLOSE = 1555  # ET; gives 25-min hold for 1530 entries
LUNCH_START, LUNCH_END = 1200, 1230  # V7.4.5: 30-min lunch chop (was 1130-1330 = 2hrs!)
# Robinhood's XSP option quote feed currently provides executable venue
# timestamps during the regular index-option session only.  Do not classify the
# exchange's overnight/GTH window as active when this broker feed is stale.
XSP_BROKER_SESSION_START = 930
XSP_BROKER_SESSION_END = 1615
OPENING_WARMUP_BARS = 40
OPENING_MAX_BREAKOUT_EXTENSION_ATR = 0.50


NON_SCORING_SIGNAL_CHECKS = frozenset({"momentum_acceleration"})


def _signal_score(checks: dict[str, bool]) -> int:
    """Return the entry score while keeping acceleration as telemetry only."""
    return sum(
        1
        for name, passed in checks.items()
        if name not in NON_SCORING_SIGNAL_CHECKS and passed
    )


def evidence_categories(checks: dict[str, bool], direction: str) -> dict[str, int]:
    """Group the scored checks into 4 independent evidence categories.

    Category A (Trend): ema_trend + supertrend → 0-2
    Category B (Mean-reversion): vwap + turtle_breakout + squeeze_breakout → 0-3
    Category C (Momentum): macd + momentum → 0-2. Acceleration is telemetry only.
    Category D (Flow): relative_volume + rsi → 0-2

    Entry requires total >= EVIDENCE_MIN_TOTAL AND >= EVIDENCE_MIN_PER_CATEGORY
    in every category. No category can be zero — prevents one-dimensional signals.
    """
    cat_a = int(checks.get("ema_trend", False)) + int(checks.get("supertrend", False))
    cat_b = int(checks.get("vwap", False)) + int(checks.get("turtle_breakout", False)) + int(checks.get("squeeze_breakout", False))
    cat_c = int(checks.get("macd", False)) + int(checks.get("momentum", False))
    cat_d = int(checks.get("relative_volume", False)) + int(checks.get("rsi", False))
    return {"trend": cat_a, "mean_reversion": cat_b, "momentum": cat_c, "flow": cat_d}


def evidence_qualified(evidence: dict[str, int]) -> tuple[bool, list[str]]:
    """Check if evidence categories meet the entry threshold."""
    total = sum(evidence.values())
    reasons = []
    if total < EVIDENCE_MIN_TOTAL:
        reasons.append(f"evidence_total_{total}_below_{EVIDENCE_MIN_TOTAL}")
    for cat, score in evidence.items():
        if score < EVIDENCE_MIN_PER_CATEGORY:
            reasons.append(f"evidence_{cat}_zero")
    return len(reasons) == 0, reasons


def _entry_signal_strength(pos: dict[str, Any]) -> float:
    """Return a 0–1 signal conviction score from the stored entry signal.

    V7.4.8: Uses independentScore (honest checks only, no forced turtle or
    volume-as-momentum substitution). Volume is already counted once in the
    relative_volume check, so it is NOT given a separate bonus here.

    Uses two factors:
      1. Gate count — how many honest directional checks passed (8/8 = full conviction).
      2. Score margin — score 8 has more margin than a bare-minimum 5.

    This drives dynamic exit thresholds so that strong entries get more room
    and weak entries get cut quickly — consistent with the entry thesis.
    """
    signal = pos.get("signal") or {}
    direction = signal.get("direction") or pos.get("direction", "bullish")
    checks = signal.get("bullChecks" if direction == "bullish" else "bearChecks", {})
    scored_checks = {
        name: value
        for name, value in checks.items()
        if name not in NON_SCORING_SIGNAL_CHECKS
    }
    check_count = sum(1 for value in scored_checks.values() if value)
    total_checks = max(len(scored_checks), 8)
    check_ratio = check_count / total_checks

    # V7.4.8: Use independentScore if available, fall back to score (now honest).
    # Acceleration is excluded from the honest score and remains diagnostic only.
    ind = signal.get("independentScore", signal.get("score", 5)) or 5
    # 5→0.25, 6→0.50, 7→0.75, 8→1.0
    score_factor = max(0.0, min(1.0, (ind - 4) / 4))

    # Weight: 70% checks, 30% score margin (no volume double-count)
    return 0.70 * check_ratio + 0.30 * score_factor


def _actual_turtle_breakout(signal: dict[str, Any]) -> bool | None:
    """Return the stored structural breakout without treating a substitute as real."""
    if not isinstance(signal, dict):
        return None
    direction = signal.get("direction") or signal.get("rawDirection")
    turtle = signal.get("turtle")
    if direction not in {"bullish", "bearish"} or not isinstance(turtle, dict):
        return None
    key = "longBreakout" if direction == "bullish" else "shortBreakout"
    value = turtle.get(key)
    return value if isinstance(value, bool) else None


def _is_nonstructural_signal(pos: dict[str, Any]) -> bool:
    """Identify any directed setup that qualified without structural evidence."""
    signal = pos.get("signal") or {}
    if signal.get("direction") not in {"bullish", "bearish"}:
        return False
    return not _structural_breakout_qualified(signal)


def entry_directional_drift_reason(
    setup: dict[str, Any], underlying: float,
) -> str | None:
    """Reject a nonstructural setup if the executable quote already reversed it.

    This is an execution-timing check, not an entry-score change. A real Turtle
    breakout keeps the existing path; detector setups are checked too when their
    structural breakout is still absent because they can otherwise chase a move.
    """
    direction = setup.get("direction")
    if direction not in {"bullish", "bearish"}:
        return None
    if _structural_breakout_qualified(setup) or _actual_turtle_breakout(setup) is not False:
        return None
    signal_close = setup.get("close")
    if (
        not isinstance(signal_close, (int, float))
        or isinstance(signal_close, bool)
        or not math.isfinite(float(signal_close))
        or float(signal_close) <= 0
        or not isinstance(underlying, (int, float))
        or isinstance(underlying, bool)
        or not math.isfinite(float(underlying))
        or float(underlying) <= 0
    ):
        return None
    raw_min_move = setup.get("minTriggerMove")
    min_move = (
        float(raw_min_move)
        if isinstance(raw_min_move, (int, float))
        and not isinstance(raw_min_move, bool)
        and math.isfinite(float(raw_min_move))
        and float(raw_min_move) >= 0
        else 0.02
    )
    tolerance = max(0.01, min_move * 0.50)
    if direction == "bullish" and float(underlying) < float(signal_close) - tolerance:
        return "entry_underlying_against_signal"
    if direction == "bearish" and float(underlying) > float(signal_close) + tolerance:
        return "entry_underlying_against_signal"
    return None


def _finite_float(value: Any) -> float | None:
    """Return a finite float or None without coercing malformed values."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _bar_timestamp(value: Any) -> datetime | None:
    """Return an aware bar timestamp or None."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value


def _bar_indicator(bar: dict[str, Any], *keys: str) -> float | None:
    """Read the first valid indicator alias from a bar fixture or payload."""
    for key in keys:
        value = _finite_float(bar.get(key))
        if value is not None:
            return value
    return None


def _future_reversal_snapshots(
    bars: list[dict[str, Any]],
    *,
    signal_end: datetime,
    indicator_bars: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build completed-bar indicator snapshots after the entry signal.

    Production bars contain OHLCV only, so MACD and EMA values are computed
    from the warmup-plus-session series supplied by the daemon. Explicit
    indicator fields remain supported for deterministic regression fixtures.
    """
    targets: list[tuple[datetime, float, dict[str, Any]]] = []
    for item in bars or []:
        if not isinstance(item, dict):
            continue
        when = _bar_timestamp(item.get("dt"))
        close = _finite_float(item.get("close"))
        if when is None or close is None:
            continue
        if when.astimezone(ET) >= signal_end.astimezone(ET):
            targets.append((when, close, item))
    targets.sort(key=lambda item: item[0])
    if not targets:
        return []

    source = list(indicator_bars or bars or [])
    source_index_by_time: dict[datetime, int] = {}
    for index, item in enumerate(source):
        if not isinstance(item, dict):
            continue
        when = _bar_timestamp(item.get("dt"))
        if when is not None:
            source_index_by_time[when] = index

    snapshots: list[dict[str, Any]] = []
    for when, close, item in targets:
        source_index = source_index_by_time.get(when)
        prefix = source[: source_index + 1] if source_index is not None else source
        prefix_closes = [
            value for candidate in prefix
            if isinstance(candidate, dict)
            for value in [_finite_float(candidate.get("close"))]
            if value is not None
        ]

        hist = _bar_indicator(item, "macdHist", "macd_hist")
        if hist is None and len(prefix_closes) >= 35:
            hist = macd_hist(prefix_closes)
        ema9_value = _bar_indicator(item, "ema9")
        if ema9_value is None and len(prefix_closes) >= 21:
            ema9_value = ema(prefix_closes, 9)
        ema21_value = _bar_indicator(item, "ema21")
        if ema21_value is None and len(prefix_closes) >= 21:
            ema21_value = ema(prefix_closes, 21)

        session_prefix_closes: list[float] = []
        for candidate in bars or []:
            if not isinstance(candidate, dict):
                continue
            candidate_time = _bar_timestamp(candidate.get("dt"))
            candidate_close = _finite_float(candidate.get("close"))
            if candidate_time is not None and candidate_close is not None and candidate_time <= when:
                session_prefix_closes.append(candidate_close)
        momentum = _bar_indicator(item, "momentumPct", "momentum_pct")
        lookback = SIGNAL_REVERSAL_MOMENTUM_LOOKBACK_BARS
        if momentum is None and len(session_prefix_closes) >= lookback:
            reference = session_prefix_closes[-lookback]
            if reference > 0:
                momentum = (session_prefix_closes[-1] / reference - 1.0) * 100.0

        snapshots.append({
            "dt": when,
            "close": close,
            "macdHist": hist,
            "ema9": ema9_value,
            "ema21": ema21_value,
            "momentumPct": momentum,
        })
    return snapshots


def _has_consecutive_conditions(
    values: list[bool | None], required: int,
) -> bool:
    """Return True only after the required consecutive valid conditions."""
    streak = 0
    for value in values:
        if value is True:
            streak += 1
            if streak >= required:
                return True
        else:
            streak = 0
    return False


def _has_consecutive_directional_moves(
    entry_value: float | None,
    future_values: list[float | None],
    *,
    direction: str,
    required: int,
) -> bool:
    """Detect two consecutive moves toward zero against the position thesis."""
    if entry_value is None:
        return False
    if direction == "bearish" and entry_value >= 0:
        return False
    if direction == "bullish" and entry_value <= 0:
        return False
    previous: float | None = entry_value
    streak = 0
    for current in future_values:
        if current is None or previous is None:
            previous = current
            streak = 0
            continue
        moved_toward_zero = (
            current > previous if direction == "bearish" else current < previous
        )
        streak = streak + 1 if moved_toward_zero else 0
        if streak >= required:
            return True
        previous = current
    return False


def signal_reversal_reason(
    position: dict[str, Any],
    bars: list[dict[str, Any]],
    *,
    indicator_bars: list[dict[str, Any]] | None = None,
) -> str | None:
    """Detect early thesis failure from the existing completed 5-minute feed.

    MACD histogram deceleration, momentum deceleration, VWAP invalidation, and
    EMA reversal are independent OR paths. Every new indicator path is debounced
    with two consecutive completed bars to avoid single-bar whipsaws.
    """
    signal = position.get("signal") or {}
    direction = signal.get("direction") or position.get("direction")
    raw_end = signal.get("signalBarEnd")
    if direction not in {"bullish", "bearish"} or not raw_end:
        return None
    try:
        signal_end = datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
        if signal_end.tzinfo is None:
            signal_end = signal_end.replace(tzinfo=ET)
    except (TypeError, ValueError):
        return None

    snapshots = _future_reversal_snapshots(
        bars,
        signal_end=signal_end,
        indicator_bars=indicator_bars,
    )
    if not snapshots:
        return None
    required = SIGNAL_REVERSAL_CONFIRM_BARS

    entry_hist = _bar_indicator(signal, "macdHist", "macd_hist")
    if _has_consecutive_directional_moves(
        entry_hist,
        [snapshot.get("macdHist") for snapshot in snapshots],
        direction=direction,
        required=required,
    ):
        return "signal_reversal_macd_deceleration"

    reference_vwap = _finite_float(position.get("entryVwap"))
    if reference_vwap is None or reference_vwap <= 0:
        reference_vwap = _finite_float(signal.get("vwap"))
    if reference_vwap is not None and reference_vwap > 0:
        buffer = reference_vwap * SIGNAL_REVERSAL_VWAP_BUFFER_PCT / 100.0
        vwap_conditions: list[bool | None] = []
        for snapshot in snapshots:
            close = _finite_float(snapshot.get("close"))
            if close is None:
                vwap_conditions.append(None)
            elif direction == "bearish":
                vwap_conditions.append(close > reference_vwap + buffer)
            else:
                vwap_conditions.append(close < reference_vwap - buffer)
        if _has_consecutive_conditions(vwap_conditions, required):
            return "signal_reversal_vwap"

    entry_ema9 = _bar_indicator(signal, "ema9")
    entry_ema21 = _bar_indicator(signal, "ema21")
    entry_trend_valid = (
        entry_ema9 is not None
        and entry_ema21 is not None
        and (
            entry_ema9 <= entry_ema21 if direction == "bearish"
            else entry_ema9 >= entry_ema21
        )
    )
    if entry_trend_valid:
        ema_conditions: list[bool | None] = []
        for snapshot in snapshots:
            current_ema9 = _finite_float(snapshot.get("ema9"))
            current_ema21 = _finite_float(snapshot.get("ema21"))
            if current_ema9 is None or current_ema21 is None:
                ema_conditions.append(None)
            else:
                ema_conditions.append(
                    current_ema9 > current_ema21 if direction == "bearish"
                    else current_ema9 < current_ema21
                )
        if _has_consecutive_conditions(ema_conditions, required):
            return "signal_reversal_ema_cross"

    entry_momentum = _bar_indicator(signal, "momentumPct", "momentum_pct")
    if _has_consecutive_directional_moves(
        entry_momentum,
        [snapshot.get("momentumPct") for snapshot in snapshots],
        direction=direction,
        required=required,
    ):
        return "signal_reversal_momentum_deceleration"

    signal_close = _finite_float(signal.get("close"))
    if signal_close is None or signal_close <= 0:
        return None
    raw_min_move = signal.get("minTriggerMove")
    min_move = (
        float(raw_min_move)
        if isinstance(raw_min_move, (int, float))
        and not isinstance(raw_min_move, bool)
        and math.isfinite(float(raw_min_move))
        and float(raw_min_move) >= 0
        else 0.02
    )
    tolerance = max(0.01, min_move * 0.50)
    close_conditions: list[bool | None] = []
    for snapshot in snapshots:
        latest_close = _finite_float(snapshot.get("close"))
        if latest_close is None:
            close_conditions.append(None)
        elif direction == "bullish":
            close_conditions.append(latest_close <= signal_close - tolerance)
        else:
            close_conditions.append(latest_close >= signal_close + tolerance)
    if _has_consecutive_conditions(close_conditions, required):
        return "signal_reversal"
    return None


def dynamic_breakeven_hold_minutes(pos: dict[str, Any]) -> float:
    """Strong entries get more time to develop; weak entries are cut quickly.

    Range: 6 min (weak score-5, low conviction) → 15 min (strong score-8, full conviction).
    The old fixed BREAKEVEN_HOLD_MINUTES=8 was too short for strong signals
    and too long for weak ones.
    """
    if _is_nonstructural_signal(pos):
        return float(min(BREAKEVEN_HOLD_MINUTES, 8.0))
    strength = _entry_signal_strength(pos)
    # Interpolate: strength 0.0 → 4 min, strength 1.0 → 12 min
    # V7.4.12: tighter breakeven (was 6+9=15 max). Cut weak entries faster.
    return 4.0 + 8.0 * strength


def dynamic_max_hold_minutes(pos: dict[str, Any]) -> float:
    """Strong entries can ride longer; weak entries expire faster.

    Range: 15 min (weak) → 35 min (strong).
    Old fixed MAX_HOLD_MINUTES=25.
    """
    strength = _entry_signal_strength(pos)
    return 15.0 + 20.0 * strength


def dynamic_trail_start_pct(pos: dict[str, Any]) -> float:
    """When to start trailing: weak entries trail early, strong entries get room.

    Range: 5% (weak) → 12% (strong).
    Old fixed TRAIL_START_PCT=8% was the same regardless of conviction.
    """
    strength = _entry_signal_strength(pos)
    return 5.0 + 7.0 * strength


def dynamic_trail_giveback_pct(pos: dict[str, Any]) -> float:
    """How much giveback from peak before trailing stop fires.

    Strong entries get tighter giveback (lock in profits).
    Weak entries get wider giveback (avoid noise exits).

    Range: 2% (strong) → 5% (weak).
    Old fixed TRAIL_GIVEBACK_PCT=5% gave weak entries too much room
    and strong entries too little.
    """
    strength = _entry_signal_strength(pos)
    return 5.0 - 3.0 * strength  # strong → 2%, weak → 5%
ENGINE_VERSION = v7_release.ENGINE_VERSION
STRATEGY_ID = v7_release.STRATEGY_ID
EVIDENCE_GATE = v7_release.EVIDENCE_GATE
RELEASE_MANIFEST_SHA256 = v7_release.MANIFEST_SHA256
_LAST_LIVE_EQUITY_VENUE_TS: dict[str, str] = {}  # Symbol → last venue timestamp for stale guard
_LAST_OPTION_VENUE_TS: dict[str, str] = {}  # OCC symbol → last venue timestamp for stale exit guard
_LAST_STALE_QUOTE_REFRESH_AT: dict[str, float] = {}  # OCC symbol → monotonic refresh time
_LAST_CRASH_REJECT_LOG_TS: dict[str, float] = {}  # Symbol → last crash rejection log time (monotonic)
_LAST_PUMP_REJECT_LOG_TS: dict[str, float] = {}   # Symbol → last pump rejection log time (monotonic)
CRASH_LOG_COOLDOWN_SECONDS = 60  # Suppress duplicate crash/pump rejection logs within this window


@dataclass(frozen=True)
class SymbolConfig:
    """Per-symbol trading parameters."""
    symbol: str
    min_debit: float
    max_debit: float
    max_otm_pct: float
    profit_target_pct: float
    bearish_profit_target_pct: float
    stop_loss_pct: float
    bearish_stop_loss_pct: float
    # ``None`` means no count cap. It does not bypass loss, quote, or position gates.
    trade_limit: int | None = MAX_TRADES
    min_entry_score: int = MIN_ENTRY_SCORE  # V7.5: 7 (operator-fixed)
    signal_feed: str | None = None
    signal_price_scale: float = 1.0
    index_option: bool = False


SYMBOLS: dict[str, SymbolConfig] = {
    "SPY": SymbolConfig(
        symbol="SPY",
        min_debit=75.0,
        max_debit=175.0,
        max_otm_pct=0.60,
        profit_target_pct=15.0,
        bearish_profit_target_pct=15.0,
        stop_loss_pct=20.0,
        bearish_stop_loss_pct=15.0,
    ),
    "QQQ": SymbolConfig(
        symbol="QQQ",
        min_debit=30.0,
        max_debit=150.0,
        max_otm_pct=1.50,
        profit_target_pct=15.0,
        bearish_profit_target_pct=15.0,
        stop_loss_pct=20.0,
        bearish_stop_loss_pct=15.0,
    ),
}


def config_for_symbol(symbol: str) -> SymbolConfig:
    """Return the SymbolConfig for a given symbol, raising KeyError if unknown."""
    cfg = SYMBOLS.get(symbol.upper())
    if cfg is None:
        raise ValueError(f"unknown_symbol:{symbol}")
    return cfg


def is_index_option_symbol(symbol: str) -> bool:
    """Return whether a symbol requires index-option session and quote handling."""
    return config_for_symbol(symbol).index_option


def entry_score_for_symbol(symbol: str) -> int:
    """Return the normal completed-bar score floor for this symbol."""
    return config_for_symbol(symbol).min_entry_score


def _bar_time_hhmm(value: Any) -> int | None:
    """Best-effort parse of a bar clock label into HHMM ET int."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 2359 else None
    if isinstance(value, float) and math.isfinite(value):
        ivalue = int(value)
        return ivalue if 0 <= ivalue <= 2359 else None
    if isinstance(value, str) and value:
        # ISO timestamps: ...T14:35:00...
        if "T" in value and len(value) >= 16:
            try:
                hour = int(value[11:13])
                minute = int(value[14:16])
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return hour * 100 + minute
            except ValueError:
                return None
        try:
            ivalue = int(value)
            return ivalue if 0 <= ivalue <= 2359 else None
        except ValueError:
            return None
    return None


def effective_entry_score_floor(
    symbol: str = SYMBOL,
    *,
    bar_time: Any = None,
    setup: dict[str, Any] | None = None,
) -> int:
    """Return the active score floor, including late-entry bump after 14:30 ET.

    ``effective_min_score`` used to exist only inside ``signal()``. Live-trigger
    and crash/pump paths referenced it unbound and crashed with NameError,
    silently killing IWM/SPY/QQQ live confirmations.
    """
    required = entry_score_for_symbol(symbol)
    tm = _bar_time_hhmm(bar_time)
    if tm is None and isinstance(setup, dict):
        for key in ("signalBarEnd", "signalBarStart", "barTime", "time"):
            tm = _bar_time_hhmm(setup.get(key))
            if tm is not None:
                break
    if tm is not None and tm >= 1430:
        return required + LATE_ENTRY_SCORE_BUMP
    return required


def trade_limit_for_symbol(symbol: str) -> int | None:
    """Return the per-symbol entry ceiling, or ``None`` when it is unrestricted."""
    return config_for_symbol(symbol).trade_limit


def signal_feed_for_symbol(symbol: str) -> str:
    """Return the approved continuous indicator feed for a scanner symbol."""
    cfg = config_for_symbol(symbol)
    return cfg.signal_feed or cfg.symbol


def signal_price_scale_for_symbol(symbol: str) -> float:
    """Return the static scale used to seed an index parity lookup from its feed."""
    return config_for_symbol(symbol).signal_price_scale


def index_reference_feed_for_symbol(
    symbol: str, *, now: datetime | None = None,
) -> str:
    """Return the best reference feed for a broker-native index parity lookup.

    XSP keeps ES=F for continuous overnight indicators, but throughout its
    daytime venue session the cash S&P 500 index is the closer reference for
    XSP. The cash feed's last regular close remains a better seed than a stale
    futures proxy during the 16:00–16:55 XSP-only tail. Using the futures proxy
    can exceed the bridge's reference-deviation guard even while the option
    books contain a valid executable parity pair.
    """
    cfg = config_for_symbol(symbol)
    if not cfg.index_option:
        return cfg.signal_feed or cfg.symbol
    current = (now or datetime.now(ET)).astimezone(ET)
    tm = current.hour * 100 + current.minute
    if current.weekday() < 5 and 930 <= tm < 1655:
        return "^GSPC"
    return cfg.signal_feed or cfg.symbol


def underlying_quote_kind_for_symbol(symbol: str) -> str:
    """Return the required broker quote envelope for a scanner symbol."""
    return "index_quote" if is_index_option_symbol(symbol) else "equity_quote"


def trading_session_key(symbol: str, now: datetime | None = None) -> str:
    """Return the risk-accounting session date, including XSP's overnight session."""
    current = (now or now_et()).astimezone(ET)
    if not is_index_option_symbol(symbol):
        return current.date().isoformat()
    tm = current.hour * 100 + current.minute
    weekday = current.weekday()
    # Overnight session (20:15+) on Sun–Thu belongs to the next business day.
    if tm >= 2015 and weekday in {6, 0, 1, 2, 3}:
        return (current.date() + timedelta(days=1)).isoformat()
    # Venue gap (17:00–20:14) on Mon–Thu also belongs to the next session.
    if 1700 <= tm < 2015 and weekday <= 3:
        return (current.date() + timedelta(days=1)).isoformat()
    return current.date().isoformat()


def xsp_market_open(now: datetime) -> bool:
    """Return whether Robinhood can provide a current executable XSP quote."""
    current = now.astimezone(ET)
    if current.weekday() >= 5 or is_market_holiday(current.date()):
        return False
    tm = current.hour * 100 + current.minute
    return XSP_BROKER_SESSION_START <= tm < XSP_BROKER_SESSION_END


def entry_window_open(now: datetime, symbol: str = SYMBOL) -> bool:
    """Return whether new paper entries may be evaluated for this symbol."""
    current = now.astimezone(ET)
    if is_market_holiday(current.date()):
        return False
    if not is_index_option_symbol(symbol):
        tm = current.hour * 100 + current.minute
        return ENTRY_START <= tm <= ENTRY_END
    if not xsp_market_open(current):
        return False
    tm = current.hour * 100 + current.minute
    weekday = current.weekday()
    if weekday == 6:
        return tm >= 2020  # first completed five-minute overnight bar
    if tm < 925:
        return tm <= 855  # exit by 09:20 before the 09:25 venue gap
    if 930 <= tm < 1700:
        return 935 <= tm <= 1630  # exit by 16:55 before the 17:00 venue gap
    return tm >= 2020


def hard_close_due(now: datetime, symbol: str = SYMBOL) -> bool:
    """Return whether an open position must exit before a venue/session boundary."""
    current = now.astimezone(ET)
    tm = current.hour * 100 + current.minute
    if not is_index_option_symbol(symbol):
        return tm >= HARD_CLOSE
    # Preserve time to obtain a verified bid before each XSP venue gap.
    return 920 <= tm < 930 or 1655 <= tm < 2015


def effective_max_otm_pct(symbol: str, *, dte: int = OPTION_DTE_TARGET) -> float:
    """Return the max OTM% widened for calendar distance to expiry.

    On Fridays, 1 business DTE means Monday expiry (3 calendar days). The extra
    time value makes near-ATM options too expensive for the debit cap, so we
    must go deeper OTM to stay within budget. BSM analysis shows QQQ needs
    ~2.5-3.8% OTM on Fridays to find puts in the $30-100 range.

    Scale: base * calendar_days * 2, capped at 8x base.
    Friday (3 cal days): 0.60% * 6 = 3.60% OTM allowed.
    """
    cfg = config_for_symbol(symbol)
    base = cfg.max_otm_pct
    today = now_et().date()
    cursor = today
    business = 0
    while business < dte:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            business += 1
    calendar_days = (cursor - today).days
    if calendar_days <= 1:
        return base
    scale = min(calendar_days * 2, 8)
    return round(base * scale, 2)


# Module-level constants for backward compat (SPY defaults)
MAX_DEBIT = SYMBOLS["SPY"].max_debit
MIN_DEBIT = SYMBOLS["SPY"].min_debit
MAX_OTM_PCT = SYMBOLS["SPY"].max_otm_pct
PROFIT_TARGET_PCT = SYMBOLS["SPY"].profit_target_pct
BEARISH_PROFIT_TARGET_PCT = SYMBOLS["SPY"].bearish_profit_target_pct
STOP_LOSS_PCT = SYMBOLS["SPY"].stop_loss_pct
BEARISH_STOP_LOSS_PCT = SYMBOLS["SPY"].bearish_stop_loss_pct


def strategy_config() -> dict[str, Any]:
    return {
        "symbol": SYMBOL,
        "release": v7_release.identity(),
        "maxTrades": MAX_TRADES,
        "debitRange": [MIN_DEBIT, MAX_DEBIT],
        "maxSpreadPct": MAX_QUOTE_SPREAD_PCT,
        "symbols": {
            symbol: vars(cfg)
            for symbol, cfg in sorted(SYMBOLS.items())
        },
        "maxOtmPct": MAX_OTM_PCT,
        "scoreGate": MIN_ENTRY_SCORE,
        "lossCooldown": {
            "consecutiveLosses": CONSECUTIVE_LOSS_COOLDOWN,
            "requiredScore": MIN_SCORE_AFTER_LOSS,
        },
        "relativeVolumeGate": MIN_RELATIVE_VOLUME,
        "bearishHighVolumeRelVol": BEARISH_HIGH_VOLUME_REL_VOL,
        "directionEdge": MIN_DIRECTION_EDGE,
        "dailyPlannedRisk": MAX_DAILY_LOSS,
        "profitTargetPct": PROFIT_TARGET_PCT,
        "profitTargetPctByDirection": {
            "bullish": PROFIT_TARGET_PCT,
            "bearish": BEARISH_PROFIT_TARGET_PCT,
        },
        "stopLossPctByDirection": {
            "bullish": STOP_LOSS_PCT,
            "bearish": BEARISH_STOP_LOSS_PCT,
        },
        "antiChaseSigmaByDirection": {
            "bullish": BULLISH_ANTI_CHASE_SIGMA,
            "bearish": BEARISH_ANTI_CHASE_SIGMA,
        },
        "crashBypassesLossCooldown": True,
        "stopLossPct": STOP_LOSS_PCT,
        "exitSystem": "dynamic_signal_strength",
        "signalReversal": {
            "confirmationBars": SIGNAL_REVERSAL_CONFIRM_BARS,
            "vwapBufferPct": SIGNAL_REVERSAL_VWAP_BUFFER_PCT,
            "momentumLookbackBars": SIGNAL_REVERSAL_MOMENTUM_LOOKBACK_BARS,
            "basis": "completed_5m_bars",
        },
        "breakevenHoldMinutes": f"{dynamic_breakeven_hold_minutes.__name__}(pos) — range 6–15 min by signal strength",
        "trailStartPct": f"{dynamic_trail_start_pct.__name__}(pos) — range 5–12% by signal strength",
        "trailGivebackPct": f"{dynamic_trail_giveback_pct.__name__}(pos) — range 2–5% by signal strength",
        "maxHoldMinutes": f"{dynamic_max_hold_minutes.__name__}(pos) — range 15–35 min by signal strength",
        "entryWindow": [ENTRY_START, ENTRY_END],
        "premarketWatchStart": PREMARKET_WATCH_START,
        "marketOpen": MARKET_OPEN,
        "openingWarmupBars": OPENING_WARMUP_BARS,
        "openingMaxBreakoutExtensionAtr": OPENING_MAX_BREAKOUT_EXTENSION_ATR,
        "lunchBlock": [LUNCH_START, LUNCH_END],
        "hardClose": HARD_CLOSE,
        "optionDteTarget": OPTION_DTE_TARGET,
        "orbEnabled": ORB_ENABLED,
        "maxCompletedBarAgeSeconds": MAX_COMPLETED_BAR_AGE_SECONDS,
        "maxIndexBarAgeSeconds": MAX_INDEX_BAR_AGE_SECONDS,
        "maxLiveEquityQuoteAgeSeconds": MAX_LIVE_EQUITY_QUOTE_AGE_SECONDS,
        "maxIndexCandidateUnderlyingQuoteAgeSeconds": MAX_INDEX_CANDIDATE_UNDERLYING_QUOTE_AGE_SECONDS,
    }


def source_code_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def price_exit_reason(
    position: dict[str, Any],
    bid: float,
    *,
    now: datetime,
    peak_bid: float | None = None,
    entry_price: float | None = None,
    symbol: str | None = None,
) -> str | None:
    """Evaluate quote-driven exits shared by paper and future live monitoring.

    Structural completed-bar exits remain in ``_monitor_locked``. This helper
    owns only exits that can be decided from one validated executable option
    quote plus durable position timing/state.
    """
    try:
        reference = float(
            position.get("entryAsk", 0.0) if entry_price is None else entry_price
        )
        bid_value = float(bid)
        entry_time = datetime.fromisoformat(str(position["entryTime"])).astimezone(ET)
        current_time = now.astimezone(ET)
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(reference) or not math.isfinite(bid_value) or reference <= 0 or bid_value <= 0:
        return None
    held_minutes = (current_time - entry_time).total_seconds() / 60.0
    if held_minutes < 0:
        return None
    pricing_position = dict(position)
    pricing_position["entryAsk"] = reference
    peak = max(
        bid_value,
        float(peak_bid if peak_bid is not None else position.get("peakBid", bid_value)),
    )
    pnl_pct = (bid_value / reference - 1.0) * 100.0
    peak_pct = (peak / reference - 1.0) * 100.0
    effective_symbol = symbol or str(position.get("symbol", SYMBOL))
    max_hold = dynamic_max_hold_minutes(pricing_position)
    stop_loss_pct = stop_loss_pct_for_position(pricing_position)
    profit_target_pct = profit_target_pct_for_position(pricing_position)
    trail_start = dynamic_trail_start_pct(pricing_position)
    trail_giveback = dynamic_trail_giveback_pct(pricing_position)
    breakeven_hold = dynamic_breakeven_hold_minutes(pricing_position)

    if hard_close_due(current_time, effective_symbol):
        return "hard_close"
    if pnl_pct <= -stop_loss_pct:
        return "stop_loss"
    if pnl_pct >= profit_target_pct:
        return "profit_target"
    if peak_pct >= trail_start and pnl_pct <= 0:
        return "breakeven_stop"
    if peak_pct >= trail_start and pnl_pct <= peak_pct - trail_giveback:
        return "trailing_stop"
    if held_minutes >= max_hold:
        return "time_stop"
    if held_minutes >= breakeven_hold and pnl_pct < 0.0:
        return "breakeven_timeout"
    if held_minutes >= MIN_HOLD_MINUTES and pnl_pct < -3.0 and peak_pct < trail_start:
        return "breakeven_exit"
    return None


def profit_target_pct_for_position(position: dict[str, Any]) -> float:
    """V7.4.4: Dynamic profit target based on entry signal strength and ATR.

    Strong entries get wider ATR-multiple targets (let winners run).
    Weak entries get tighter targets (lock in smaller gains).

    The target is the option-side percentage that corresponds to the underlying
    moving N × ATR in the entry direction. Capped at DYNAMIC_TARGET_PCT_CAP.

    Math: underlying moves N*ATR dollars. Option moves by delta * N * ATR.
    Target % = (delta * N * ATR) / option_entry_price * 100.
    We use turtleN (the stored ATR) and entryAsk (the option price paid).
    """
    cfg = config_for_symbol(position.get("symbol", "SPY"))
    if position.get("direction") == "bearish":
        base_target = cfg.bearish_profit_target_pct
    else:
        base_target = cfg.profit_target_pct
    if _is_nonstructural_signal(position):
        return min(base_target, FAST_PROFIT_TARGET_PCT)
    strength = _entry_signal_strength(position)
    atr_mult = DYNAMIC_TARGET_ATR_MULT_MIN + (DYNAMIC_TARGET_ATR_MULT_MAX - DYNAMIC_TARGET_ATR_MULT_MIN) * strength
    turtle_n = float(position.get("turtleN", 0.0))
    entry_ask = float(position.get("entryAsk", 0.0))
    if entry_ask > 0 and turtle_n > 0:
        # Option moves by delta * underlying_move. Use 0.40 as conservative delta proxy.
        option_move = 0.40 * atr_mult * turtle_n
        # Target % relative to the option price, not the underlying.
        raw_target = (option_move / entry_ask) * 100
        dynamic_target = min(raw_target, DYNAMIC_TARGET_PCT_CAP)
        # Never go below 5% (too tight causes noise exits) or above the cap
        return max(5.0, dynamic_target)
    # Fallback to fixed target if ATR data is unavailable
    return base_target

def stop_loss_pct_for_position(position: dict[str, Any]) -> float:
    """Return the directional stop-loss percentage for a position."""
    cfg = config_for_symbol(position.get("symbol", SYMBOL))
    if position.get("direction") == "bearish" or position.get("optionType") == "put":
        return cfg.bearish_stop_loss_pct
    return cfg.stop_loss_pct


def config_sha256() -> str:
    encoded = json.dumps(strategy_config(), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def calendar_block(now: datetime) -> str | None:
    """Fail closed unless a recently reviewed calendar covers this session."""
    try:
        payload = json.loads(CALENDAR_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return "macro_calendar_unavailable"
    try:
        if payload.get("timezone") != "America/New_York" or not payload.get("source"):
            raise ValueError("calendar metadata")
        generated_at = datetime.fromisoformat(str(payload["generatedAt"]).replace("Z", "+00:00"))
        if generated_at.tzinfo is None:
            raise ValueError("naive generatedAt")
        coverage_start = datetime.fromisoformat(str(payload["coverageStart"])).date()
        coverage_through = datetime.fromisoformat(str(payload["coverageThrough"])).date()
        days = payload["days"]
        if not isinstance(days, dict):
            raise ValueError("days")
    except (KeyError, TypeError, ValueError):
        return "macro_calendar_stale_or_incomplete"
    current_time = now.astimezone(ET)
    calendar_age = current_time - generated_at.astimezone(ET)
    if (calendar_age > timedelta(days=MAX_CALENDAR_AGE_DAYS)
            or calendar_age < timedelta(hours=-1)
            or not coverage_start <= current_time.date() <= coverage_through):
        return "macro_calendar_stale_or_incomplete"
    current = current_time.hour * 60 + current_time.minute
    for event in days.get(current_time.strftime("%Y-%m-%d"), []):
        if event.get("action") != "block_entries":
            continue
        try:
            start_h, start_m = map(int, event["start"].split(":"))
            end_h, end_m = map(int, event["end"].split(":"))
        except (KeyError, AttributeError, TypeError, ValueError):
            return "macro_calendar_stale_or_incomplete"
        if start_h * 60 + start_m <= current <= end_h * 60 + end_m:
            return f"macro_block_{event.get('event', 'scheduled_event')}"
    return None


def now_et() -> datetime:
    return datetime.now(ET)


def today_key() -> str:
    return now_et().strftime("%Y-%m-%d")


class StateCorruptionError(RuntimeError):
    """State cannot be trusted and trading must fail closed."""


class StateLockBusy(RuntimeError):
    """Another V7.4.2 process owns the state mutation lock."""


@contextmanager
def exclusive_state_lock(symbol: str = SYMBOL):
    lock_path = state_lock_path(symbol)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StateLockBusy("v73_state_lock_busy") from exc
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def state_metadata(symbol: str = SYMBOL) -> dict[str, Any]:
    """Static state fields derived from the active source configuration."""
    cfg = config_for_symbol(symbol)
    return {
        "version": ENGINE_VERSION,
        "strategyId": STRATEGY_ID,
        "sourceCodeSha256": source_code_sha256(),
        "configSha256": config_sha256(),
        "mode": "dry_run",
        "symbol": symbol,
        "strategy": f"1DTE naked {symbol} directional scalps; real ask entry / real bid exit",
        "tradeLimit": cfg.trade_limit,
        "maxDebit": cfg.max_debit,
        "minDebit": cfg.min_debit,
        "dailyLossLimit": MAX_DAILY_LOSS,
        "liveOrdersEnabled": False,
    }


def default_state(symbol: str = SYMBOL) -> dict[str, Any]:
    return {
        **state_metadata(symbol),
        "lastResetDate": "",
        "dailyTrades": 0,
        "dailyPnl": 0.0,
        "dailyLoss": 0.0,
        "dailyGrossLoss": 0.0,
        "activePositions": [],
        "pendingEntry": None,
        "stateRevision": 0,
        "stateSchemaVersion": 1,
        "fundingGate": "blocked_pending_quote_validated_positive_dry_run",
        "consecutiveLosses": 0,
        # V7.5.3 re-entry risk policy state. Optional fields: older state files
        # load_state()-merge these defaults, so they are NOT in the required
        # schema set.
        "lastEnteredSignalBarEnd": None,
        "lastLossExitAt": None,
        "lastLossDirection": None,
    }


def _ledger_event_time(event: dict[str, Any]) -> datetime | None:
    raw = event.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(ET) if parsed.tzinfo else None


def reconcile_state_from_ledger(state: dict[str, Any], *, now: datetime, symbol: str = SYMBOL) -> dict[str, Any]:
    _log = log_path(symbol)
    if not _log.exists():
        return state
    entries: dict[str, dict[str, Any]] = {}
    exited: set[str] = set()
    existing_positions = {
        str(position.get("positionId")): position
        for position in state.get("activePositions", [])
        if isinstance(position, dict) and position.get("positionId")
    }
    daily_trades = 0
    daily_pnl = 0.0
    daily_gross_loss = 0.0
    consecutive_losses = 0
    today = trading_session_key(symbol, now)
    try:
        lines = _log.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise StateCorruptionError(f"ledger_unreadable:{exc}") from exc
    for line_number, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StateCorruptionError(f"ledger_json_error_line_{line_number}:{exc}") from exc
        if not isinstance(event, dict):
            raise StateCorruptionError(f"ledger_non_object_line_{line_number}")
        event_type = event.get("event")
        position_id = event.get("positionId")
        event_time = _ledger_event_time(event)
        if event_type == "PAPER_ENTRY_REAL_ASK":
            if not isinstance(position_id, str) or not position_id:
                raise StateCorruptionError(f"ledger_entry_missing_position_id_line_{line_number}")
            if position_id in entries:
                raise StateCorruptionError(f"duplicate_entry:{position_id}")
            entries[position_id] = {
                key: value for key, value in event.items()
                if key not in {"event", "timestamp", "mode"}
            }
            if event_time and trading_session_key(symbol, event_time) == today:
                daily_trades += 1
        elif event_type == "PAPER_EXIT_REAL_BID":
            if not isinstance(position_id, str) or not position_id:
                raise StateCorruptionError(f"ledger_exit_missing_position_id_line_{line_number}")
            if position_id not in entries:
                raise StateCorruptionError(f"unmatched_exit:{position_id}")
            if position_id in exited:
                raise StateCorruptionError(f"duplicate_exit:{position_id}")
            exited.add(position_id)
            try:
                pnl = float(event["pnl"])
            except (KeyError, TypeError, ValueError) as exc:
                raise StateCorruptionError(f"invalid_exit_pnl_line_{line_number}") from exc
            if not math.isfinite(pnl):
                raise StateCorruptionError(f"nonfinite_exit_pnl_line_{line_number}")
            # V7.4.7: only count consecutive losses within the CURRENT session.
            # All-time streak was a deadlock — last 2 losses from days ago blocked
            # every score-7 signal forever until a win that could never fire.
            if event_time and trading_session_key(symbol, event_time) == today:
                consecutive_losses = consecutive_losses + 1 if pnl < 0 else 0
                daily_pnl += pnl
                if pnl < 0:
                    daily_gross_loss += abs(pnl)
    active_positions: list[dict[str, Any]] = []
    for position_id, entry in entries.items():
        if position_id in exited:
            continue
        position = dict(entry)
        previous = existing_positions.get(position_id)
        if previous:
            # Ledger reconciliation restores immutable entry fields. Preserve
            # monitor-owned fields from the durable state so a quote-bridge
            # failure cannot erase the last trusted position quote or exit
            # confirmation state before the health gate inspects it.
            for field in ("lastQuote", "peakBid", "pendingExitReason", "exitConfirmCount"):
                if field in previous:
                    position[field] = previous[field]
        active_positions.append(position)
    if len(active_positions) > 1:
        raise StateCorruptionError(f"multiple_unmatched_positions:{len(active_positions)}")
    state.update({
        "activePositions": active_positions,
        "dailyTrades": daily_trades,
        "dailyPnl": round(daily_pnl, 2),
        "dailyLoss": round(max(0.0, -daily_pnl), 2),
        "dailyGrossLoss": round(daily_gross_loss, 2),
        "consecutiveLosses": consecutive_losses,
    })
    return state


def validate_state_schema(data: dict[str, Any], symbol: str = SYMBOL) -> None:
    required = {
        "version", "mode", "symbol", "lastResetDate", "dailyTrades",
        "dailyPnl", "dailyLoss", "dailyGrossLoss", "activePositions",
        "liveOrdersEnabled", "consecutiveLosses",
    }
    missing = sorted(required.difference(data))
    if missing:
        raise StateCorruptionError(f"state_missing_fields:{','.join(missing)}")
    if data.get("mode") != "dry_run" or data.get("liveOrdersEnabled") is not False:
        raise StateCorruptionError("state_mode_or_symbol_invalid")
    if not isinstance(data.get("version"), str) or not isinstance(data.get("lastResetDate"), str):
        raise StateCorruptionError("state_text_fields_invalid")
    for key in ("dailyTrades", "consecutiveLosses"):
        value = data.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise StateCorruptionError(f"state_invalid_nonnegative_integer:{key}")
    for key in ("dailyPnl", "dailyLoss", "dailyGrossLoss"):
        value = data.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise StateCorruptionError(f"state_invalid_number:{key}")
        if key == "dailyGrossLoss" and value < 0:
            raise StateCorruptionError("state_invalid_nonnegative_number:dailyGrossLoss")
    positions = data.get("activePositions")
    if not isinstance(positions, list) or len(positions) > 1 or any(not isinstance(item, dict) for item in positions):
        raise StateCorruptionError("state_active_positions_invalid")


def _migrate_legacy_state(symbol: str) -> None:
    """Rename the old single-symbol state/log files to per-symbol names if needed."""
    sp = state_path(symbol)
    if not sp.exists() and _STATE_PATH_LEGACY.exists():
        import shutil
        shutil.move(str(_STATE_PATH_LEGACY), str(sp))
    lp = log_path(symbol)
    if not lp.exists() and _LOG_PATH_LEGACY.exists():
        import shutil
        shutil.move(str(_LOG_PATH_LEGACY), str(lp))

def load_state(symbol: str = SYMBOL) -> dict[str, Any]:
    _migrate_legacy_state(symbol)
    sp = state_path(symbol)
    if not sp.exists():
        return reconcile_state_from_ledger(default_state(symbol), now=now_et(), symbol=symbol)
    try:
        data = json.loads(sp.read_text())
        if not isinstance(data, dict):
            raise StateCorruptionError("state_root_must_be_object")
        validate_state_schema(data, symbol)
        metadata = state_metadata(symbol)
        metadata_needs_persist = any(data.get(key) != value for key, value in metadata.items())
        state = {**default_state(symbol), **data}
        state.update(metadata)
        if metadata_needs_persist:
            state["_stateMetadataNeedsPersist"] = True
        state.setdefault("consecutiveLosses", 0)
        state.setdefault("dailyGrossLoss", 0.0)
        return reconcile_state_from_ledger(state, now=now_et(), symbol=symbol)
    except StateCorruptionError:
        raise
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise StateCorruptionError(f"state_unreadable:{exc}") from exc


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(data, indent=2) + "\n"
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()


def save_state(state: dict[str, Any], symbol: str = SYMBOL) -> None:
    # Internal load-state migration metadata must never reach the durable schema.
    state.pop("_stateMetadataNeedsPersist", None)
    state["stateRevision"] = int(state.get("stateRevision", 0)) + 1
    state["updatedAt"] = now_et().isoformat()
    atomic_write_json(state_path(symbol), state)


def reset_daily(
    state: dict[str, Any], *, symbol: str = SYMBOL, now: datetime | None = None,
) -> None:
    session_date = trading_session_key(symbol, now or now_et())
    if state.get("lastResetDate") != session_date:
        state.update({
            "lastResetDate": session_date,
            "dailyTrades": 0,
            "dailyPnl": 0.0,
            "dailyLoss": 0.0,
            "dailyGrossLoss": 0.0,
            "consecutiveLosses": 0,  # V7.4.7: session-scoped — don't carry losses across days
        })


def log(event: dict[str, Any], symbol: str = SYMBOL) -> None:
    _log = log_path(symbol)
    _log.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(event)
    payload.setdefault("timestamp", now_et().isoformat())
    payload.setdefault("mode", "dry_run")
    payload.setdefault("symbol", symbol)
    payload.setdefault("eventId", uuid.uuid4().hex)
    # Lineage is process-owned. Never allow a stale caller-supplied identity
    # to create a mixed-version entry or exit event.
    payload["engineVersion"] = ENGINE_VERSION
    payload["strategyId"] = STRATEGY_ID
    payload["releaseManifestSha256"] = RELEASE_MANIFEST_SHA256
    payload["sourceCodeSha256"] = source_code_sha256()
    payload["configSha256"] = config_sha256()
    encoded = json.dumps(payload, separators=(",", ":")) + "\n"
    with _log.open("a", encoding="utf-8") as handle:
        write = handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _fetch_yahoo_chart_result(symbol: str, start: int, end: int) -> dict[str, Any]:
    """Fetch one Yahoo chart result with bounded host failover.

    This is an external read-only data boundary. A TLS timeout, transient HTTP
    failure, malformed JSON response, or Yahoo chart error is retried against
    the alternate chart edge. If the bounded budget is exhausted, raise a
    diagnostic error so the caller remains fail-closed.
    """
    errors: list[str] = []
    for attempt in range(YAHOO_CHART_MAX_ATTEMPTS):
        host = YAHOO_CHART_HOSTS[attempt % len(YAHOO_CHART_HOSTS)]
        url = (
            f"https://{host}/v8/finance/chart/{symbol}"
            f"?interval=5m&period1={start}&period2={end}&includePrePost=true"
        )
        request = urllib.request.Request(url, headers=UA)
        try:
            with urllib.request.urlopen(request, timeout=YAHOO_CHART_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode())
            if not isinstance(payload, dict):
                raise ValueError("yahoo_chart_payload_not_object")
            chart = payload.get("chart")
            if not isinstance(chart, dict):
                raise ValueError("yahoo_chart_missing_container")
            if chart.get("error"):
                raise RuntimeError(f"yahoo_chart_error:{chart['error']}")
            results = chart.get("result")
            if not isinstance(results, list) or not results or not isinstance(results[0], dict):
                raise ValueError("yahoo_chart_missing_result")
            return results[0]
        except Exception as exc:  # bounded external transport/data boundary
            errors.append(f"{host}:{type(exc).__name__}:{exc}")
            if attempt < YAHOO_CHART_MAX_ATTEMPTS - 1:
                time.sleep(YAHOO_CHART_RETRY_BACKOFF_SECONDS)

    detail = errors[-1] if errors else "no_attempts"
    attempted_hosts = ",".join(YAHOO_CHART_HOSTS[: min(YAHOO_CHART_MAX_ATTEMPTS, len(YAHOO_CHART_HOSTS))])
    raise RuntimeError(
        f"yahoo_chart_fetch_failed:{symbol}:hosts={attempted_hosts}:last={detail}"
    )


def fetch_bars(symbol: str = SYMBOL, days: int = 5, *, force: bool = False) -> list[dict[str, Any]]:
    # ``force`` is consumed by the persistent daemon's cache wrapper. The
    # standalone engine always performs a real fetch, preserving CLI behavior.
    del force
    end = int(datetime.now().timestamp())
    start = int((datetime.now() - timedelta(days=days)).timestamp())
    result = _fetch_yahoo_chart_result(symbol, start, end)
    quote = result["indicators"]["quote"][0]
    output: list[dict[str, Any]] = []
    for index, epoch in enumerate(result["timestamp"]):
        try:
            o, h, l, c, v = (quote[key][index] for key in ("open", "high", "low", "close", "volume"))
            if any(value is None or math.isnan(value) for value in (o, h, l, c, v)):
                continue
            dt = datetime.fromtimestamp(epoch, ET)
            output.append({"dt": dt, "date": dt.strftime("%Y-%m-%d"), "time": dt.hour * 100 + dt.minute,
                           "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(v)})
        except (IndexError, TypeError):
            continue
    return output


def bar_hhmm(bar: dict[str, Any]) -> int:
    """Return ET HHMM from a normalized bar, tolerating legacy fixtures."""
    raw_time = bar.get("time")
    if isinstance(raw_time, (int, float)) and not isinstance(raw_time, bool):
        return int(raw_time)
    when = bar.get("dt")
    if isinstance(when, datetime) and when.tzinfo is not None:
        localized = when.astimezone(ET)
        return localized.hour * 100 + localized.minute
    return -1


def xsp_bar_session_key(bar: dict[str, Any]) -> str | None:
    """Map a legal XSP bar to its cross-midnight trading-session date.

    XSP trades on CBOE with these sessions (ET):
      - Overnight: Sun 20:15 – Mon 09:25, Mon–Thu 20:15 – next day 09:25
      - Day: 09:30 – 16:55
      - Evening: Mon–Thu 17:00+ belongs to the next day's session

    Bars in the 17:00–20:14 gap (between day close and overnight open)
    on Mon–Thu are pre-venue data for the next session.  Sunday bars
    before 20:15 have no session.  Saturday has no session.
    """
    when = bar.get("dt")
    if not isinstance(when, datetime) or when.tzinfo is None:
        return None
    current = when.astimezone(ET)
    tm = current.hour * 100 + current.minute
    weekday = current.weekday()
    if weekday == 5:  # Saturday: no session
        return None
    if weekday == 6:  # Sunday: 20:15+ belongs to Monday; earlier bars are pre-venue noise
        return (current.date() + timedelta(days=1)).isoformat() if tm >= 2015 else None
    if tm < 925 or 930 <= tm < 1700:
        # Regular session (pre-market or RTH): belongs to today
        return current.date().isoformat()
    if weekday <= 3 and tm >= 2015:
        # Mon–Thu 20:15+: overnight session belongs to tomorrow
        return (current.date() + timedelta(days=1)).isoformat()
    # 17:00–20:14 on Mon–Thu: venue gap.  These pre-venue bars
    # belong to the next session (the overnight that opens at 20:15).
    if weekday <= 3 and 1700 <= tm < 2015:
        return (current.date() + timedelta(days=1)).isoformat()
    # Friday daytime (before 17:00) already handled above.
    # Friday 17:00+ has no next session (weekend) — exclude.
    return None


def completed_day_bars(
    bars: list[dict[str, Any]], *, now: datetime, symbol: str = SYMBOL,
) -> list[dict[str, Any]]:
    current = now.astimezone(ET)
    if is_index_option_symbol(symbol):
        session_key = trading_session_key(symbol, current)
        return [
            bar for bar in bars
            if xsp_bar_session_key(bar) == session_key
            and isinstance(bar.get("dt"), datetime)
            and bar["dt"].astimezone(ET) + timedelta(minutes=BAR_INTERVAL_MINUTES) <= current
        ]
    today = current.date()
    return [
        bar for bar in bars
        if isinstance(bar.get("dt"), datetime)
        and bar["dt"].astimezone(ET).date() == today
        and MARKET_OPEN <= bar_hhmm(bar) <= 1555
        and bar["dt"].astimezone(ET) + timedelta(minutes=BAR_INTERVAL_MINUTES) <= current
    ]


def regular_history_bars(
    bars: list[dict[str, Any]], *, session_date: str, symbol: str = SYMBOL,
) -> list[dict[str, Any]]:
    """Return prior comparable-session bars, excluding inactive venue gaps."""
    if is_index_option_symbol(symbol):
        return [
            bar for bar in bars
            if (key := xsp_bar_session_key(bar)) is not None and key < session_date
        ]
    return [
        bar for bar in bars
        if str(bar.get("date", "")) < session_date
        and MARKET_OPEN <= int(bar.get("time", -1)) <= 1555
    ]


def build_premarket_context(
    bars: list[dict[str, Any]],
    *,
    now: datetime,
    symbol: str = SYMBOL,
    current_price: float | None = None,
) -> dict[str, Any]:
    """Build the auditable premarket range and the price target being followed.

    Extended-hours bars define context only. They never enter regular-session
    VWAP, volume, or indicator calculations.
    """
    current = now.astimezone(ET)
    if is_index_option_symbol(symbol):
        return {
            "ready": False,
            "reason": "not_applicable_for_index_session",
            "symbol": symbol,
            "sessionDate": trading_session_key(symbol, current),
            "previousClose": None,
            "observedAt": current.isoformat(),
            "engineVersion": ENGINE_VERSION,
        }
    session_date = current.date().isoformat()
    prior_regular = regular_history_bars(bars, session_date=session_date)
    premarket = [
        bar for bar in bars
        if bar.get("date") == session_date
        and PREMARKET_WATCH_START <= int(bar.get("time", -1)) < MARKET_OPEN
        and isinstance(bar.get("dt"), datetime)
        and bar["dt"].astimezone(ET) <= current
    ]
    previous_close = float(prior_regular[-1]["close"]) if prior_regular else None
    if not premarket:
        return {
            "ready": False,
            "reason": "no_premarket_bars",
            "symbol": symbol,
            "sessionDate": session_date,
            "previousClose": previous_close,
            "observedAt": current.isoformat(),
            "engineVersion": ENGINE_VERSION,
        }

    premarket_high = max(float(bar["high"]) for bar in premarket)
    premarket_low = min(float(bar["low"]) for bar in premarket)
    premarket_mid = (premarket_high + premarket_low) / 2.0
    last_bar_price = float(premarket[-1]["close"])
    price = float(current_price) if (
        isinstance(current_price, (int, float))
        and not isinstance(current_price, bool)
        and math.isfinite(current_price)
        and current_price > 0
    ) else last_bar_price
    if price > premarket_high:
        target_state = "above_premarket_high"
        active_direction = "bullish"
        active_target = premarket_high
    elif price < premarket_low:
        target_state = "below_premarket_low"
        active_direction = "bearish"
        active_target = premarket_low
    elif price >= premarket_mid:
        target_state = "inside_premarket_range"
        active_direction = "bullish"
        active_target = premarket_high
    else:
        target_state = "inside_premarket_range"
        active_direction = "bearish"
        active_target = premarket_low
    gap_pct = (
        (price / previous_close - 1.0) * 100.0
        if previous_close and previous_close > 0 else None
    )
    return {
        "ready": previous_close is not None,
        "reason": "ready" if previous_close is not None else "previous_close_unavailable",
        "symbol": symbol,
        "sessionDate": session_date,
        "previousClose": round(previous_close, 4) if previous_close is not None else None,
        "premarketOpen": round(float(premarket[0]["open"]), 4),
        "premarketHigh": round(premarket_high, 4),
        "premarketLow": round(premarket_low, 4),
        "premarketMid": round(premarket_mid, 4),
        "premarketLast": round(last_bar_price, 4),
        "bullishBreakoutTarget": round(premarket_high, 4),
        "bearishBreakoutTarget": round(premarket_low, 4),
        "currentPrice": round(price, 4),
        "activeDirection": active_direction,
        "activePriceTarget": round(active_target, 4),
        "distanceToTarget": round(abs(active_target - price), 4),
        "targetState": target_state,
        "gapPct": round(gap_pct, 4) if gap_pct is not None else None,
        "premarketBarsUsed": len(premarket),
        "lastPremarketBarStart": premarket[-1]["dt"].astimezone(ET).isoformat(),
        "observedAt": current.isoformat(),
        "engineVersion": ENGINE_VERSION,
    }


def track_premarket_price(
    symbol: str = SYMBOL,
    *,
    now: datetime | None = None,
    current_price: float | None = None,
    bars: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Update the per-symbol price-target snapshot without placing any trade."""
    observed_at = (now or now_et()).astimezone(ET)
    source_bars = bars if bars is not None else fetch_bars(symbol)
    context = build_premarket_context(
        source_bars,
        now=observed_at,
        symbol=symbol,
        current_price=current_price,
    )
    atomic_write_json(premarket_path(symbol), context)
    return context


def session_signal_inputs(
    bars: list[dict[str, Any]],
    day: list[dict[str, Any]],
    *,
    now: datetime,
    symbol: str = SYMBOL,
) -> tuple[list[dict[str, Any]], float | None, str, dict[str, Any]]:
    """Return prior-RTH warmup, opening volume, and today's premarket target."""
    if not day:
        return [], None, "unavailable", build_premarket_context(
            bars, now=now, symbol=symbol,
        )
    if is_index_option_symbol(symbol):
        session_date = xsp_bar_session_key(day[-1]) or trading_session_key(symbol, now)
    else:
        session_date = str(day[-1].get("date", now.astimezone(ET).date().isoformat()))
    history = regular_history_bars(bars, session_date=session_date, symbol=symbol)
    relative_volume: float | None = None
    relative_volume_source = "intraday_session"
    if len(day) < 35:
        relative_volume, relative_volume_source = relative_volume_for_session(day, history)
    premarket_context = build_premarket_context(bars, now=now, symbol=symbol)
    return history[-OPENING_WARMUP_BARS:], relative_volume, relative_volume_source, premarket_context


def relative_volume_for_session(
    day: list[dict[str, Any]], history: list[dict[str, Any]],
) -> tuple[float, str]:
    """Compare opening volume with prior sessions at the same clock times."""
    if not day:
        return 0.0, "unavailable"
    current_date = str(day[-1].get("date", ""))
    current_bars = day[-3:]
    expected: list[float] = []
    actual: list[float] = []
    for bar in current_bars:
        tm = int(bar.get("time", -1))
        peers = [
            float(item["volume"]) for item in history
            if str(item.get("date", "")) < current_date
            and int(item.get("time", -1)) == tm
            and isinstance(item.get("volume"), (int, float))
            and float(item["volume"]) > 0
        ]
        volume = bar.get("volume")
        if peers and isinstance(volume, (int, float)) and float(volume) >= 0:
            expected.append(mean(peers))
            actual.append(float(volume))
    if not expected or mean(expected) <= 0:
        return 0.0, "historical_same_clock_unavailable"
    return round(mean(actual) / mean(expected), 4), "historical_same_clock"


def validate_completed_bar_freshness(
    bars: list[dict[str, Any]], *, now: datetime,
    symbol: str = SYMBOL,
) -> tuple[bool, str]:
    if not bars:
        # Index-option symbols return empty day bars during the first few
        # minutes after the overnight venue opens, before enough 5-min bars
        # have completed.  Return a distinct reason so callers can skip
        # gracefully instead of hammering repeated errors.
        if is_index_option_symbol(symbol):
            return False, "venue_warmup_no_completed_bar"
        return False, "no_completed_bar"
    last_start = bars[-1].get("dt")
    if not isinstance(last_start, datetime) or last_start.tzinfo is None:
        return False, "invalid_bar_timestamp"
    bar_end = last_start.astimezone(ET) + timedelta(minutes=BAR_INTERVAL_MINUTES)
    age_seconds = (now.astimezone(ET) - bar_end).total_seconds()
    if age_seconds < 0:
        return False, "incomplete_bar"
    # Index-option symbols trade on CBOE with overnight sessions where
    # 5-minute bars can have wide gaps during low-liquidity periods.
    # Use a more generous staleness window for those symbols.
    max_age = MAX_INDEX_BAR_AGE_SECONDS if is_index_option_symbol(symbol) else MAX_COMPLETED_BAR_AGE_SECONDS
    if age_seconds > max_age:
        return False, "stale_completed_bar"
    return True, "ok"


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        return values[-1]
    value = mean(values[:period])
    alpha = 2 / (period + 1)
    for price in values[period:]:
        value = alpha * price + (1 - alpha) * value
    return value


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0
    changes = [values[i] - values[i - 1] for i in range(1, len(values))][-period:]
    gains = sum(max(change, 0) for change in changes) / period
    losses = sum(max(-change, 0) for change in changes) / period
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def macd_hist(values: list[float]) -> float:
    if len(values) < 35:
        return 0.0
    # Reconstruct a short MACD series so the signal EMA is meaningful.
    series = []
    for end in range(26, len(values) + 1):
        prefix = values[:end]
        series.append(ema(prefix, 12) - ema(prefix, 26))
    return series[-1] - ema(series, 9)


def vwap_bands(day: list[dict[str, Any]]) -> tuple[float, float, float]:
    volume = sum(bar["volume"] for bar in day)
    if not volume:
        return day[-1]["close"], day[-1]["close"], day[-1]["close"]
    vwap = sum(bar["close"] * bar["volume"] for bar in day) / volume
    variance = sum(bar["volume"] * (bar["close"] - vwap) ** 2 for bar in day) / volume
    sigma = math.sqrt(variance)
    return vwap, vwap + sigma, vwap - sigma


def atr(bars: list[dict[str, Any]], period: int = 10) -> float:
    if len(bars) < period + 1:
        return 0.0
    tr = [max(bars[i]["high"] - bars[i]["low"], abs(bars[i]["high"] - bars[i-1]["close"]),
              abs(bars[i]["low"] - bars[i-1]["close"])) for i in range(1, len(bars))]
    return mean(tr[-period:])


def time_normalized_rvol(day: list[dict[str, Any]], all_bars: list[dict[str, Any]],
                        symbol: str) -> float:
    """Time-of-day normalized relative volume.

    Compares the last 3 bars' average volume to the typical volume at the same
    time of day across the last N historical sessions, not the session average.
    This eliminates the bias where early entries always look high-volume (few bars)
    and late entries always look low-volume (many bars diluting the average).

    Returns a ratio: 1.0 = exactly average for this time of day, 2.0 = double.
    Falls back to the old session-average method if historical data is unavailable.
    """
    if len(day) < 3:
        return 0.0
    current_time = int(day[-1].get("time", 0))
    if current_time == 0:
        # Fallback: session average method
        today_vols = [bar["volume"] for bar in day]
        today_avg = mean(today_vols) if today_vols else 0.0
        today_recent = mean(today_vols[-3:])
        return today_recent / today_avg if today_avg > 0 else 0.0

    # Build historical baseline: for each of the last N sessions, find the bar
    # at the same time-of-day and collect its volume.
    session_date = str(day[-1].get("date", ""))
    historical_vols: list[float] = []
    # Group all_bars by date
    by_date: dict[str, list[dict[str, Any]]] = {}
    for bar in all_bars:
        d = str(bar.get("date", ""))
        if d and d != session_date:
            by_date.setdefault(d, []).append(bar)

    # Sort dates descending, take last N
    sorted_dates = sorted(by_date.keys(), reverse=True)[:RVOL_TIME_NORMALIZATION_LOOKBACK_DAYS]
    for d in sorted_dates:
        session_bars = by_date[d]
        # Find bar matching current_time (or closest within ±5 min)
        matching_vol = None
        for bar in session_bars:
            bt = int(bar.get("time", 0))
            if bt == current_time:
                matching_vol = float(bar["volume"])
                break
            elif abs(bt - current_time) <= 5:
                matching_vol = float(bar["volume"])
        if matching_vol is not None and matching_vol > 0:
            historical_vols.append(matching_vol)

    today_recent = mean([float(bar["volume"]) for bar in day[-3:]])

    if len(historical_vols) >= 3:
        expected_vol = mean(historical_vols)
        return today_recent / expected_vol if expected_vol > 0 else 0.0
    # Fallback: session average method (old behavior)
    today_vols = [bar["volume"] for bar in day]
    today_avg = mean(today_vols) if today_vols else 0.0
    return today_recent / today_avg if today_avg > 0 else 0.0


def turtle_intraday_context(bars: list[dict[str, Any]], entry_window: int = 12, exit_window: int = 6) -> dict[str, Any]:
    """Intraday Turtle adaptation for 5-minute scalps.

    Classic Turtles use 20/55 daily breakouts. On 5-minute SPY bars that is
    too slow for a scalp, so this preserves the rule shape while using a
    one-hour entry channel (12 bars), a 30-minute exit channel (6 bars), and
    ATR(10) as the volatility unit. Current bar is excluded from channels.
    """
    if len(bars) < max(entry_window, exit_window) + 1:
        return {"entryHigh": 0.0, "entryLow": 0.0, "exitHigh": 0.0, "exitLow": 0.0,
                "n": 0.0, "longBreakout": False, "shortBreakout": False}
    prior = bars[:-1]
    entry_high = max(bar["high"] for bar in prior[-entry_window:])
    entry_low = min(bar["low"] for bar in prior[-entry_window:])
    exit_high = max(bar["high"] for bar in prior[-exit_window:])
    exit_low = min(bar["low"] for bar in prior[-exit_window:])
    n = atr(bars, 10)
    close = bars[-1]["close"]
    return {"entryHigh": entry_high, "entryLow": entry_low,
            "exitHigh": exit_high, "exitLow": exit_low, "n": n,
            "longBreakout": close > entry_high, "shortBreakout": close < entry_low}


def supertrend_direction(bars: list[dict[str, Any]], period: int = 10, multiplier: float = 3.0) -> str:
    if len(bars) < period + 2:
        return "flat"
    a = atr(bars, period)
    direction = "up"
    level = (bars[0]["high"] + bars[0]["low"]) / 2 + multiplier * a
    for bar in bars[1:]:
        upper = (bar["high"] + bar["low"]) / 2 + multiplier * a
        lower = (bar["high"] + bar["low"]) / 2 - multiplier * a
        if direction == "up":
            level = max(level, lower)
            if bar["close"] < level:
                direction, level = "down", upper
        else:
            level = min(level, upper)
            if bar["close"] > level:
                direction, level = "up", lower
    return direction


def crash_signal(day: list[dict[str, Any]], symbol: str = SYMBOL) -> dict[str, Any] | None:
    """Detect rapid intraday momentum crashes (SPY selloffs, gap-down reversals).

    Triggers when:
    - MACD histogram is deeply negative (below -0.15) AND falling (3-bar delta < -0.03) OR
    - RSI has dropped from >55 to <40 in recent bars (momentum crash) OR
    - Price has dropped >0.4% from session high while below VWAP

    This catches the "SPY just started crashing" moment that the normal
    signal() misses because by the time all 8 gates align, the move is over.
    Uses prior-day bars for indicator warmup if available.
    """
    if len(day) < 10:
        return None
    closes = [bar["close"] for bar in day]
    volumes = [bar["volume"] for bar in day]

    # Need enough bars for MACD/RSI computation
    # Fetch prior days for warmup if we don't have 35 bars
    if len(closes) < 35:
        try:
            all_bars = fetch_bars(symbol)
            prior = regular_history_bars(all_bars, session_date=trading_session_key(symbol))[-40:]
            warmup_closes = [bar["close"] for bar in prior]
            full_closes = warmup_closes + closes
            full_volumes = [bar["volume"] for bar in prior] + volumes
        except:
            full_closes = closes
            full_volumes = volumes
    else:
        full_closes = closes
        full_volumes = volumes

    # Use the last 40 bars of the full series for computation
    series = full_closes[-40:] if len(full_closes) >= 40 else full_closes
    vol_series = full_volumes[-40:] if len(full_volumes) >= 40 else full_volumes
    day_series = day[-min(len(day), 40):]

    # MACD histogram current and recent (need 35+ bars)
    if len(series) < 35:
        return None
    current_rsi = rsi(series)
    hist_now = macd_hist(series)
    hist_prev = macd_hist(series[:-3]) if len(series) > 38 else 0.0
    delta_3 = hist_now - hist_prev

    # RSI 6 bars ago for crash rate detection
    rsi_now = rsi(series)
    rsi_prev = rsi(series[:-6]) if len(series) > 20 else 50.0

    # VWAP from today's bars only
    vwap, upper, lower = vwap_bands(day_series)
    close = closes[-1]

    # Volume: use today's bars only for relative volume (warmup distorts it)
    if len(day) >= 6:
        today_vols = [bar["volume"] for bar in day]
        today_avg = mean(today_vols)
        today_recent = mean(today_vols[-3:])
        rel_vol = today_recent / today_avg if today_avg > 0 else 0.0
    else:
        avg_vol = mean(vol_series[-25:-5]) if len(vol_series) > 25 else mean(vol_series)
        cur_vol = mean(vol_series[-3:]) if len(vol_series) >= 3 else vol_series[-1]
        rel_vol = cur_vol / avg_vol if avg_vol > 0 else 0.0

    # Session high and drop
    session_high = max(b["high"] for b in day_series)
    drop_from_high = (session_high - close) / session_high * 100

    # Crash conditions (multiple triggers)
    macd_crashing = hist_now < -0.15 and delta_3 < -0.03  # Deep negative + falling
    rsi_plunging = rsi_prev > 55 and rsi_now < 40  # RSI crashed through neutral
    sharp_drop = drop_from_high > 0.4 and close < vwap  # Down >0.4% from high + below VWAP
    volume_ok = rel_vol >= MIN_RELATIVE_VOLUME

    if (macd_crashing or rsi_plunging) and sharp_drop and volume_ok:
        e9, e21 = ema(series, 9), ema(series, 21)
        st = supertrend_direction(day_series[-35:] if len(day_series) >= 35 else day_series)
        momentum = (close / closes[-4] - 1) * 100 if len(closes) >= 4 else 0.0
        turtle = turtle_intraday_context(day_series)
        bear_score = sum([
            e9 < e21,  # ema_trend
            close < vwap,  # vwap
            st == "down",  # supertrend
            hist_now < 0,  # V7.4.12: actual MACD check (was hardcoded True)
            rsi_now < 50,  # V7.4.12: actual RSI check (was hardcoded True)
            momentum < -0.04,  # V7.4.12: kept at -0.04 — symmetric with bull, don't reject early bearish trends
            volume_ok,  # relative_volume
            turtle["shortBreakout"],  # V7.4.8: actual breakout
        ])
        return {
            "direction": "bearish",
            "score": bear_score,
            "independentScore": bear_score,
            "qualificationScore": bear_score,
            "substitutionFlags": [],
            "close": close, "ema9": e9, "ema21": e21,
            "vwap": vwap, "vwapUpper": upper, "vwapLower": lower,
            "rsi": rsi_now, "macdHist": hist_now,
            "supertrend": st, "momentumPct": momentum,
            "relativeVolume": rel_vol, "bullScore": 0, "bearScore": bear_score,
            "rawDirection": "bearish",
            "rejectionReasons": [],
            "bullChecks": {"ema_trend": False, "vwap": False, "supertrend": st == "down",
                           "macd": hist_now < 0, "rsi": rsi_now < 50, "momentum": momentum < -0.04,
                           "relative_volume": volume_ok, "turtle_breakout": turtle["longBreakout"]},
            "bearChecks": {"ema_trend": e9 < e21, "vwap": True, "supertrend": st == "down",
                           "macd": hist_now < 0, "rsi": rsi_now < 50, "momentum": momentum < -0.04,
                           "relative_volume": volume_ok, "turtle_breakout": turtle["shortBreakout"]},
            "turtle": turtle,
            "crash_detector": True,
            "macd_delta_3": round(delta_3, 4),
            "macd_hist": round(hist_now, 4),
            "rsi_drop": f"{rsi_prev:.0f}->{rsi_now:.0f}",
            "drop_from_high": round(drop_from_high, 2),
        }
    return None


def pump_signal(day: list[dict[str, Any]], symbol: str = SYMBOL) -> dict[str, Any] | None:
    """Detect rapid intraday momentum pumps (SPY rallies, gap-up surges).

    Triggers when:
    - MACD histogram is strongly positive (above +0.15) AND rising (3-bar delta > +0.03) OR
    - RSI has surged from <45 to >60 in recent bars (momentum pump) OR
    - Price has risen >0.4% from session low while above VWAP

    This catches the "SPY just started ripping" moment that the normal
    signal() misses because by the time all 8 gates align, the move is over.
    Uses prior-day bars for indicator warmup if available.
    """
    if len(day) < 10:
        return None
    closes = [bar["close"] for bar in day]
    volumes = [bar["volume"] for bar in day]

    # Need enough bars for MACD/RSI computation
    if len(closes) < 35:
        try:
            all_bars = fetch_bars(symbol)
            prior = regular_history_bars(all_bars, session_date=trading_session_key(symbol))[-40:]
            warmup_closes = [bar["close"] for bar in prior]
            full_closes = warmup_closes + closes
            full_volumes = [bar["volume"] for bar in prior] + volumes
        except:
            full_closes = closes
            full_volumes = volumes
    else:
        full_closes = closes
        full_volumes = volumes

    series = full_closes[-40:] if len(full_closes) >= 40 else full_closes
    vol_series = full_volumes[-40:] if len(full_volumes) >= 40 else full_volumes
    day_series = day[-min(len(day), 40):]

    if len(series) < 35:
        return None
    current_rsi = rsi(series)
    hist_now = macd_hist(series)
    hist_prev = macd_hist(series[:-3]) if len(series) > 38 else 0.0
    delta_3 = hist_now - hist_prev

    # RSI 6 bars ago for pump rate detection
    rsi_now = rsi(series)
    rsi_prev = rsi(series[:-6]) if len(series) > 20 else 50.0

    # VWAP from today's bars only
    vwap, upper, lower = vwap_bands(day_series)
    close = closes[-1]

    # Volume
    if len(day) >= 6:
        today_vols = [bar["volume"] for bar in day]
        today_avg = mean(today_vols)
        today_recent = mean(today_vols[-3:])
        rel_vol = today_recent / today_avg if today_avg > 0 else 0.0
    else:
        avg_vol = mean(vol_series[-25:-5]) if len(vol_series) > 25 else mean(vol_series)
        cur_vol = mean(vol_series[-3:]) if len(vol_series) >= 3 else vol_series[-1]
        rel_vol = cur_vol / avg_vol if avg_vol > 0 else 0.0

    # Session low and surge
    session_low = min(b["low"] for b in day_series)
    surge_from_low = (close - session_low) / session_low * 100

    # Pump conditions (multiple triggers)
    macd_pumping = hist_now > 0.15 and delta_3 > 0.03   # Strong positive + rising
    rsi_surge = rsi_prev < 45 and rsi_now > 60           # RSI surged through neutral
    sharp_surge = surge_from_low > 0.4 and close > vwap  # Up >0.4% from low + above VWAP
    volume_ok = rel_vol >= MIN_RELATIVE_VOLUME

    if (macd_pumping or rsi_surge) and sharp_surge and volume_ok:
        e9, e21 = ema(series, 9), ema(series, 21)
        st = supertrend_direction(day_series[-35:] if len(day_series) >= 35 else day_series)
        momentum = (close / closes[-4] - 1) * 100 if len(closes) >= 4 else 0.0
        turtle = turtle_intraday_context(day_series)

        bull_score = sum([
            e9 > e21,           # ema_trend
            close > vwap,       # vwap
            st == "up",         # supertrend
            hist_now > 0,       # V7.4.12: actual MACD check (was hardcoded True)
            rsi_now > 50,       # V7.4.12: actual RSI check (was hardcoded True)
            momentum > 0.04,   # V7.4.12: kept at 0.04 — raising to 0.06 would reject the +$32 winner and miss early trends
            volume_ok,          # relative_volume
            turtle["longBreakout"],  # V7.4.8: actual breakout
        ])
        return {
            "direction": "bullish",
            "score": bull_score,
            "independentScore": bull_score,
            "qualificationScore": bull_score,
            "substitutionFlags": [],
            "close": close, "ema9": e9, "ema21": e21,
            "vwap": vwap, "vwapUpper": upper, "vwapLower": lower,
            "rsi": rsi_now, "macdHist": hist_now,
            "supertrend": st, "momentumPct": momentum,
            "relativeVolume": rel_vol, "bullScore": bull_score, "bearScore": 0,
            "rawDirection": "bullish",
            "rejectionReasons": [],
            "bullChecks": {"ema_trend": e9 > e21, "vwap": True, "supertrend": st == "up",
                           "macd": hist_now > 0, "rsi": rsi_now > 50, "momentum": momentum > 0.04,
                           "relative_volume": volume_ok, "turtle_breakout": turtle["longBreakout"]},
            "bearChecks": {"ema_trend": e9 < e21, "vwap": False, "supertrend": st == "up",
                           "macd": hist_now > 0, "rsi": rsi_now > 50, "momentum": momentum > 0.04,
                           "relative_volume": volume_ok, "turtle_breakout": turtle["shortBreakout"]},
            "turtle": turtle,
            "pump_detector": True,
            "macd_delta_3": round(delta_3, 4),
            "macd_hist": round(hist_now, 4),
            "rsi_surge": f"{rsi_prev:.0f}->{rsi_now:.0f}",
            "surge_from_low": round(surge_from_low, 2),
        }
    return None


def opening_range_context(day: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a deterministic first-30-minute opening-range snapshot.

    This is deliberately separate from ``orb_signal``. The legacy standalone
    detector remains disabled, while the trend-confluence path uses this
    context only after EMA, MACD, volume, momentum, VWAP, RSI, and direction-edge
    checks agree. A close must clear the range by at least 0.10 percent; a wick
    or a touch is not a breakout.
    """
    empty = {
        "ready": False,
        "orbHigh": None,
        "orbLow": None,
        "rangePct": 0.0,
        "bullBreakout": False,
        "bearBreakout": False,
        "breakoutPct": 0.0,
    }
    if len(day) < 7:
        return empty
    try:
        opening = day[:6]
        orb_high = max(float(bar["high"]) for bar in opening)
        orb_low = min(float(bar["low"]) for bar in opening)
        first_close = float(opening[0]["close"])
        current_close = float(day[-1]["close"])
    except (KeyError, TypeError, ValueError):
        return empty
    if (
        not all(math.isfinite(value) for value in (orb_high, orb_low, first_close, current_close))
        or first_close <= 0
        or orb_high <= orb_low
    ):
        return empty
    range_pct = (orb_high - orb_low) / first_close * 100.0
    bull_breakout = (
        range_pct >= TREND_ORB_MIN_RANGE_PCT
        and current_close > orb_high * (1.0 + TREND_ORB_BREAKOUT_PCT / 100.0)
    )
    bear_breakout = (
        range_pct >= TREND_ORB_MIN_RANGE_PCT
        and current_close < orb_low * (1.0 - TREND_ORB_BREAKOUT_PCT / 100.0)
    )
    if bull_breakout:
        breakout_pct = (current_close / orb_high - 1.0) * 100.0
    elif bear_breakout:
        breakout_pct = (1.0 - current_close / orb_low) * 100.0
    else:
        breakout_pct = 0.0
    return {
        "ready": True,
        "orbHigh": orb_high,
        "orbLow": orb_low,
        "rangePct": range_pct,
        "bullBreakout": bull_breakout,
        "bearBreakout": bear_breakout,
        "breakoutPct": breakout_pct,
    }



def orb_signal(day: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Opening Range Breakout detector.

    Defines the ORB range from the first 30 minutes (6 bars) of the session.
    Triggers when price breaks above ORB high (bullish) or below ORB low (bearish)
    with conviction: volume, momentum, and VWAP alignment.

    Rules:
    - ORB range = first 30 min high/low
    - Breakout must close beyond ORB (not just wick)
    - Volume must be above average
    - Price must be on the correct side of VWAP
    - Momentum must confirm (3-bar % move)
    - RSI must not be overextended (bull: 30-75, bear: 25-70)
    - Avoid ORB on inside days (range < 0.15% = skip)

    Returns a signal dict like pump_signal/crash_signal, or None.
    """
    if len(day) < 12:
        return None

    # ORB range from first 30 min (6 bars)
    orb_bars = day[:6]
    orb_high = max(b["high"] for b in orb_bars)
    orb_low = min(b["low"] for b in orb_bars)
    orb_range = orb_high - orb_low
    orb_mid = (orb_high + orb_low) / 2
    orb_pct = orb_range / orb_bars[0]["close"] * 100

    # Skip inside days (range too tight)
    if orb_pct < 0.15:
        return None

    closes = [bar["close"] for bar in day]
    current_close = closes[-1]
    volumes = [bar["volume"] for bar in day]

    # Volume check
    avg_vol = mean(volumes[:-3]) if len(volumes) > 3 else mean(volumes)
    recent_vol = mean(volumes[-3:]) if len(volumes) >= 3 else volumes[-1]
    rel_vol = recent_vol / avg_vol if avg_vol > 0 else 0.0

    # VWAP check
    vwap, upper, lower = vwap_bands(day)

    # Momentum (3-bar %)
    momentum = (current_close / closes[-4] - 1) * 100 if len(closes) >= 4 else 0.0

    # Indicators
    current_rsi = rsi(closes) if len(closes) >= 14 else 50.0
    hist = macd_hist(closes) if len(closes) >= 35 else 0.0
    e9 = ema(closes, 9) if len(closes) >= 21 else closes[-1]
    e21 = ema(closes, 21) if len(closes) >= 21 else closes[-1]
    st = supertrend_direction(day[-35:]) if len(day) >= 35 else "neutral"

    # Determine breakout direction
    # Bullish: close above ORB high
    if current_close > orb_high:
        # Must be above VWAP, volume above avg, positive momentum, not overbought
        bull_score = sum([
            e9 > e21,                        # ema_trend
            current_close > vwap,             # vwap
            st == "up",                       # supertrend
            hist > 0,                         # macd
            30 <= current_rsi <= 75,          # rsi (not overbought)
            momentum > 0.04,                  # momentum
            rel_vol >= MIN_RELATIVE_VOLUME,                   # relative_volume
            current_close > orb_high * 1.001, # 0.1% beyond ORB (not just touching)
        ])

        if bull_score >= MIN_ENTRY_SCORE:
            return {
                "direction": "bullish",
                "score": bull_score,
                "close": current_close, "ema9": e9, "ema21": e21,
                "vwap": vwap, "vwapUpper": upper, "vwapLower": lower,
                "rsi": current_rsi, "macdHist": hist,
                "supertrend": st, "momentumPct": momentum,
                "relativeVolume": rel_vol,
                "bullScore": bull_score, "bearScore": 0,
                "rawDirection": "bullish",
                "rejectionReasons": [],
                "bullChecks": {
                    "ema_trend": e9 > e21, "vwap": current_close > vwap,
                    "supertrend": st == "up", "macd": hist > 0,
                    "rsi": 30 <= current_rsi <= 75,
                    "momentum": momentum > 0.04,
                    "relative_volume": rel_vol >= MIN_RELATIVE_VOLUME,
                    "orb_breakout": current_close > orb_high * 1.001,
                },
                "bearChecks": {
                    "ema_trend": e9 < e21, "vwap": current_close < vwap,
                    "supertrend": st == "down", "macd": hist < 0,
                    "rsi": 25 <= current_rsi <= 70,
                    "momentum": momentum < -0.04,
                    "relative_volume": rel_vol >= MIN_RELATIVE_VOLUME,
                    "orb_breakout": False,
                },
                "turtle": turtle_intraday_context(day),
                "orb_detector": True,
                "orb_high": round(orb_high, 2),
                "orb_low": round(orb_low, 2),
                "orb_range_pct": round(orb_pct, 2),
                "breakout_pct": round((current_close / orb_high - 1) * 100, 2),
            }

    # Bearish: close below ORB low
    if current_close < orb_low:
        bear_score = sum([
            e9 < e21,                        # ema_trend
            current_close < vwap,             # vwap
            st == "down",                    # supertrend
            hist < 0,                         # macd
            25 <= current_rsi <= 70,          # rsi (not oversold)
            momentum < -0.04,                 # momentum
            rel_vol >= MIN_RELATIVE_VOLUME,                   # relative_volume
            current_close < orb_low * 0.999,  # 0.1% beyond ORB (not just touching)
        ])

        if bear_score >= MIN_ENTRY_SCORE:
            return {
                "direction": "bearish",
                "score": bear_score,
                "close": current_close, "ema9": e9, "ema21": e21,
                "vwap": vwap, "vwapUpper": upper, "vwapLower": lower,
                "rsi": current_rsi, "macdHist": hist,
                "supertrend": st, "momentumPct": momentum,
                "relativeVolume": rel_vol,
                "bullScore": 0, "bearScore": bear_score,
                "rawDirection": "bearish",
                "rejectionReasons": [],
                "bullChecks": {
                    "ema_trend": e9 > e21, "vwap": current_close > vwap,
                    "supertrend": st == "up", "macd": hist > 0,
                    "rsi": 30 <= current_rsi <= 75,
                    "momentum": momentum > 0.04,
                    "relative_volume": rel_vol >= MIN_RELATIVE_VOLUME,
                    "orb_breakout": False,
                },
                "bearChecks": {
                    "ema_trend": e9 < e21, "vwap": current_close < vwap,
                    "supertrend": st == "down", "macd": hist < 0,
                    "rsi": 25 <= current_rsi <= 70,
                    "momentum": momentum < -0.04,
                    "relative_volume": rel_vol >= MIN_RELATIVE_VOLUME,
                    "orb_breakout": current_close < orb_low * 0.999,
                },
                "turtle": turtle_intraday_context(day),
                "orb_detector": True,
                "orb_high": round(orb_high, 2),
                "orb_low": round(orb_low, 2),
                "orb_range_pct": round(orb_pct, 2),
                "breakout_pct": round((1 - current_close / orb_low) * 100, 2),
            }

    return None


def qualify_signal_direction(
    *,
    bull_score: int,
    bear_score: int,
    close: float,
    vwap: float,
    upper: float,
    lower: float,
    dominant_failed_checks: list[str] | None = None,
    hard_gate_failures: list[str] | None = None,
    current_rsi: float | None = None,
    min_edge: int = 1,
    last_bar_close: float | None = None,
    prev_bar_close: float | None = None,
    min_trigger_move: float = 0.0,
    skip_fresh_trigger: bool = False,
    skip_anti_chase: bool = False,
    skip_rsi_extreme: bool = False,
    allow_rsi_extreme_on_breakout: bool = False,
    min_entry_score: int = MIN_ENTRY_SCORE,
) -> tuple[str | None, str | None, list[str]]:
    """Return final direction plus explicit reasons when qualification fails.

    V7.4.1 additions:
    - min_edge: bull_score must exceed bear_score by >= min_edge (and vice versa).
      Prevents entering on ambiguous signals where both sides are moderately strong.
    - Fresh trigger: last completed bar must confirm the direction (higher close for
      bullish, lower close for bearish). Prevents entering on stale trend.
    """
    raw_direction = (
        "bullish" if bull_score >= min_entry_score and bull_score - bear_score >= min_edge
        else "bearish" if bear_score >= min_entry_score and bear_score - bull_score >= min_edge
        else None
    )
    direction = raw_direction
    reasons: list[str] = []
    if raw_direction is None:
        if max(bull_score, bear_score) < min_entry_score:
            reasons.append(f"score_below_{min_entry_score}")
        elif abs(bull_score - bear_score) < min_edge:
            reasons.append(f"direction_edge_below_{min_edge}")
        else:
            reasons.append("direction_scores_tied")
    elif hard_gate_failures:
        direction = None
        reasons.extend(f"hard_gate:{failure}" for failure in hard_gate_failures)
    elif (raw_direction == "bullish" and not skip_anti_chase
          and close > vwap + BULLISH_ANTI_CHASE_SIGMA * (upper - vwap)):
        direction = None
        reasons.append(f"bullish_chase_above_{BULLISH_ANTI_CHASE_SIGMA:.1f}_vwap_sigma")
    elif (raw_direction == "bearish" and not skip_anti_chase
          and close < vwap - BEARISH_ANTI_CHASE_SIGMA * (vwap - lower)):
        direction = None
        reasons.append(f"bearish_chase_below_{BEARISH_ANTI_CHASE_SIGMA:.1f}_vwap_sigma")
    # RSI extreme block: reject overbought bullish / oversold bearish entries.
    # Crash/pump detectors bypass this — their own MACD/volume/momentum gates
    # confirm the move is live, and RSI extremes are exactly when crashes/pumps happen.
    if not skip_rsi_extreme and not allow_rsi_extreme_on_breakout:
        if direction == "bullish" and current_rsi is not None and current_rsi > RSI_OVERBOUGHT:
            direction = None
            reasons.append(f"rsi_overbought_{current_rsi:.0f}")
        elif direction == "bearish" and current_rsi is not None and current_rsi < RSI_OVERSOLD:
            direction = None
            reasons.append(f"rsi_oversold_{current_rsi:.0f}")
    # Fresh trigger: last bar must confirm direction with >= min_trigger_move (ATR-based).
    # This prevents entering on stale trends or 1-cent noise.
    # Crash/pump detectors skip this (their conditions already confirm the move is live).
    if direction == "bullish" and not skip_fresh_trigger:
        if last_bar_close is None or prev_bar_close is None:
            direction = None
            reasons.append("missing_fresh_bullish_trigger")
        elif last_bar_close - prev_bar_close < min_trigger_move:
            direction = None
            reasons.append("insufficient_bullish_trigger")
    elif direction == "bearish" and not skip_fresh_trigger:
        if last_bar_close is None or prev_bar_close is None:
            direction = None
            reasons.append("missing_fresh_bearish_trigger")
        elif prev_bar_close - last_bar_close < min_trigger_move:
            direction = None
            reasons.append("insufficient_bearish_trigger")
    if direction is None and dominant_failed_checks:
        reasons.append("failed:" + ",".join(dominant_failed_checks))
    return direction, raw_direction, reasons


def opening_breakout_extension_failure(
    setup: dict[str, Any], live_price: float,
) -> str | None:
    """Fail closed when an opening target is lost or the move is already chased."""
    if setup.get("openingContinuation") is not True:
        return None
    direction = setup.get("rawDirection") or setup.get("direction")
    target = setup.get("premarketBreakoutTarget")
    opening_atr = setup.get("openingAtr")
    if (
        direction not in {"bullish", "bearish"}
        or not isinstance(target, (int, float))
        or not isinstance(opening_atr, (int, float))
        or isinstance(target, bool)
        or isinstance(opening_atr, bool)
        or not math.isfinite(float(target))
        or not math.isfinite(float(opening_atr))
        or float(target) <= 0
        or float(opening_atr) <= 0
        or not isinstance(live_price, (int, float))
        or isinstance(live_price, bool)
        or not math.isfinite(float(live_price))
        or float(live_price) <= 0
    ):
        return "opening_breakout_context_invalid"
    price = float(live_price)
    target_value = float(target)
    if direction == "bullish":
        if price <= target_value:
            return "opening_breakout_lost"
        extension = price - target_value
    else:
        if price >= target_value:
            return "opening_breakout_lost"
        extension = target_value - price
    max_extension = OPENING_MAX_BREAKOUT_EXTENSION_ATR * float(opening_atr)
    if extension > max_extension + 1e-9:
        return "opening_breakout_already_extended"
    return None


def signal(
    day: list[dict[str, Any]],
    *,
    warmup_bars: list[dict[str, Any]] | None = None,
    relative_volume: float | None = None,
    premarket_context: dict[str, Any] | None = None,
    symbol: str = SYMBOL,
) -> dict[str, Any]:
    """Score a regular-session setup with prior RTH bars used only for warmup.

    During the first six completed bars, a confirmed break of the premarket high
    or low may be recorded as opening context, but it never substitutes for the
    actual intraday Turtle breakout or adds a score point. Session VWAP and volume
    remain regular-session-only, and the honest score gate remains structural.
    """
    if not day:
        return {
            "direction": None, "score": 0, "why": ["insufficient_bars"],
            "rejectionReasons": ["insufficient_bars"],
            "sessionBarsUsed": 0, "warmupBarsUsed": 0,
        }
    if is_index_option_symbol(symbol):
        session_date = xsp_bar_session_key(day[-1]) or trading_session_key(symbol)
        session = [bar for bar in day if xsp_bar_session_key(bar) == session_date]
    else:
        session_date = str(day[-1].get("date", ""))
        session = [
            bar for bar in day
            if str(bar.get("date", "")) == session_date
            and MARKET_OPEN <= int(bar.get("time", -1)) <= 1555
        ]
    if not session:
        return {
            "direction": None, "score": 0, "why": ["insufficient_bars"],
            "rejectionReasons": ["insufficient_bars"],
            "sessionBarsUsed": 0, "warmupBarsUsed": 0,
        }
    warmup: list[dict[str, Any]] = []
    if len(session) < 35:
        if is_index_option_symbol(symbol):
            warmup = [
                bar for bar in (warmup_bars or [])
                if (key := xsp_bar_session_key(bar)) is not None and key < session_date
            ][-OPENING_WARMUP_BARS:]
        else:
            warmup = [
                bar for bar in (warmup_bars or [])
                if str(bar.get("date", "")) < session_date
                and MARKET_OPEN <= int(bar.get("time", -1)) <= 1555
            ][-OPENING_WARMUP_BARS:]
    indicator_bars = warmup + session
    if len(indicator_bars) < 35:
        return {
            "direction": None, "score": 0, "why": ["insufficient_bars"],
            "rejectionReasons": ["insufficient_bars"],
            "sessionBarsUsed": len(session), "warmupBarsUsed": len(warmup),
        }

    closes = [float(bar["close"]) for bar in indicator_bars]
    session_closes = [float(bar["close"]) for bar in session]
    close = session_closes[-1]
    e9, e21 = ema(closes, 9), ema(closes, 21)
    # Premarket and prior-session bars never contaminate the current-session VWAP.
    rvwap, upper, lower = vwap_bands(session)
    current_rsi = rsi(closes)
    hist = macd_hist(closes)
    st = supertrend_direction(indicator_bars[-35:])
    momentum_reference = (
        session_closes[-4] if len(session_closes) >= 4
        else float(session[0]["open"])
    )
    momentum = (close / momentum_reference - 1) * 100 if momentum_reference else 0.0
    if (
        isinstance(relative_volume, (int, float))
        and not isinstance(relative_volume, bool)
        and math.isfinite(float(relative_volume))
        and float(relative_volume) >= 0
    ):
        rel_vol = float(relative_volume)
    else:
        baseline = [float(bar["volume"]) for bar in session[-25:-5]]
        avg_vol = mean(baseline) if baseline else 0.0
        rel_vol = mean(float(bar["volume"]) for bar in session[-3:]) / avg_vol if avg_vol else 0.0

    # Turtle channels describe today's intraday structure only. Prior-session
    # bars may warm lagging indicators but must never define a session breakout.
    turtle = turtle_intraday_context(session)
    orb_context = opening_range_context(session)
    opening_window = (not is_index_option_symbol(symbol)) and len(session) <= 6
    context_ready = (
        opening_window
        and isinstance(premarket_context, dict)
        and premarket_context.get("ready") is True
        and premarket_context.get("sessionDate") == session_date
    )
    premarket_high = premarket_context.get("premarketHigh") if context_ready else None
    premarket_low = premarket_context.get("premarketLow") if context_ready else None
    bullish_premarket_breakout = (
        isinstance(premarket_high, (int, float)) and close > float(premarket_high)
    )
    bearish_premarket_breakout = (
        isinstance(premarket_low, (int, float)) and close < float(premarket_low)
    )

    # --- V7.4.4: ATR-normalized thresholds ---
    # V7.4.10: restore the V7.4.3 fixed ±0.04% momentum floor for equities.
    # The 2.5 × ATR multiplier made a normal SPY move (for example -0.07%)
    # fail momentum when ATR was elevated, leaving SPY at score 5–6 all day.
    # Keep XSP's ATR-normalized profile separate because its index feed has a
    # different price scale and overnight session behavior.
    current_atr_value = atr(indicator_bars[-35:], 10)
    atr_price = current_atr_value if current_atr_value > 0 else 0.0
    underlying_price = close if close > 0 else 1.0
    atr_pct = (atr_price / underlying_price * 100) if underlying_price > 0 else 0.2
    opening_mult = 1.5 if (not is_index_option_symbol(symbol) and len(session) <= 6) else 1.0
    mom_mult = MOMENTUM_ATR_FRACTION * opening_mult
    is_opening = (not is_index_option_symbol(symbol) and len(session) <= 6)
    if is_index_option_symbol(symbol):
        momentum_threshold = 0.04 if is_opening else max(mom_mult * atr_pct, 0.04)
    else:
        momentum_threshold = 0.04
    # VWAP no-conviction: FIXED percent floor (not ATR-inflated).
    # Equities 0.05%, index options 0.01%. ATR scaling previously pushed the
    # effective floor to ~0.12–0.20% and rejected most real setups.
    vwap_dev_threshold = (
        MIN_VWAP_DEVIATION_PCT_INDEX if is_index_option_symbol(symbol)
        else MIN_VWAP_DEVIATION_PCT
    )

    # V7.4.8/V7.4.10: keep structural Turtle evidence honest. Opening premarket
    # breakout is a qualification path, not proof that today's Turtle channel broke.
    # V7.5.1: squeeze remains scored; momentum acceleration is telemetry only.
    bb_squeeze_bull = False
    bb_squeeze_bear = False
    mom_accel_bull = False
    mom_accel_bear = False
    if SQUEEZE_BREAKOUT_ENABLED and len(closes) >= 20:
        bb_mid = sum(closes[-10:]) / 10
        bb_std = (sum((c - bb_mid) ** 2 for c in closes[-10:]) / 10) ** 0.5
        bb_upper = bb_mid + 2 * bb_std
        bb_lower = bb_mid - 2 * bb_std
        bb_bandwidth = (bb_upper - bb_lower) / bb_mid if bb_mid > 0 else 0
        prior_slice = closes[-20:-10]
        prior_mid = sum(prior_slice) / 10
        prior_std = (sum((c - prior_mid) ** 2 for c in prior_slice) / 10) ** 0.5
        prior_bandwidth = (4 * prior_std) / prior_mid if prior_mid > 0 else 0
        is_squeeze = bb_bandwidth < prior_bandwidth * 0.8 and prior_bandwidth > 0
        bb_squeeze_bull = is_squeeze and close > bb_upper and rel_vol >= MIN_RELATIVE_VOLUME
        bb_squeeze_bear = is_squeeze and close < bb_lower and rel_vol >= MIN_RELATIVE_VOLUME
        short_roc = (close / closes[-3] - 1) * 100 if closes[-3] > 0 else 0
        long_roc = (close / closes[-7] - 1) * 100 if closes[-7] > 0 else 0
        mom_accel_bull = short_roc > long_roc and short_roc > 0
        mom_accel_bear = short_roc < long_roc and short_roc < 0

    bull_checks = {"ema_trend": e9 > e21, "vwap": close > rvwap, "supertrend": st == "up",
                   "macd": hist > 0, "rsi": 25 <= current_rsi <= 78,
                   "momentum": momentum >= momentum_threshold, "relative_volume": rel_vol >= MIN_RELATIVE_VOLUME,
                   "turtle_breakout": turtle["longBreakout"],
                   "squeeze_breakout": bb_squeeze_bull, "momentum_acceleration": mom_accel_bull}
    bear_checks = {"ema_trend": e9 < e21, "vwap": close < rvwap, "supertrend": st == "down",
                   "macd": hist < 0, "rsi": 22 <= current_rsi <= 75,
                   "momentum": momentum <= -momentum_threshold, "relative_volume": rel_vol >= MIN_RELATIVE_VOLUME,
                   "turtle_breakout": turtle["shortBreakout"],
                   "squeeze_breakout": bb_squeeze_bear, "momentum_acceleration": mom_accel_bear}
    # V7.4.8: High-volume confirmation is pure telemetry — records what V7.4.5
    # would have substituted, but does NOT modify the checks dict.
    bearish_high_volume_confirmation = (
        (not opening_window or bearish_premarket_breakout)
        and rel_vol >= BEARISH_HIGH_VOLUME_REL_VOL
        and not bear_checks["momentum"]
        and _signal_score(bear_checks) >= (MIN_ENTRY_SCORE - 1)
    )
    bullish_high_volume_confirmation = (
        (not opening_window or bullish_premarket_breakout)
        and rel_vol >= BULLISH_HIGH_VOLUME_REL_VOL
        and not bull_checks["momentum"]
        and _signal_score(bull_checks) >= (MIN_ENTRY_SCORE - 1)
    )
    substitution_flags = []
    if bearish_high_volume_confirmation:
        substitution_flags.append("bearish_volume_as_momentum")
    if bullish_high_volume_confirmation:
        substitution_flags.append("bullish_volume_as_momentum")
    bull_score = _signal_score(bull_checks)
    bear_score = _signal_score(bear_checks)
    qualification_flags = []
    qualification_bull_score = bull_score
    qualification_bear_score = bear_score
    if opening_window and bullish_premarket_breakout:
        qualification_flags.append("premarket_breakout_context")
    if opening_window and bearish_premarket_breakout:
        qualification_flags.append("premarket_breakout_context")

    # Compute VWAP deviation for no-conviction gate
    vwap_pct_abs = abs((close - rvwap) / rvwap * 100) if rvwap > 0 else 99.0
    dominant_direction = "bullish" if qualification_bull_score >= qualification_bear_score else "bearish"
    dominant_checks = bull_checks if dominant_direction == "bullish" else bear_checks
    failed_checks = [name for name, passed in dominant_checks.items() if not passed]
    hard_gate_failures = (
        [f"relative_volume_below_{MIN_RELATIVE_VOLUME:.2f}"]
        if rel_vol < MIN_RELATIVE_VOLUME else []
    )
    # VWAP no-conviction gate: reject entries sitting flat on VWAP.
    # Data: VWAP% -0.05 to +0.05 → 20% WR, -$40 over 5 trades.
    # Fixed % floor only (ATR inflation disabled — it blocked 71% of valid deviations).
    if vwap_pct_abs < vwap_dev_threshold:
        hard_gate_failures.append("vwap_no_conviction")
    # V7.4.4: Evidence category gate — no category can be zero
    dominant_direction = "bullish" if qualification_bull_score >= qualification_bear_score else "bearish"
    dominant_checks_for_evidence = bull_checks if dominant_direction == "bullish" else bear_checks
    evidence = evidence_categories(dominant_checks_for_evidence, dominant_direction)
    evidence_ok, evidence_reasons = evidence_qualified(evidence)
    if not evidence_ok:
        hard_gate_failures.extend(evidence_reasons)
    if opening_window:
        if not context_ready:
            hard_gate_failures.append("premarket_context_unavailable")
        elif qualification_bull_score >= MIN_ENTRY_SCORE and qualification_bull_score - qualification_bear_score >= MIN_DIRECTION_EDGE:
            if not bullish_premarket_breakout:
                hard_gate_failures.append("premarket_target_not_broken")
        elif qualification_bear_score >= MIN_ENTRY_SCORE and qualification_bear_score - qualification_bull_score >= MIN_DIRECTION_EDGE:
            if not bearish_premarket_breakout:
                hard_gate_failures.append("premarket_target_not_broken")

    opening_direction = (
        "bullish" if bullish_premarket_breakout and qualification_bull_score >= qualification_bear_score
        else "bearish" if bearish_premarket_breakout and qualification_bear_score > qualification_bull_score
        else None
    )
    opening_atr_source = warmup[-OPENING_WARMUP_BARS:] if warmup else session
    opening_atr = atr(opening_atr_source, 10)
    opening_target = (
        float(premarket_high) if opening_direction == "bullish"
        else float(premarket_low) if opening_direction == "bearish"
        else None
    )
    opening_continuation = opening_direction is not None
    if opening_continuation:
        extension_failure = opening_breakout_extension_failure({
            "openingContinuation": True,
            "rawDirection": opening_direction,
            "premarketBreakoutTarget": opening_target,
            "openingAtr": opening_atr,
        }, close)
        if extension_failure:
            hard_gate_failures.append(extension_failure)

    # The first completed bar confirms against its open. Later bars confirm
    # against the immediately preceding completed regular-session close.
    last_bar_close = close
    prev_bar_close = (
        float(session[-2]["close"]) if len(session) >= 2
        else float(session[-1]["open"])
    )
    current_atr = atr(opening_atr_source, 10)
    min_move = max(0.05 * current_atr, 0.02) if current_atr > 0 else 0.02

    # Late-entry score bump: after 14:30 ET, require a higher score.
    # Data: 15:00 ET entries → 17% WR, -$16 over 6 trades.
    # These low-conviction late entries consistently lose.
    last_bar_time = int(session[-1].get("time", 0)) if session else 0
    late_entry = last_bar_time >= 1430
    effective_min_score = MIN_ENTRY_SCORE
    if late_entry:
        effective_min_score = MIN_ENTRY_SCORE + LATE_ENTRY_SCORE_BUMP
        # Apply the bump by adding a hard gate if score doesn't meet the raised bar
        if max(qualification_bull_score, qualification_bear_score) < effective_min_score:
            hard_gate_failures.append(f"late_entry_score_below_{effective_min_score}")

    candidate_direction = (
        "bullish"
        if qualification_bull_score >= effective_min_score
        and qualification_bull_score - qualification_bear_score >= MIN_DIRECTION_EDGE
        else "bearish"
        if qualification_bear_score >= effective_min_score
        and qualification_bear_score - qualification_bull_score >= MIN_DIRECTION_EDGE
        else None
    )
    allow_rsi_extreme_on_breakout = (
        (candidate_direction == "bullish" and (turtle["longBreakout"] or bb_squeeze_bull))
        or (candidate_direction == "bearish" and (turtle["shortBreakout"] or bb_squeeze_bear))
    )

    direction, raw_direction, rejection_reasons = qualify_signal_direction(
        bull_score=qualification_bull_score, bear_score=qualification_bear_score, close=close, vwap=rvwap,
        upper=upper, lower=lower, dominant_failed_checks=failed_checks,
        hard_gate_failures=hard_gate_failures,
        current_rsi=current_rsi, min_edge=MIN_DIRECTION_EDGE,
        min_entry_score=effective_min_score,
        allow_rsi_extreme_on_breakout=allow_rsi_extreme_on_breakout,
        last_bar_close=last_bar_close,
        prev_bar_close=prev_bar_close,
        min_trigger_move=min_move,
    )
    return {"direction": direction, "score": max(qualification_bull_score, qualification_bear_score),
            "independentScore": max(bull_score, bear_score),
            "qualificationScore": max(qualification_bull_score, qualification_bear_score),
            "independentBullScore": bull_score, "independentBearScore": bear_score,
            "qualificationBullScore": qualification_bull_score,
            "qualificationBearScore": qualification_bear_score,
            "qualificationFlags": qualification_flags,
            "substitutionFlags": substitution_flags,
            "close": close,
            "ema9": e9, "ema21": e21, "vwap": rvwap, "vwapUpper": upper, "vwapLower": lower,
            "rsi": current_rsi, "macdHist": hist, "supertrend": st, "momentumPct": momentum,
            "relativeVolume": rel_vol, "bullScore": qualification_bull_score, "bearScore": qualification_bear_score,
            "rawDirection": raw_direction, "rejectionReasons": rejection_reasons,
            "minTriggerMove": min_move,
            "bullChecks": bull_checks, "bearChecks": bear_checks,
            "bearishHighVolumeConfirmation": bearish_high_volume_confirmation,
            "bullishHighVolumeConfirmation": bullish_high_volume_confirmation,
            "evidence": evidence,
            "turtle": turtle,
            "orbContext": orb_context,
            "sessionBarsUsed": len(session), "warmupBarsUsed": len(warmup),
            "premarketBreakout": opening_continuation,
            "openingContinuation": opening_continuation,
            "premarketBreakoutTarget": opening_target,
            "openingAtr": opening_atr,
            "premarketContext": premarket_context if context_ready else None}


def apply_trend_confluence_gate(
    setup: dict[str, Any], *, symbol: str = SYMBOL, bar_time: Any = None,
) -> dict[str, Any]:
    """Promote only a strict ORB-backed multi-indicator trend continuation.

    The normal V7 score floor and structural Turtle/squeeze paths are unchanged.
    This separate path exists for a setup that has a real opening-range close and
    independent EMA, MACD, volume, and direction-edge agreement, plus at least
    two of momentum, VWAP, and RSI. It uses a score floor of six only because the
    ORB close is an additional structural requirement and is intentionally not
    counted in the normal score. Late-entry and consecutive-loss floors still
    apply elsewhere.
    """
    result = dict(setup)
    if not TREND_ORB_CONFLUENCE_ENABLED or not isinstance(setup, dict):
        return result
    orb = setup.get("orbContext")
    if not isinstance(orb, dict) or orb.get("ready") is not True:
        return result

    bull_checks = setup.get("bullChecks")
    bear_checks = setup.get("bearChecks")
    if not isinstance(bull_checks, dict) or not isinstance(bear_checks, dict):
        return result

    def score(name: str, fallback: str) -> float:
        value = setup.get(name, setup.get(fallback, 0))
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) else 0.0

    bull_score = score("qualificationBullScore", "bullScore")
    bear_score = score("qualificationBearScore", "bearScore")
    candidate_direction = (
        "bullish"
        if bull_score >= TREND_CONTINUATION_MIN_SCORE
        and bull_score - bear_score >= MIN_DIRECTION_EDGE
        else "bearish"
        if bear_score >= TREND_CONTINUATION_MIN_SCORE
        and bear_score - bull_score >= MIN_DIRECTION_EDGE
        else None
    )
    if candidate_direction is None:
        return result
    if setup.get("direction") not in {None, candidate_direction}:
        return result

    checks = bull_checks if candidate_direction == "bullish" else bear_checks
    # V7.5.3 (operator-approved 2026-10-12): directional momentum is now a
    # core requirement, not an optional supporting check. Exact-release paper
    # evidence 2026-09-28..2026-10-12: momentum-confirmed ORB entries went
    # 7W/5L for +$85 (PF 2.27); momentum-failed ORB entries went 3W/7L for
    # -$51 (PF 0.45), including all four 2026-10-12 QQQ losses.
    required_checks = ("ema_trend", "macd", "relative_volume", "momentum")
    if not all(checks.get(name) is True for name in required_checks):
        return result
    supporting_checks = ("momentum", "vwap", "rsi")
    if sum(checks.get(name) is True for name in supporting_checks) < 2:
        return result
    orb_key = "bullBreakout" if candidate_direction == "bullish" else "bearBreakout"
    if orb.get(orb_key) is not True:
        return result
    rsi_value = setup.get("rsi")
    if (
        not isinstance(rsi_value, (int, float))
        or isinstance(rsi_value, bool)
        or not math.isfinite(float(rsi_value))
        or (candidate_direction == "bullish" and float(rsi_value) > RSI_OVERBOUGHT)
        or (candidate_direction == "bearish" and float(rsi_value) < RSI_OVERSOLD)
    ):
        return result
    safety_prefixes = (
        "bullish_chase_", "bearish_chase_", "missing_", "insufficient_",
        "vwap_no_conviction", "relative_volume_below_", "premarket_target_not_broken",
        "opening_breakout_", "late_entry_score_below_",
    )
    if any(
        isinstance(reason, str) and reason.startswith(safety_prefixes)
        for reason in (setup.get("rejectionReasons") or [])
    ):
        return result
    if (
        not isinstance(orb.get("rangePct"), (int, float))
        or float(orb["rangePct"]) < TREND_ORB_MIN_RANGE_PCT
        or not isinstance(orb.get("breakoutPct"), (int, float))
        or float(orb["breakoutPct"]) < TREND_ORB_BREAKOUT_PCT
    ):
        return result

    trend_time = _bar_time_hhmm(bar_time)
    if trend_time is None:
        for key in ("signalBarEnd", "signalBarStart", "barTime", "time"):
            trend_time = _bar_time_hhmm(setup.get(key))
            if trend_time is not None:
                break
    entry_floor = TREND_CONTINUATION_MIN_SCORE
    if trend_time is not None and trend_time >= 1430:
        entry_floor = entry_score_for_symbol(symbol) + LATE_ENTRY_SCORE_BUMP
    if candidate_direction == "bullish" and bull_score < entry_floor:
        return result
    if candidate_direction == "bearish" and bear_score < entry_floor:
        return result

    result["baseRejectionReasons"] = list(setup.get("rejectionReasons") or [])
    result["direction"] = candidate_direction
    result["rawDirection"] = candidate_direction
    result["trendDirection"] = candidate_direction
    result["trendConfluence"] = True
    result["entryStructure"] = "orb_trend_confluence"
    result["orbBreakout"] = True
    result["trendScore"] = int(round(bull_score if candidate_direction == "bullish" else bear_score))
    result["trendEntryScoreFloor"] = int(entry_floor)
    result["trendComponents"] = {
        "ema": checks.get("ema_trend") is True,
        "macd": checks.get("macd") is True,
        "relativeVolume": checks.get("relative_volume") is True,
        "momentum": checks.get("momentum") is True,
        "vwap": checks.get("vwap") is True,
        "rsi": checks.get("rsi") is True,
        "orb": True,
    }
    result["trendSupportingChecks"] = [
        name for name in supporting_checks if checks.get(name) is True
    ]
    result["rejectionReasons"] = []
    return result



def detect_setup_for_recheck(
    day: list[dict[str, Any]],
    *,
    warmup_bars: list[dict[str, Any]] | None = None,
    relative_volume: float | None = None,
    premarket_context: dict[str, Any] | None = None,
    symbol: str = SYMBOL,
) -> dict[str, Any]:
    """Recompute the full setup without emitting duplicate detector events."""
    setup = signal(
        day,
        warmup_bars=warmup_bars,
        relative_volume=relative_volume,
        premarket_context=premarket_context,
        symbol=symbol,
    )
    setup = apply_trend_confluence_gate(
        setup,
        symbol=symbol,
        bar_time=(day[-1].get("time") if day else None),
    )
    detectors = [crash_signal, pump_signal]
    if ORB_ENABLED:
        detectors.append(orb_signal)
    for detector in detectors:
        if setup.get("direction"):
            break
        candidate = detector(day, symbol) if detector in {crash_signal, pump_signal} else detector(day)
        if not candidate:
            continue
        direction, raw_direction, reasons = qualify_signal_direction(
            bull_score=candidate.get("bullScore", 0),
            bear_score=candidate.get("bearScore", 0),
            close=candidate["close"],
            vwap=candidate["vwap"],
            upper=candidate["vwapUpper"],
            lower=candidate["vwapLower"],
            current_rsi=candidate["rsi"],
            min_edge=MIN_DIRECTION_EDGE,
            min_entry_score=effective_entry_score_floor(
                symbol, bar_time=(day[-1].get("time") if day else None), setup=candidate,
            ),
            skip_fresh_trigger=True,
            skip_anti_chase=True,
        )
        candidate["direction"] = direction
        candidate["rawDirection"] = raw_direction
        candidate["rejectionReasons"] = reasons
        if direction:
            setup = candidate
            break
    common_failure = post_quote_signal_failure(setup, setup, symbol=symbol)
    if common_failure:
        setup = dict(setup)
        setup["direction"] = None
        setup["rejectionReasons"] = list(setup.get("rejectionReasons", [])) + [common_failure]
    return setup


def bridge(*args: str) -> dict[str, Any]:
    """Call the Robinhood quote bridge with retry on transient failures."""
    max_attempts = 3
    # First attempt gets 120s; retries get 45s. Total worst case: 120+1+45+2+45 = 213s,
    # safely under the 300s daemon tick interval to avoid missed scan cycles.
    attempt_timeouts = [120, 45, 45]
    for attempt in range(max_attempts):
        try:
            proc = subprocess.run(
                [sys.executable, str(BRIDGE), *args],
                capture_output=True, text=True,
                timeout=attempt_timeouts[attempt], check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr[-800:] or proc.stdout[-800:])
            payload = json.loads(proc.stdout)
            if not payload.get("ok"):
                raise RuntimeError(payload.get("error", "unknown bridge failure"))
            data = dict(payload["data"])
            data["_snapshotPath"] = payload.get("snapshot")
            return data
        except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError) as exc:
            err_text = str(exc).lower()
            is_transient = any(kw in err_text for kw in (
                "timeout", "timed out", "connection", "econnrefused",
                "429", "rate limit", "temporarily", "503", "502", "500",
            ))
            if attempt < max_attempts - 1 and is_transient:
                time.sleep(2 ** attempt)  # 1s, 2s backoff
                continue
            raise


def refresh_option_quote(*args: str) -> dict[str, Any]:
    """Re-query an exit option through a fresh provider session when stale."""
    return bridge(*args)


def fetch_live_equity_quote(symbol: str = SYMBOL) -> dict[str, Any]:
    """Fetch a read-only underlying quote used only to confirm a completed-bar setup."""
    if not is_index_option_symbol(symbol):
        return bridge("--equity", symbol)
    signal_bars = fetch_bars(signal_feed_for_symbol(symbol), days=2)
    if not signal_bars:
        raise RuntimeError("xsp_signal_feed_unavailable")
    try:
        reference = float(signal_bars[-1]["close"]) * signal_price_scale_for_symbol(symbol)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("xsp_signal_feed_invalid") from exc
    if not math.isfinite(reference) or reference <= 0:
        raise RuntimeError("xsp_signal_feed_invalid")
    return bridge(
        "--index", symbol, "--underlying", str(round(reference, 6)),
        "--dte", str(OPTION_DTE_TARGET),
    )


def live_trigger_eligible(setup: dict[str, Any], symbol: str = SYMBOL) -> bool:
    if setup.get("direction") is not None:
        return False
    if setup.get("rawDirection") not in {"bullish", "bearish"}:
        return False
    if not _structural_breakout_qualified(setup):
        return False
    if setup.get("score", 0) < effective_entry_score_floor(symbol, setup=setup):
        return False
    relative_volume = setup.get("relativeVolume")
    if not isinstance(relative_volume, (int, float)) or relative_volume < MIN_RELATIVE_VOLUME:
        return False
    reasons = setup.get("rejectionReasons") or []
    # A live tick may repair only the fresh-trigger timing, never a persisted
    # hard gate or failed completed-bar momentum requirement.
    if any(
        isinstance(reason, str) and reason.startswith("hard_gate:")
        for reason in reasons
    ):
        return False
    if "momentum_not_confirmed" in reasons:
        return False
    return any(
        reason in {
            "missing_fresh_bullish_trigger", "insufficient_bullish_trigger",
            "missing_fresh_bearish_trigger", "insufficient_bearish_trigger",
        }
        for reason in reasons
    )


def apply_live_trigger_confirmation(
    setup: dict[str, Any],
    live_quote: dict[str, Any],
    *,
    now: datetime,
    symbol: str = SYMBOL,
) -> dict[str, Any]:
    """Confirm timing from a live quote without recomputing score or volume.

    The completed 5-minute bar remains the sole source of indicators, score,
    relative volume, and direction. A live midpoint may only satisfy the fresh
    directional-move gate for a setup that already passed those completed-bar
    requirements. This avoids waiting for the next bar without treating an
    unfinished bar as backtest evidence.
    """
    result = dict(setup)
    if not live_trigger_eligible(result, symbol):
        return result
    valid, reason = validate_quote_metadata(
        live_quote,
        expected_kind=underlying_quote_kind_for_symbol(symbol),
        expected_symbol=symbol,
        max_age_seconds=MAX_LIVE_EQUITY_QUOTE_AGE_SECONDS,
        now=now,
    )
    if not valid:
        result["liveTriggerReason"] = reason
        return result
    # Stale venue timestamp guard: reject if the venue has not updated its quote.
    venue_ts = live_quote.get("quote", {}).get("updated_at") or live_quote.get("timestamp")
    if isinstance(venue_ts, str) and venue_ts:
        last_ts = _LAST_LIVE_EQUITY_VENUE_TS.get(symbol)
        if last_ts and venue_ts == last_ts:
            result["liveTriggerReason"] = "stale_venue_timestamp"
            return result
        _LAST_LIVE_EQUITY_VENUE_TS[symbol] = venue_ts
    quote = live_quote.get("quote")
    if not isinstance(quote, dict):
        result["liveTriggerReason"] = "missing_live_equity_quote"
        return result
    try:
        bid = float(quote["bid_price"])
        ask = float(quote["ask_price"])
        completed_close = float(result["close"])
        min_move = float(result.get("minTriggerMove", 0.02))
    except (KeyError, TypeError, ValueError):
        result["liveTriggerReason"] = "invalid_live_equity_quote"
        return result
    if not all(math.isfinite(value) for value in (bid, ask, completed_close, min_move)):
        result["liveTriggerReason"] = "invalid_live_equity_quote"
        return result
    if bid <= 0 or ask < bid or min_move < 0:
        result["liveTriggerReason"] = "invalid_live_equity_quote"
        return result
    live_mid = (bid + ask) / 2.0
    hard_gate_failures = (
        [f"relative_volume_below_{MIN_RELATIVE_VOLUME:.2f}"]
        if float(result.get("relativeVolume", 0.0)) < MIN_RELATIVE_VOLUME else []
    )
    extension_failure = opening_breakout_extension_failure(result, live_mid)
    if extension_failure:
        hard_gate_failures.append(extension_failure)
    direction, raw_direction, reasons = qualify_signal_direction(
        bull_score=int(result.get("bullScore", 0)),
        bear_score=int(result.get("bearScore", 0)),
        close=live_mid,
        vwap=float(result["vwap"]),
        upper=float(result["vwapUpper"]),
        lower=float(result["vwapLower"]),
        hard_gate_failures=hard_gate_failures,
        current_rsi=float(result["rsi"]),
        min_edge=MIN_DIRECTION_EDGE,
        min_entry_score=effective_entry_score_floor(symbol, setup=result),
        last_bar_close=live_mid,
        prev_bar_close=completed_close,
        min_trigger_move=min_move,
    )
    result.update({
        "direction": direction,
        "rawDirection": raw_direction,
        "liveTrigger": direction is not None,
        "liveTriggerReason": "confirmed" if direction else "not_confirmed",
        "liveTriggerRejectionReasons": reasons,
        "completedBarClose": completed_close,
        "liveUnderlyingBid": bid,
        "liveUnderlyingAsk": ask,
        "liveUnderlyingMid": live_mid,
        "liveQuoteTimestamp": live_quote.get("timestamp"),
        "liveQuoteAgeSeconds": quote_freshness_seconds(live_quote, now=now),
    })
    if direction:
        result["close"] = live_mid
        result["rejectionReasons"] = []
    return result


OCC_SYMBOL_RE = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<expiry>\d{6})(?P<type>[CP])(?P<strike>\d{8})$")


def _business_dte(start: datetime, expiry: datetime) -> int:
    cursor = start.date()
    end = expiry.date()
    count = 0
    while cursor < end:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            count += 1
    return count


def validate_contract_identity(
    contract: dict[str, Any],
    *,
    expected_symbol: str,
    expected_option_type: str,
    expected_dte: int,
    now: datetime,
) -> tuple[bool, str]:
    occ_symbol = str(contract.get("occ_symbol", "")).upper()
    match = OCC_SYMBOL_RE.fullmatch(occ_symbol)
    if not match:
        return False, "invalid_occ_symbol"
    if match.group("root") != expected_symbol.upper():
        return False, "underlying_mismatch"
    expected_occ_type = "C" if expected_option_type.lower() == "call" else "P"
    if match.group("type") != expected_occ_type:
        return False, "option_type_mismatch"
    try:
        occ_expiry = datetime.strptime(match.group("expiry"), "%y%m%d").replace(tzinfo=ET)
        payload_expiry = datetime.fromisoformat(str(contract.get("expiry", ""))).replace(tzinfo=ET)
        payload_strike = float(contract.get("strike"))
    except (TypeError, ValueError):
        return False, "invalid_contract_identity"
    if payload_expiry.date() != occ_expiry.date():
        return False, "expiry_identity_mismatch"
    occ_strike = int(match.group("strike")) / 1000.0
    if not math.isclose(payload_strike, occ_strike, abs_tol=0.001):
        return False, "strike_identity_mismatch"
    if occ_expiry.date() <= now.astimezone(ET).date():
        return False, "expired_or_same_day_contract"
    if _business_dte(now.astimezone(ET), occ_expiry) != expected_dte:
        return False, "dte_mismatch"
    return True, "ok"


def validate_quote_metadata(
    metadata: dict[str, Any],
    *,
    expected_kind: str,
    expected_symbol: str | None,
    max_age_seconds: int,
    now: datetime,
) -> tuple[bool, str]:
    if metadata.get("kind") != expected_kind:
        return False, "quote_kind_mismatch"
    if expected_symbol is not None and metadata.get("symbol") != expected_symbol:
        return False, "quote_symbol_mismatch"
    if metadata.get("source") != "Robinhood MCP":
        return False, "untrusted_quote_source"
    raw_timestamp = metadata.get("timestamp")
    if not isinstance(raw_timestamp, str) or not raw_timestamp:
        return False, "missing_quote_timestamp"
    try:
        quote_time = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False, "invalid_quote_timestamp"
    if quote_time.tzinfo is None:
        return False, "invalid_quote_timestamp"
    age_seconds = (now.astimezone(ET) - quote_time.astimezone(ET)).total_seconds()
    if age_seconds > max_age_seconds:
        return False, "stale_quote"
    if age_seconds < -5:
        return False, "quote_timestamp_in_future"
    return True, "ok"


def quote_freshness_seconds(metadata: dict[str, Any], *, now: datetime) -> float | None:
    raw_timestamp = metadata.get("timestamp")
    if not isinstance(raw_timestamp, str) or not raw_timestamp:
        return None
    try:
        quote_time = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if quote_time.tzinfo is None:
        return None
    return max(0.0, (now.astimezone(ET) - quote_time.astimezone(ET)).total_seconds())


def stale_exit_quote_refresh_allowed(occ_symbol: str) -> bool:
    """Allow one fresh-worker retry per contract during a frozen venue quote."""
    current = time.monotonic()
    previous = _LAST_STALE_QUOTE_REFRESH_AT.get(occ_symbol)
    if previous is not None and current - previous < STALE_EXIT_QUOTE_REFRESH_COOLDOWN_SECONDS:
        return False
    _LAST_STALE_QUOTE_REFRESH_AT[occ_symbol] = current
    return True


def quality_gate(contract: dict[str, Any], direction: str, underlying: float, *, symbol: str = SYMBOL) -> tuple[bool, str]:
    cfg = config_for_symbol(symbol)
    bid, ask, strike = contract.get("bid"), contract.get("ask"), contract.get("strike")
    if not all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0 for value in (bid, ask, strike)):
        return False, "missing_quote"
    if bid > ask:
        return False, "crossed_quote"
    debit = ask * 100
    if not cfg.min_debit <= debit <= cfg.max_debit:
        return False, f"debit_${debit:.2f}_outside_${cfg.min_debit:.0f}-${cfg.max_debit:.0f}"
    spread_pct = (ask - bid) / ask * 100
    if spread_pct > MAX_QUOTE_SPREAD_PCT:
        return False, f"wide_spread_{spread_pct:.1f}%"
    # Relative spread check: reject if spread > 30% of target distance in dollars.
    target_pct = cfg.bearish_profit_target_pct if direction == "bearish" else cfg.profit_target_pct
    target_dollars = ask * target_pct / 100 * 100  # target gain per contract in dollars
    spread_dollars = (ask - bid) * 100  # spread cost per contract in dollars
    if target_dollars > 0 and spread_dollars / target_dollars > MAX_SPREAD_VS_TARGET_PCT / 100:
        return False, f"spread_vs_target_{spread_dollars / target_dollars * 100:.0f}%"
    otm = ((strike - underlying) / underlying * 100) if direction == "bullish" else ((underlying - strike) / underlying * 100)
    if otm > effective_max_otm_pct(symbol):
        return False, f"too_far_otm_{otm:.2f}%"
    # V7.4.4: Delta-aware selection. Estimate delta from OTM distance.
    # For 1DTE options, a rough delta proxy: delta ≈ 0.50 * exp(-otm_pct * 0.5)
    # This gives realistic values: 0% OTM → 0.50, 0.5% OTM → 0.39, 1% OTM → 0.30, 2% OTM → 0.18
    estimated_delta = max(0.02, 0.50 * math.exp(-otm * 0.5))
    if estimated_delta < PREFERRED_DELTA_MIN:
        return False, f"delta_too_low_{estimated_delta:.2f}"
    # V7.4.4: Slippage-aware check. Reject if half-spread eats too much of the
    # expected profit. For a scalp targeting ~10-15% on the option, paying more
    # than 15% of the target gain in half-spread is unacceptable.
    # half_spread_dollars / target_dollars > 15% → reject
    half_spread_dollars = ((ask - bid) / 2) * 100
    if target_dollars > 0 and half_spread_dollars / target_dollars > 0.15:
        return False, f"slippage_{half_spread_dollars / target_dollars * 100:.0f}%_of_target"
    # V7.5: Option liquidity gates — reject thin contracts
    # The Robinhood bridge normalizes contract metadata to snake_case before
    # handing it to the engine. Keep the legacy camelCase fallback for callers
    # that still provide the broker-native shape.
    oi_raw = contract.get("open_interest")
    if oi_raw is None:
        oi_raw = contract.get("openInterest")
    vol_raw = contract.get("volume")
    try:
        oi = float(oi_raw) if oi_raw is not None else 0.0
    except (TypeError, ValueError):
        oi = 0.0
    try:
        vol = float(vol_raw) if vol_raw is not None else 0.0
    except (TypeError, ValueError):
        vol = 0.0
    if not math.isfinite(oi):
        return False, "invalid_open_interest"
    if oi < MIN_OPTION_OPEN_INTEREST:
        return False, f"low_open_interest_{oi:.0f}"
    if not math.isfinite(vol):
        return False, "invalid_option_volume"
    if vol < MIN_OPTION_VOLUME:
        return False, f"low_option_volume_{vol:.0f}"
    return True, "ok"


def validate_exit_quote(
    quote: dict[str, Any],
    *,
    expected_occ_symbol: str,
    now: datetime,
) -> tuple[float | None, str]:
    valid, reason = validate_quote_metadata(
        quote,
        expected_kind="option_quote",
        expected_symbol=None,
        max_age_seconds=MAX_EXIT_QUOTE_AGE_SECONDS,
        now=now,
    )
    if not valid:
        return None, reason
    if quote.get("occ_symbol") != expected_occ_symbol:
        return None, "occ_symbol_mismatch"
    bid, ask = quote.get("bid"), quote.get("ask")
    if not isinstance(bid, (int, float)) or not math.isfinite(bid) or bid < 0:
        return None, "invalid_exit_bid"
    if not isinstance(ask, (int, float)) or not math.isfinite(ask) or ask <= 0:
        return None, "invalid_exit_ask"
    if bid > ask:
        return None, "crossed_quote"
    return float(bid), "ok"


def select_valid_entry_contract(
    candidates: dict[str, Any],
    *,
    direction: str,
    underlying: float,
    now: datetime,
    symbol: str = SYMBOL,
) -> tuple[dict[str, Any] | None, str]:
    if candidates.get("dte_target") != OPTION_DTE_TARGET:
        return None, "dte_target_mismatch"
    valid, reason = validate_quote_metadata(
        candidates,
        expected_kind="option_candidates",
        expected_symbol=symbol,
        max_age_seconds=MAX_ENTRY_QUOTE_AGE_SECONDS,
        now=now,
    )
    if not valid:
        return None, reason
    if is_index_option_symbol(symbol):
        underlying_quote = candidates.get("underlying_quote")
        if not isinstance(underlying_quote, dict):
            return None, "missing_underlying_quote"
        valid, reason = validate_quote_metadata(
            underlying_quote,
            expected_kind=underlying_quote_kind_for_symbol(symbol),
            expected_symbol=symbol,
            max_age_seconds=MAX_INDEX_CANDIDATE_UNDERLYING_QUOTE_AGE_SECONDS,
            now=now,
        )
        if not valid:
            return None, f"underlying_{reason}"
        quote = underlying_quote.get("quote")
        try:
            quote_bid = float(quote["bid_price"])
            quote_ask = float(quote["ask_price"])
            candidate_spot = float(candidates["underlying"])
        except (KeyError, TypeError, ValueError):
            return None, "invalid_underlying_quote"
        if (
            not all(math.isfinite(value) for value in (quote_bid, quote_ask, candidate_spot))
            or quote_bid <= 0 or quote_ask < quote_bid or candidate_spot <= 0
        ):
            return None, "invalid_underlying_quote"
        quote_mid = (quote_bid + quote_ask) / 2.0
        if not math.isclose(candidate_spot, quote_mid, rel_tol=0.0, abs_tol=0.01):
            return None, "underlying_quote_spot_mismatch"
    option_type = "call" if direction == "bullish" else "put"
    contract = candidates.get(option_type)
    if not isinstance(contract, dict):
        return None, f"no_{option_type}_candidate"
    instrument_id = contract.get("instrument_id")
    if not isinstance(instrument_id, str) or not instrument_id.strip():
        return None, "missing_instrument_id"
    valid, reason = validate_contract_identity(
        contract,
        expected_symbol=symbol,
        expected_option_type=option_type,
        expected_dte=OPTION_DTE_TARGET,
        now=now,
    )
    if not valid:
        return None, reason
    valid, reason = quality_gate(contract, direction, underlying, symbol=symbol)
    if not valid:
        return None, reason
    return contract, "ok"


def entry_risk_budget(
    state: dict[str, Any], *, entry_cost: float, symbol: str = SYMBOL,
) -> tuple[bool, str, float, float]:
    cfg = config_for_symbol(symbol)
    planned_risk = round(entry_cost * cfg.stop_loss_pct / 100.0, 2)
    remaining = round(max(0.0, MAX_DAILY_LOSS - float(state.get("dailyGrossLoss", 0.0))), 2)
    if planned_risk > remaining:
        return False, "insufficient_daily_risk_budget", planned_risk, remaining
    # V7.4.4: Portfolio-wide daily loss cap across all symbols
    portfolio_loss = _portfolio_daily_gross_loss()
    portfolio_remaining = round(max(0.0, PORTFOLIO_DAILY_LOSS_CAP - portfolio_loss), 2)
    if planned_risk > portfolio_remaining:
        return False, f"portfolio_risk_exceeded_${portfolio_loss:.0f}_of_${PORTFOLIO_DAILY_LOSS_CAP:.0f}", planned_risk, portfolio_remaining
    return True, "ok", planned_risk, remaining


def _portfolio_daily_gross_loss() -> float:
    """Sum ledger-reconciled daily gross loss across the portfolio."""
    total = 0.0
    for sym in SYMBOLS:
        state = load_state(sym)
        ledger = log_path(sym)
        loss = state.get("dailyGrossLoss", 0.0)
        if (
            not isinstance(loss, (int, float))
            or isinstance(loss, bool)
            or not math.isfinite(float(loss))
            or loss < 0
        ):
            raise StateCorruptionError(f"portfolio_loss_invalid:{sym}")
        # A nonzero state counter without its ledger is not safe to use. An
        # absent ledger with zero reconciled loss is the normal no-trade case.
        if not ledger.exists() and loss != 0:
            raise StateCorruptionError(f"portfolio_ledger_missing:{sym}")
        total += float(loss)
    return round(total, 2)


def apply_symbol_entry_score_gate(
    setup: dict[str, Any], *, symbol: str = SYMBOL,
) -> dict[str, Any]:
    """Fail closed when a directed setup misses its symbol-specific score floor."""
    result = dict(setup)
    direction = result.get("direction")
    score = result.get("score")
    if result.get("trendConfluence") is True:
        required = int(result.get("trendEntryScoreFloor", TREND_CONTINUATION_MIN_SCORE))
    else:
        required = entry_score_for_symbol(symbol)
    score_valid = (
        isinstance(score, (int, float))
        and not isinstance(score, bool)
        and math.isfinite(float(score))
        and float(score) >= required
    )
    if direction in {"bullish", "bearish"} and not score_valid:
        result["direction"] = None
        reasons = list(result.get("rejectionReasons") or [])
        reason = f"symbol_score_below_gate_{required}"
        if reason not in reasons:
            reasons.append(reason)
        result["rejectionReasons"] = reasons
    return result


def _squeeze_qualified(signal: dict[str, Any]) -> bool:
    """Return whether a confirmed Bollinger squeeze breakout is present."""
    if not isinstance(signal, dict):
        return False
    direction = signal.get("direction") or signal.get("rawDirection")
    checks = signal.get("bullChecks" if direction == "bullish" else "bearChecks")
    if not isinstance(checks, dict):
        return False
    return bool(checks.get("squeeze_breakout"))


def _structural_breakout_qualified(signal: dict[str, Any]) -> bool:
    """Require Turtle, squeeze, or strict ORB trend-confluence structure."""
    if _actual_turtle_breakout(signal) is True or _squeeze_qualified(signal):
        return True
    if not isinstance(signal, dict) or signal.get("trendConfluence") is not True:
        return False
    direction = signal.get("direction")
    orb = signal.get("orbContext")
    if direction not in {"bullish", "bearish"} or not isinstance(orb, dict):
        return False
    breakout_key = "bullBreakout" if direction == "bullish" else "bearBreakout"
    return (
        orb.get("ready") is True
        and orb.get(breakout_key) is True
        and isinstance(orb.get("rangePct"), (int, float))
        and float(orb["rangePct"]) >= TREND_ORB_MIN_RANGE_PCT
        and isinstance(orb.get("breakoutPct"), (int, float))
        and float(orb["breakoutPct"]) >= TREND_ORB_BREAKOUT_PCT
    )


def apply_structural_entry_gate(setup: dict[str, Any]) -> dict[str, Any]:
    """Require a real Turtle or squeeze breakout before entry lookup."""
    result = dict(setup)
    if result.get("direction") in {"bullish", "bearish"}:
        if not _structural_breakout_qualified(result):
            result["direction"] = None
            reasons = list(result.get("rejectionReasons") or [])
            if "signal_nonstructural_turtle" not in reasons:
                reasons.append("signal_nonstructural_turtle")
            result["rejectionReasons"] = reasons
    return result


def post_quote_signal_failure(
    original: dict[str, Any], refreshed: dict[str, Any], *, symbol: str = SYMBOL,
) -> str | None:
    for reason in (refreshed.get("rejectionReasons") or []):
        if isinstance(reason, str) and reason.startswith("hard_gate:"):
            return reason
    refreshed_direction = refreshed.get("direction")
    if refreshed_direction not in {"bullish", "bearish"}:
        return "signal_no_longer_qualified"
    if refreshed_direction != original.get("direction"):
        return "signal_direction_changed"
    if not _structural_breakout_qualified(refreshed):
        return "signal_nonstructural_turtle"
    score = refreshed.get("score")
    required_score = (
        int(refreshed.get("trendEntryScoreFloor", TREND_CONTINUATION_MIN_SCORE))
        if refreshed.get("trendConfluence") is True
        else effective_entry_score_floor(symbol, setup=refreshed)
    )
    if (not isinstance(score, (int, float)) or not math.isfinite(score)
            or score < required_score):
        return "signal_score_below_gate"
    relative_volume = refreshed.get("relativeVolume")
    if (not isinstance(relative_volume, (int, float)) or not math.isfinite(relative_volume)
            or relative_volume < MIN_RELATIVE_VOLUME):
        return "signal_relative_volume_below_gate"
    # Post-quote recomputation must retain the completed-bar momentum gate.
    # Live-trigger confirmation can change timing, not indicator evidence.
    if not refreshed.get("trendConfluence") and not refreshed.get("detector"):
        checks = refreshed.get("bullChecks") if refreshed_direction == "bullish" else refreshed.get("bearChecks")
        if not isinstance(checks, dict) or checks.get("momentum") is not True:
            return "momentum_not_confirmed"
    return None


def pending_entry_is_active(state: dict[str, Any], *, now: datetime) -> bool:
    pending = state.get("pendingEntry")
    if not isinstance(pending, dict):
        return False
    try:
        expires_at = datetime.fromisoformat(str(pending["expiresAt"])).astimezone(ET)
    except (KeyError, TypeError, ValueError):
        return False
    return expires_at > now.astimezone(ET)


def clear_expired_entry_reservation(state: dict[str, Any], *, now: datetime) -> None:
    if state.get("pendingEntry") and not pending_entry_is_active(state, now=now):
        state["pendingEntry"] = None


def available_daily_planned_risk(state: dict[str, Any], *, now: datetime, symbol: str = SYMBOL) -> float:
    clear_expired_entry_reservation(state, now=now)
    realized = float(state.get("dailyGrossLoss", 0.0))
    open_reserved = sum(float(position.get("riskReserved", 0.0)) for position in state.get("activePositions", []))
    pending = state.get("pendingEntry")
    pending_reserved = float(pending.get("riskReserved", 0.0)) if isinstance(pending, dict) else 0.0
    return round(max(0.0, MAX_DAILY_LOSS - realized - open_reserved - pending_reserved), 2)


def create_entry_reservation(
    state: dict[str, Any], *, setup: dict[str, Any], now: datetime, symbol: str = SYMBOL,
) -> tuple[dict[str, Any] | None, str]:
    cfg = config_for_symbol(symbol)
    clear_expired_entry_reservation(state, now=now)
    if pending_entry_is_active(state, now=now):
        return None, "pending_entry_exists"
    available = available_daily_planned_risk(state, now=now)
    stop_fraction = cfg.stop_loss_pct / 100.0
    max_debit = round(min(cfg.max_debit, available / stop_fraction), 2) if stop_fraction > 0 else 0.0
    if max_debit < cfg.min_debit:
        return None, "insufficient_daily_risk_budget"
    reservation = {
        "reservationId": uuid.uuid4().hex,
        "direction": setup.get("direction"),
        "score": setup.get("score"),
        "relativeVolume": setup.get("relativeVolume"),
        "riskReserved": round(max_debit * stop_fraction, 2),
        "maxDebit": max_debit,
        "createdAt": now.astimezone(ET).isoformat(),
        "expiresAt": (now.astimezone(ET) + timedelta(seconds=ENTRY_RESERVATION_TTL_SECONDS)).isoformat(),
        "engineVersion": ENGINE_VERSION,
        "configSha256": config_sha256(),
    }
    state["pendingEntry"] = reservation
    return reservation, "ok"


def release_entry_reservation(state: dict[str, Any], reservation_id: str) -> bool:
    pending = state.get("pendingEntry")
    if not isinstance(pending, dict) or pending.get("reservationId") != reservation_id:
        return False
    state["pendingEntry"] = None
    return True


def market_block(
    now: datetime,
    state: dict[str, Any],
    *,
    symbol: str = SYMBOL,
    ignore_reservation_id: str | None = None,
) -> str | None:
    clear_expired_entry_reservation(state, now=now)
    current = now.astimezone(ET)
    if is_index_option_symbol(symbol):
        if not xsp_market_open(current):
            return "market_closed"
    elif current.weekday() >= 5:
        return "market_closed"
    event_block = calendar_block(current)
    if event_block:
        return event_block
    if not entry_window_open(current, symbol):
        return "outside_entry_window"
    tm = current.hour * 100 + current.minute
    if LUNCH_START <= tm <= LUNCH_END:
        return "lunch_chop_window"
    trade_limit = trade_limit_for_symbol(symbol)
    if trade_limit is not None and state["dailyTrades"] >= trade_limit:
        return "daily_trade_limit"
    if state.get("dailyGrossLoss", 0.0) >= MAX_DAILY_LOSS:
        return "daily_loss_limit"
    if state["activePositions"]:
        return "position_already_open"
    pending = state.get("pendingEntry")
    if (isinstance(pending, dict) and pending_entry_is_active(state, now=current)
            and pending.get("reservationId") != ignore_reservation_id):
        return "pending_entry_exists"
    if kill_switch_active():
        return "kill_switch"
    return None


def loss_cooldown_blocks(state: dict[str, Any], setup: dict[str, Any]) -> bool:
    """Return whether the loss-streak score gate blocks this setup.

    A confirmed crash detector setup bypasses only this adaptive score gate.
    All market, quote, contract, and risk controls remain active.
    """
    if setup.get("crash_detector") is True:
        return False
    return (
        state.get("consecutiveLosses", 0) >= CONSECUTIVE_LOSS_COOLDOWN
        and setup.get("score", 0) < MIN_SCORE_AFTER_LOSS
    )


def reentry_policy_block(state: dict[str, Any], setup: dict[str, Any], *, now: datetime) -> str | None:
    """Return a skip reason when re-entry risk policy blocks this setup.

    V7.5.3 operator-approved risk policy (2026-10-12 entry review):
    1. ``duplicate_signal_bar_entry`` — never enter twice on the same
       completed signal bar. Detector-derived setups that carry no
       ``signalBarEnd`` are not bar-deduplicated; the post-loss delay below
       still applies to them.
    2. ``post_loss_same_direction_delay`` — after a losing exit, block
       same-direction entries for ``POST_LOSS_SAME_DIRECTION_DELAY_MINUTES``.
       A winning exit does not clear the delay.
    """
    bar = setup.get("signalBarEnd")
    last_bar = state.get("lastEnteredSignalBarEnd")
    if isinstance(bar, str) and isinstance(last_bar, str) and bar == last_bar:
        return "duplicate_signal_bar_entry"
    direction = setup.get("direction")
    last_loss_direction = state.get("lastLossDirection")
    last_loss_at = state.get("lastLossExitAt")
    if (
        direction in {"bullish", "bearish"}
        and direction == last_loss_direction
        and isinstance(last_loss_at, str)
    ):
        try:
            elapsed = (
                now.astimezone(ET) - datetime.fromisoformat(last_loss_at).astimezone(ET)
            ).total_seconds()
        except ValueError:
            return None
        if 0 <= elapsed < POST_LOSS_SAME_DIRECTION_DELAY_MINUTES * 60.0:
            return "post_loss_same_direction_delay"
    return None


def record_entry_reentry_state(state: dict[str, Any], position: dict[str, Any]) -> None:
    """Remember the signal bar of the most recent entry for same-bar dedup."""
    bar = (position.get("signal") or {}).get("signalBarEnd")
    if isinstance(bar, str) and bar:
        state["lastEnteredSignalBarEnd"] = bar


def record_exit_reentry_state(
    state: dict[str, Any], position: dict[str, Any], pnl: float, now: datetime,
) -> None:
    """Remember the most recent losing exit for the same-direction delay."""
    if pnl >= 0:
        return
    direction = (position.get("signal") or {}).get("direction") or position.get("direction")
    if direction in {"bullish", "bearish"}:
        state["lastLossExitAt"] = now.isoformat()
        state["lastLossDirection"] = direction


def apply_normal_path_momentum_gate(setup: dict[str, Any]) -> dict[str, Any]:
    """V7.5.4: require directional momentum on the normal signal path too.

    Operator-approved momentum principle (2026-10-12 winner review): every
    momentum-failed entry cohort in the paper data lost money. Confluence
    entries already require momentum (V7.5.3 required_checks); crash/pump
    detector setups are exempt by design. Normal-path momentum-failed entries
    were 0W/2L in the modern cohort — a consistency extension, not a
    statistical fix. Rejection reason: momentum_not_confirmed.
    """
    if setup.get("direction") not in {"bullish", "bearish"}:
        return setup
    if setup.get("trendConfluence") or setup.get("detector"):
        return setup
    checks = setup.get("bullChecks") if setup["direction"] == "bullish" else setup.get("bearChecks")
    if not (checks or {}).get("momentum"):
        result = dict(setup)
        result["direction"] = None
        result["rejectionReasons"] = list(setup.get("rejectionReasons") or []) + ["momentum_not_confirmed"]
        return result
    return setup


def _scan_transaction(symbol: str = SYMBOL) -> int:
    cfg = config_for_symbol(symbol)
    # Short preflight lock only. Market-data and quote network calls must never
    # hold this lock because the monitor needs priority access to open positions.
    with exclusive_state_lock(symbol):
        state = load_state(symbol)
        previous_reset_date = state.get("lastResetDate")
        reset_daily(state, symbol=symbol)
        now = now_et()
        block = market_block(now, state, symbol=symbol)
        if state.get("lastResetDate") != previous_reset_date:
            save_state(state, symbol)
        if block:
            print(json.dumps({"action": "SKIP", "reason": block, "at": now.isoformat(), "symbol": symbol}))
            return 0
    try:
        bars = fetch_bars(symbol)
        data_now = now_et()
        day = completed_day_bars(bars, now=data_now, symbol=symbol)
        bars_fresh, bars_reason = validate_completed_bar_freshness(day, now=data_now, symbol=symbol)
        if not bars_fresh:
            if bars_reason == "venue_warmup_no_completed_bar":
                # XSP overnight venue just opened; not enough completed bars yet.
                # Skip gracefully instead of raising an error that spams the log.
                log({"event": "VENUE_WARMUP", "reason": bars_reason, "symbol": symbol, "day_bars": len(day)}, symbol)
                print(json.dumps({"action": "SKIP", "reason": "venue_warmup", "symbol": symbol}))
                return 0
            raise RuntimeError(bars_reason)
        # Index-option symbols (XSP) need enough current-session bars to compute
        # VWAP and indicators.  During the first few minutes after the overnight
        # venue opens, there may be only 1-2 completed bars — not enough for
        # meaningful signal computation.  Skip gracefully until at least 6 bars
        # (30 minutes of data) are available.
        MIN_SESSION_BARS_INDEX_OPTION = 6  # 30 min of 5m bars
        if is_index_option_symbol(symbol) and len(day) < MIN_SESSION_BARS_INDEX_OPTION:
            log({"event": "VENUE_WARMUP", "reason": "insufficient_session_bars", "symbol": symbol, "day_bars": len(day)}, symbol)
            print(json.dumps({"action": "SKIP", "reason": f"venue_warmup_{len(day)}_bars_lt_{MIN_SESSION_BARS_INDEX_OPTION}", "symbol": symbol}))
            return 0
        warmup, opening_rel_volume, rel_volume_source, premarket_context = session_signal_inputs(
            bars, day, now=data_now, symbol=symbol,
        )
        setup = signal(
            day,
            warmup_bars=warmup,
            relative_volume=opening_rel_volume,
            premarket_context=premarket_context,
            symbol=symbol,
        )
        setup = apply_trend_confluence_gate(
            setup,
            symbol=symbol,
            bar_time=(day[-1].get("time") if day else None),
        )
        setup["relativeVolumeSource"] = rel_volume_source
        setup["signalBarStart"] = day[-1]["dt"].isoformat()
        setup["signalBarEnd"] = (day[-1]["dt"] + timedelta(minutes=BAR_INTERVAL_MINUTES)).isoformat()
    except Exception as exc:
        log({"event": "DATA_ERROR", "error": str(exc), "symbol": symbol}, symbol); print(json.dumps({"action": "ERROR", "error": str(exc), "symbol": symbol})); return 1

    # Crash detector: check for rapid momentum crashes even if normal signal doesn't qualify
    if not setup["direction"]:
        crash = crash_signal(day, symbol)
        if crash:
            # Apply safety filters to crash signal (edge, RSI, anti-chase) but skip
            # fresh-trigger since crash detection itself confirms the move is live.
            c_dir, c_raw, c_reasons = qualify_signal_direction(
                bull_score=crash.get("bullScore", 0), bear_score=crash.get("bearScore", 0),
                close=crash["close"], vwap=crash["vwap"], upper=crash["vwapUpper"], lower=crash["vwapLower"],
                current_rsi=crash["rsi"], min_edge=MIN_DIRECTION_EDGE, skip_fresh_trigger=True,
                skip_anti_chase=True, skip_rsi_extreme=True,
                min_entry_score=effective_entry_score_floor(
                    symbol, bar_time=(day[-1].get("time") if day else None), setup=crash,
                ),
            )
            if c_dir:
                crash["direction"] = c_dir
                crash["rawDirection"] = c_raw
                crash["rejectionReasons"] = []
                crash["detector"] = "crash_detector"
                setup = crash
                log({"event": "CRASH_DETECTED", "symbol": symbol, "macd_delta_3": crash.get("macd_delta_3"), "rsi_drop": crash.get("rsi_drop"), "close": crash["close"], "score": crash["score"]}, symbol)
            else:
                crash["direction"] = None
                crash["rejectionReasons"] = c_reasons
                now_mono = time.monotonic()
                if now_mono - _LAST_CRASH_REJECT_LOG_TS.get(symbol, 0.0) >= CRASH_LOG_COOLDOWN_SECONDS:
                    _LAST_CRASH_REJECT_LOG_TS[symbol] = now_mono
                    log({"event": "CRASH_SIGNAL_REJECTED", "symbol": symbol, "reasons": c_reasons, "signal": crash}, symbol)

    # Pump detector: check for rapid momentum pumps even if normal signal doesn't qualify
    if not setup["direction"]:
        pump = pump_signal(day, symbol)
        if pump:
            p_dir, p_raw, p_reasons = qualify_signal_direction(
                bull_score=pump.get("bullScore", 0), bear_score=pump.get("bearScore", 0),
                close=pump["close"], vwap=pump["vwap"], upper=pump["vwapUpper"], lower=pump["vwapLower"],
                current_rsi=pump["rsi"], min_edge=MIN_DIRECTION_EDGE, skip_fresh_trigger=True,
                skip_anti_chase=True, skip_rsi_extreme=True,
                min_entry_score=effective_entry_score_floor(
                    symbol, bar_time=(day[-1].get("time") if day else None), setup=pump,
                ),
            )
            if p_dir:
                pump["direction"] = p_dir
                pump["rawDirection"] = p_raw
                pump["rejectionReasons"] = []
                pump["detector"] = "pump_detector"
                setup = pump
                log({"event": "PUMP_DETECTED", "symbol": symbol, "macd_delta_3": pump.get("macd_delta_3"), "rsi_surge": pump.get("rsi_surge"), "close": pump["close"], "score": pump["score"]}, symbol)
            else:
                pump["direction"] = None
                pump["rejectionReasons"] = p_reasons
                now_mono = time.monotonic()
                if now_mono - _LAST_PUMP_REJECT_LOG_TS.get(symbol, 0.0) >= CRASH_LOG_COOLDOWN_SECONDS:
                    _LAST_PUMP_REJECT_LOG_TS[symbol] = now_mono
                    log({"event": "PUMP_SIGNAL_REJECTED", "symbol": symbol, "reasons": p_reasons, "signal": pump}, symbol)

    # ORB detector: Opening Range Breakout (first 30 min high/low break)
    # DISABLED — A/B tested and proved harmful. See skill docs.
    if ORB_ENABLED and not setup["direction"]:
        orb = orb_signal(day)
        if orb:
            o_dir, o_raw, o_reasons = qualify_signal_direction(
                bull_score=orb.get("bullScore", 0), bear_score=orb.get("bearScore", 0),
                close=orb["close"], vwap=orb["vwap"], upper=orb["vwapUpper"], lower=orb["vwapLower"],
                current_rsi=orb["rsi"], min_edge=MIN_DIRECTION_EDGE, skip_fresh_trigger=True,
                skip_anti_chase=True,
                min_entry_score=effective_entry_score_floor(
                    symbol, bar_time=(day[-1].get("time") if day else None), setup=orb,
                ),
            )
            if o_dir:
                orb["direction"] = o_dir
                orb["rawDirection"] = o_raw
                orb["rejectionReasons"] = []
                setup = orb
                log({"event": "ORB_DETECTED", "direction": orb["direction"], "orb_high": orb.get("orb_high"), "orb_low": orb.get("orb_low"), "breakout_pct": orb.get("breakout_pct"), "close": orb["close"], "score": orb["score"]})
            else:
                orb["direction"] = None
                orb["rejectionReasons"] = o_reasons
                log({"event": "ORB_SIGNAL_REJECTED", "reasons": o_reasons, "signal": orb})

    setup = apply_symbol_entry_score_gate(setup, symbol=symbol)
    setup = apply_structural_entry_gate(setup)
    setup = apply_normal_path_momentum_gate(setup)

    # Near-real-time timing confirmation. The live quote may only satisfy the
    # fresh-trigger check for a completed-bar setup that already has score,
    # direction edge, relative volume, RSI, and anti-chase eligibility.
    if not setup["direction"] and live_trigger_eligible(setup, symbol):
        try:
            live_quote = fetch_live_equity_quote(symbol)
            live_setup = apply_live_trigger_confirmation(setup, live_quote, now=now_et(), symbol=symbol)
            if live_setup.get("direction"):
                setup = live_setup
                log({
                    "event": "LIVE_TRIGGER_CONFIRMED",
                    "symbol": symbol,
                    "direction": setup["direction"],
                    "completedBarClose": setup.get("completedBarClose"),
                    "liveUnderlyingMid": setup.get("liveUnderlyingMid"),
                    "liveQuoteTimestamp": setup.get("liveQuoteTimestamp"),
                    "signalBarStart": setup.get("signalBarStart"),
                }, symbol)
            else:
                setup = live_setup
        except Exception as exc:
            log({"event": "LIVE_TRIGGER_QUOTE_ERROR", "symbol": symbol, "error": str(exc), "signal": setup}, symbol)

    if not setup["direction"]:
        print(json.dumps({"action": "SKIP", "reason": "no_qualified_signal", "signal": setup}))
        return 0

    # Consecutive loss cooldown: after N consecutive losses, require higher score
    consecutive_losses = state.get("consecutiveLosses", 0)
    if loss_cooldown_blocks(state, setup):
        print(json.dumps({"action": "SKIP", "reason": "consecutive_loss_cooldown", "consecutiveLosses": consecutive_losses, "requiredScore": MIN_SCORE_AFTER_LOSS, "actualScore": setup.get("score", 0), "signal": setup}))
        return 0
    # V7.5.3 re-entry risk policy: same-bar dedup + post-loss same-direction delay
    reentry_block = reentry_policy_block(state, setup, now=now)
    if reentry_block:
        print(json.dumps({"action": "SKIP", "reason": reentry_block, "symbol": symbol}))
        return 0
    reservation: dict[str, Any] | None = None
    with exclusive_state_lock(symbol):
        # Serialize competing scans and reserve planned risk before starting the
        # expensive quote request. The reservation expires if this process dies.
        state = load_state(symbol)
        reset_daily(state, symbol=symbol)
        reservation_at = now_et()
        reservation_block = market_block(reservation_at, state, symbol=symbol)
        if reservation_block:
            save_state(state, symbol)
            print(json.dumps({"action": "SKIP", "reason": f"pre_quote:{reservation_block}", "symbol": symbol}))
            return 0
        consecutive_losses = state.get("consecutiveLosses", 0)
        if loss_cooldown_blocks(state, setup):
            save_state(state, symbol)
            print(json.dumps({"action": "SKIP", "reason": "pre_quote:consecutive_loss_cooldown", "symbol": symbol}))
            return 0
        reentry_block = reentry_policy_block(state, setup, now=reservation_at)
        if reentry_block:
            save_state(state, symbol)
            print(json.dumps({"action": "SKIP", "reason": f"pre_quote:{reentry_block}", "symbol": symbol}))
            return 0
        reservation, reservation_reason = create_entry_reservation(state, setup=setup, now=reservation_at, symbol=symbol)
        if reservation is None:
            save_state(state, symbol)
            print(json.dumps({"action": "SKIP", "reason": reservation_reason, "symbol": symbol}))
            return 0
        log({"event": "ENTRY_RESERVATION", **reservation}, symbol)
        save_state(state, symbol)
    reservation_id = str(reservation["reservationId"])
    try:
        option_type = "call" if setup["direction"] == "bullish" else "put"
        candidates = bridge(
            "--scalp-candidates", symbol,
            "--option-type", option_type,
            "--underlying", str(setup["close"]),
            "--dte", str(OPTION_DTE_TARGET),
            "--min-debit", str(cfg.min_debit),
            "--max-debit", str(reservation["maxDebit"]),
            "--max-otm-pct", str(effective_max_otm_pct(symbol)),
        )
        refresh_bars = fetch_bars(symbol, force=True)
        refresh_at = now_et()
        refresh_day = completed_day_bars(refresh_bars, now=refresh_at, symbol=symbol)
        refresh_ok, refresh_reason = validate_completed_bar_freshness(refresh_day, now=refresh_at, symbol=symbol)
        if not refresh_ok:
            if refresh_reason == "venue_warmup_no_completed_bar":
                log({"event": "VENUE_WARMUP", "reason": refresh_reason, "symbol": symbol, "day_bars": len(refresh_day)}, symbol)
                raise RuntimeError(f"post_quote_bars:{refresh_reason}")
            raise RuntimeError(f"post_quote_bars:{refresh_reason}")
        refresh_warmup, refresh_rel_volume, refresh_rel_volume_source, refresh_premarket = session_signal_inputs(
            refresh_bars, refresh_day, now=refresh_at, symbol=symbol,
        )
        refreshed_setup = detect_setup_for_recheck(
            refresh_day,
            warmup_bars=refresh_warmup,
            relative_volume=refresh_rel_volume,
            premarket_context=refresh_premarket,
            symbol=symbol,
        )
        refreshed_setup["relativeVolumeSource"] = refresh_rel_volume_source
        refreshed_setup["signalBarStart"] = refresh_day[-1]["dt"].isoformat()
        refreshed_setup["signalBarEnd"] = (
            refresh_day[-1]["dt"] + timedelta(minutes=BAR_INTERVAL_MINUTES)
        ).isoformat()
        if setup.get("liveTrigger") and not refreshed_setup.get("direction"):
            refreshed_setup = apply_live_trigger_confirmation(
                refreshed_setup,
                fetch_live_equity_quote(symbol),
                now=now_et(),
                symbol=symbol,
            )
        refreshed_setup = apply_symbol_entry_score_gate(refreshed_setup, symbol=symbol)
        signal_failure = post_quote_signal_failure(setup, refreshed_setup, symbol=symbol)
        with exclusive_state_lock(symbol):
            # Time-of-check/time-of-use defense: reload and replay the ledger
            # after the slow quote call, then repeat every mutable control.
            state = load_state(symbol)
            reset_daily(state, symbol=symbol)
            accepted_at = now_et()
            current_reservation = state.get("pendingEntry")
            if (not isinstance(current_reservation, dict)
                    or current_reservation.get("reservationId") != reservation_id
                    or not pending_entry_is_active(state, now=accepted_at)):
                log({"event": "POST_QUOTE_REJECT", "reason": "reservation_missing_or_expired",
                     "reservationId": reservation_id, "direction": setup["direction"], "signal": setup}, symbol)
                save_state(state, symbol)
                print(json.dumps({"action": "SKIP", "reason": "post_quote:reservation_missing_or_expired", "symbol": symbol}))
                return 0
            if signal_failure:
                release_entry_reservation(state, reservation_id)
                log({"event": "POST_QUOTE_REJECT", "reason": signal_failure,
                     "reservationId": reservation_id, "originalSignal": setup,
                     "refreshedSignal": refreshed_setup}, symbol)
                save_state(state, symbol)
                print(json.dumps({"action": "SKIP", "reason": f"post_quote:{signal_failure}", "symbol": symbol}))
                return 0
            setup = refreshed_setup
            post_quote_block = market_block(
                accepted_at, state, symbol=symbol, ignore_reservation_id=reservation_id,
            )
            if post_quote_block:
                release_entry_reservation(state, reservation_id)
                log({"event": "POST_QUOTE_REJECT", "reason": post_quote_block,
                     "reservationId": reservation_id, "direction": setup["direction"], "signal": setup}, symbol)
                save_state(state, symbol)
                print(json.dumps({"action": "SKIP", "reason": f"post_quote:{post_quote_block}", "symbol": symbol}))
                return 0
            consecutive_losses = state.get("consecutiveLosses", 0)
            if loss_cooldown_blocks(state, setup):
                release_entry_reservation(state, reservation_id)
                log({"event": "POST_QUOTE_REJECT", "reason": "consecutive_loss_cooldown",
                     "reservationId": reservation_id, "direction": setup["direction"], "signal": setup}, symbol)
                save_state(state, symbol)
                print(json.dumps({"action": "SKIP", "reason": "post_quote:consecutive_loss_cooldown", "symbol": symbol}))
                return 0
            reentry_block = reentry_policy_block(state, setup, now=accepted_at)
            if reentry_block:
                release_entry_reservation(state, reservation_id)
                log({"event": "POST_QUOTE_REJECT", "reason": reentry_block,
                     "reservationId": reservation_id, "direction": setup["direction"], "signal": setup}, symbol)
                save_state(state, symbol)
                print(json.dumps({"action": "SKIP", "reason": f"post_quote:{reentry_block}", "symbol": symbol}))
                return 0
            quote_underlying = candidates.get("underlying", setup["close"])
            if (not isinstance(quote_underlying, (int, float))
                    or not math.isfinite(quote_underlying) or quote_underlying <= 0):
                quote_underlying = setup["close"]
            opening_failure = opening_breakout_extension_failure(setup, float(quote_underlying))
            if opening_failure:
                release_entry_reservation(state, reservation_id)
                log({
                    "event": "POST_QUOTE_REJECT", "reason": opening_failure,
                    "reservationId": reservation_id, "direction": setup["direction"],
                    "quoteUnderlying": quote_underlying, "signal": setup,
                }, symbol)
                save_state(state, symbol)
                print(json.dumps({"action": "SKIP", "reason": f"post_quote:{opening_failure}", "symbol": symbol}))
                return 0
            drift_failure = entry_directional_drift_reason(setup, float(quote_underlying))
            if drift_failure:
                release_entry_reservation(state, reservation_id)
                log({
                    "event": "POST_QUOTE_REJECT", "reason": drift_failure,
                    "reservationId": reservation_id, "direction": setup["direction"],
                    "signalClose": setup.get("close"),
                    "quoteUnderlying": quote_underlying,
                    "timingTolerance": max(0.01, float(setup.get("minTriggerMove") or 0.02) * 0.50),
                    "signal": setup,
                }, symbol)
                save_state(state, symbol)
                print(json.dumps({"action": "SKIP", "reason": f"post_quote:{drift_failure}", "symbol": symbol}))
                return 0
            contract, why = select_valid_entry_contract(
                candidates,
                direction=setup["direction"],
                underlying=float(quote_underlying),
                now=accepted_at,
                symbol=symbol,
            )
            if contract is None:
                release_entry_reservation(state, reservation_id)
                log({"event": "SKIP_QUOTE", "reason": why, "direction": setup["direction"],
                     "reservationId": reservation_id, "signal": setup, "candidates": candidates}, symbol)
                save_state(state, symbol)
                print(json.dumps({"action": "SKIP", "reason": why, "symbol": symbol}))
                return 0
            entry = float(contract["ask"]) * 100
            risk_allowed, risk_reason, planned_risk, remaining_risk = entry_risk_budget(state, entry_cost=entry, symbol=symbol)
            if not risk_allowed:
                release_entry_reservation(state, reservation_id)
                log({"event": "SKIP_RISK", "reason": risk_reason, "plannedRisk": planned_risk,
                     "remainingRisk": remaining_risk, "reservationId": reservation_id,
                     "direction": setup["direction"], "signal": setup}, symbol)
                save_state(state, symbol)
                print(json.dumps({"action": "SKIP", "reason": risk_reason,
                                  "plannedRisk": planned_risk, "remainingRisk": remaining_risk, "symbol": symbol}))
                return 0
            position = {"positionId": f"paper-{accepted_at.strftime('%Y%m%dT%H%M%S')}", "symbol": symbol,
                    "direction": setup["direction"], "optionType": "call" if setup["direction"] == "bullish" else "put",
                    "occSymbol": contract["occ_symbol"], "expiry": contract["expiry"], "strike": contract["strike"],
                    "instrumentId": contract["instrument_id"],
                    "entryAsk": float(contract["ask"]), "entryBid": float(contract["bid"]), "entryCost": entry,
                    "entryUnderlying": float(quote_underlying), "entryVwap": setup.get("vwap", 0.0),
                    "entryTime": accepted_at.isoformat(), "peakBid": float(contract["bid"]),
                    "turtleN": setup.get("turtle", {}).get("n", 0.0),
                    "turtleEntryHigh": setup.get("turtle", {}).get("entryHigh", 0.0),
                    "turtleEntryLow": setup.get("turtle", {}).get("entryLow", 0.0),
                    "turtleExitHigh": setup.get("turtle", {}).get("exitHigh", 0.0),
                    "turtleExitLow": setup.get("turtle", {}).get("exitLow", 0.0),
                    "turtleStop": (setup["close"] - 2 * setup["turtle"]["n"] if setup["direction"] == "bullish" else setup["close"] + 2 * setup["turtle"]["n"]),
                    "signal": setup, "quoteSource": "Robinhood MCP",
                    "entryQuoteTimestamp": candidates.get("timestamp"),
                    "entryQuoteSnapshot": candidates.get("_snapshotPath"),
                    "reservationId": reservation_id, "riskReserved": planned_risk,
                    "engineVersion": ENGINE_VERSION, "strategyId": STRATEGY_ID,
                    "sourceCodeSha256": source_code_sha256(), "configSha256": config_sha256()}
            release_entry_reservation(state, reservation_id)
            state["activePositions"] = [position]
            state["dailyTrades"] += 1
            record_entry_reentry_state(state, position)
            log({"event": "PAPER_ENTRY_REAL_ASK", **position,
                 "candidateTimestamp": candidates.get("timestamp"),
                 "quoteSnapshot": candidates.get("_snapshotPath")}, symbol)
            save_state(state, symbol)
        print(json.dumps({"action": "PAPER_ENTRY", "position": position}, indent=2))
        return 0
    except (StateLockBusy, StateCorruptionError):
        raise
    except Exception as exc:
        # Release only our own reservation. If another process has changed it,
        # leave that state untouched and let the normal expiry path handle it.
        try:
            with exclusive_state_lock(symbol):
                state = load_state(symbol)
                released = release_entry_reservation(state, reservation_id)
                log({"event": "QUOTE_ERROR", "error": str(exc), "signal": setup,
                     "reservationId": reservation_id, "reservationReleased": released}, symbol)
                save_state(state, symbol)
        except (StateLockBusy, StateCorruptionError):
            raise
        print(json.dumps({"action": "ERROR", "error": str(exc)}))
        return 1


def option_quote_bridge_args(position: dict[str, Any]) -> tuple[str, ...]:
    args = ["--option", str(position["occSymbol"])]
    if position.get("instrumentId"):
        args.extend(["--instrument-id", str(position["instrumentId"])])
    return tuple(args)


def _monitor_locked(symbol: str = SYMBOL) -> int:
    state = load_state(symbol); reset_daily(state, symbol=symbol); now = now_et(); positions = state["activePositions"]
    if not positions:
        print(json.dumps({"action": "NO_POSITION", "symbol": symbol})); save_state(state, symbol); return 0
    pos = positions[0]
    quote: dict[str, Any] | None = None
    quote_at = now_et()
    quote_refresh_attempted = False
    quote_refresh_error: str | None = None
    try:
        quote = bridge(*option_quote_bridge_args(pos))
        quote_at = now_et()
        bid, quote_reason = validate_exit_quote(
            quote,
            expected_occ_symbol=pos["occSymbol"],
            now=quote_at,
        )
        if bid is None and quote_reason == "stale_quote" and stale_exit_quote_refresh_allowed(pos["occSymbol"]):
            quote_refresh_attempted = True
            try:
                refreshed_quote = refresh_option_quote(*option_quote_bridge_args(pos))
            except Exception as refresh_exc:
                quote_refresh_error = str(refresh_exc)[:200]
            else:
                quote = refreshed_quote
                quote_at = now_et()
                bid, quote_reason = validate_exit_quote(
                    quote,
                    expected_occ_symbol=pos["occSymbol"],
                    now=quote_at,
                )
        if bid is None:
            raise RuntimeError(f"invalid_exit_quote:{quote_reason}")
        # Compute display-only quote/P&L fields before the duplicate-timestamp
        # guard. A repeated venue timestamp must HOLD without advancing peak or
        # evaluating exits, but it still needs a complete lastQuote payload.
        ask = float(quote["ask"])
        now = quote_at
        entry = float(pos["entryAsk"])
        pnl = (bid - entry) * 100
        pnl_pct = (bid / entry - 1) * 100
        held_minutes = (now - datetime.fromisoformat(pos["entryTime"]).astimezone(ET)).total_seconds() / 60
        tm = now.hour * 100 + now.minute
        # Time-based exits are independent of market-data novelty. A duplicate
        # venue timestamp may suppress a new price tick, but never a hard-close
        # or max-hold exit for an already-open position.
        max_hold = dynamic_max_hold_minutes(pos)
        time_exit_due = hard_close_due(now, symbol) or held_minutes >= max_hold
        # Stale venue timestamp guard: if the option quote timestamp is identical
        # to the last one, the market data hasn't updated — don't treat it as a new tick.
        opt_ts = quote.get("timestamp")
        occ = pos["occSymbol"]
        if not time_exit_due and isinstance(opt_ts, str) and opt_ts:
            if _LAST_OPTION_VENUE_TS.get(occ) == opt_ts:
                pos["lastQuote"] = {"bid": bid, "ask": ask, "pnl": round(pnl, 2), "pnlPct": round(pnl_pct, 2),
                                    "at": now.isoformat(), "staleVenue": True,
                                    "quoteTimestamp": opt_ts,
                                    "quoteAgeSeconds": quote_freshness_seconds(quote, now=now),
                                    "quoteSnapshot": quote.get("_snapshotPath")}
                # Preserve an in-flight soft-exit confirmation. A duplicate venue
                # timestamp is not a new price tick, so it must neither advance
                # confirmation nor erase confirmation already earned on a prior tick.
                state["activePositions"] = [pos]; save_state(state, symbol)
                print(json.dumps({"action": "HOLD", "occSymbol": occ, "staleVenue": True, **pos["lastQuote"]}, indent=2))
                return 0
            _LAST_OPTION_VENUE_TS[occ] = opt_ts
        pos["peakBid"] = max(float(pos.get("peakBid", bid)), bid)
        peak_pct = (pos["peakBid"] / entry - 1) * 100
        profit_target_pct = profit_target_pct_for_position(pos)
        stop_loss_pct = stop_loss_pct_for_position(pos)
        # Dynamic exit thresholds driven by entry signal conviction.
        # Strong entries (high score, many checks, high volume) get more room;
        # weak entries are cut quickly — consistent with the entry thesis.
        trail_start = dynamic_trail_start_pct(pos)
        trail_giveback = dynamic_trail_giveback_pct(pos)
        breakeven_hold = dynamic_breakeven_hold_minutes(pos)
        reason = price_exit_reason(
            pos,
            bid,
            now=now,
            peak_bid=pos["peakBid"],
            entry_price=entry,
            symbol=symbol,
        )
        if reason is None and held_minutes >= MIN_HOLD_MINUTES:
            # Underlying invalidation: compare against ENTRY VWAP, not current VWAP.
            # Current VWAP drifts intraday; using it causes premature exits on normal
            # retests. Entry VWAP is the structural level that justified the entry.
            bars = fetch_bars(symbol)
            data_now = now_et()
            day = completed_day_bars(bars, now=data_now, symbol=symbol)
            bars_fresh, _bars_reason = validate_completed_bar_freshness(day, now=data_now, symbol=symbol)
            if not bars_fresh:
                day = []
            warmup_bars, refreshed_relative_volume, _, premarket_context = session_signal_inputs(
                bars, day, now=data_now, symbol=symbol,
            )
            reversal_reason = signal_reversal_reason(
                pos,
                day,
                indicator_bars=warmup_bars + day,
            )
            if reversal_reason:
                reason = reversal_reason
            else:
                fresh = signal(
                    day,
                    warmup_bars=warmup_bars,
                    relative_volume=refreshed_relative_volume,
                    premarket_context=premarket_context,
                    symbol=symbol,
                )
                # Guard: signal() returns a minimal dict when < 35 bars. Skip structural
                # exits in that case rather than crashing with KeyError.
                if "vwap" not in fresh or "close" not in fresh:
                    pass  # Not enough bars for VWAP/turtle invalidation; rely on other exits
                else:
                    entry_vwap = float(pos.get("entryVwap", 0.0))
                    reference_vwap = entry_vwap if entry_vwap > 0 else fresh["vwap"]
                    buf = reference_vwap * SIGNAL_REVERSAL_VWAP_BUFFER_PCT / 100.0
                    turtle = fresh.get("turtle", {})
                    turtle_exit = ((pos["direction"] == "bullish" and fresh["close"] < turtle.get("exitLow", float("-inf"))) or
                                   (pos["direction"] == "bearish" and fresh["close"] > turtle.get("exitHigh", float("inf"))))
                    if turtle_exit:
                        reason = "turtle_opposite_breakout"
                    else:
                        prev = signal(
                            day[:-1],
                            warmup_bars=warmup_bars,
                            relative_volume=refreshed_relative_volume,
                            premarket_context=premarket_context,
                            symbol=symbol,
                        ) if len(day) >= 2 else fresh
                        if "vwap" not in prev or "close" not in prev:
                            pass  # Previous bar signal insufficient; skip VWAP invalidation
                        else:
                            both_above = fresh["close"] > reference_vwap + buf and prev["close"] > reference_vwap + buf
                            both_below = fresh["close"] < reference_vwap - buf and prev["close"] < reference_vwap - buf
                            if (pos["direction"] == "bullish" and both_below) or \
                               (pos["direction"] == "bearish" and both_above):
                                reason = "vwap_invalidation"
        if reason:
            # 2-tick confirmation for soft exit reasons (stop_loss, breakeven_stop,
            # trailing_stop) to filter bid flicker. Hard exits fire immediately.
            if reason in SOFT_EXIT_REASONS:
                pending = pos.get("pendingExitReason")
                confirm_count = pos.get("exitConfirmCount", 0) + 1
                required_ticks = MIN_CONFIRMATION_TICKS
                if pending != reason or confirm_count < required_ticks:
                    pos["pendingExitReason"] = reason
                    pos["exitConfirmCount"] = confirm_count
                    state["activePositions"] = [pos]
                    save_state(state, symbol)
                    print(json.dumps({"action": "HOLD", "occSymbol": pos["occSymbol"],
                                      "pendingExit": reason, "exitConfirmCount": confirm_count,
                                      "exitConfirmRequired": required_ticks,
                                      **pos.get("lastQuote", {}), "at": now.isoformat()}, indent=2))
                    return 0
                # Confirmed: fall through to execute the exit.
            # Clear any pending soft exit if a hard reason overrides or on execution.
            pos.pop("pendingExitReason", None)
            pos.pop("exitConfirmCount", None)
            exit_event = {"event": "PAPER_EXIT_REAL_BID", "positionId": pos["positionId"], "occSymbol": pos["occSymbol"],
                          "reason": reason, "entryAsk": entry, "exitBid": bid, "exitAskAtCheck": ask,
                          "pnl": round(pnl, 2), "pnlPct": round(pnl_pct, 2), "heldMinutes": round(held_minutes, 1),
                          "dynamicThresholds": {
                              "signalStrength": round(_entry_signal_strength(pos), 3),
                              "breakevenHoldMin": round(breakeven_hold, 1),
                              "maxHoldMin": round(max_hold, 1),
                              "trailStartPct": round(trail_start, 1),
                              "trailGivebackPct": round(trail_giveback, 1),
                              "breakevenExitMinHold": MIN_HOLD_MINUTES,
                              "breakevenExitPnlPct": -3.0,
                          },
                          "quoteSource": "Robinhood MCP", "quoteTimestamp": quote.get("timestamp"),
                          "quoteAgeSeconds": quote_freshness_seconds(quote, now=now),
                          "quoteSnapshot": quote.get("_snapshotPath"),
                          "engineVersion": ENGINE_VERSION, "strategyId": STRATEGY_ID,
                          "sourceCodeSha256": source_code_sha256(), "configSha256": config_sha256()}
            log(exit_event, symbol); state["activePositions"] = []; state["lastExitAt"] = now.isoformat()
            state["dailyPnl"] = round(state["dailyPnl"] + pnl, 2)
            state["dailyLoss"] = round(max(0.0, -state["dailyPnl"]), 2)
            # Track consecutive losses for cooldown gate
            if pnl < 0:
                state["consecutiveLosses"] = state.get("consecutiveLosses", 0) + 1
                state["dailyGrossLoss"] = round(state.get("dailyGrossLoss", 0.0) + abs(pnl), 2)
            else:
                state["consecutiveLosses"] = 0
            record_exit_reentry_state(state, pos, pnl, now)
            save_state(state, symbol)
            print(json.dumps({"action": "PAPER_EXIT", **exit_event}, indent=2)); return 0
        pos["lastQuote"] = {"bid": bid, "ask": ask, "pnl": round(pnl, 2), "pnlPct": round(pnl_pct, 2),
                            "at": now.isoformat(), "quoteTimestamp": quote.get("timestamp"),
                            "quoteAgeSeconds": quote_freshness_seconds(quote, now=now),
                            "quoteSnapshot": quote.get("_snapshotPath")}
        # No exit reason this tick — clear any stale soft-exit confirmation.
        pos.pop("pendingExitReason", None)
        pos.pop("exitConfirmCount", None)
        # Diagnostic: log when time_exit_due is True but no exit reason was set.
        # This catches the bug where a position should exit but the monitor logic doesn't trigger.
        if time_exit_due and not reason:
            log({"event": "MONITOR_ANOMALY", "positionId": pos.get("positionId"), "symbol": symbol,
                 "anomaly": "time_exit_due_but_no_exit_reason", "heldMinutes": round(held_minutes, 1),
                 "maxHold": round(max_hold, 1), "pnlPct": round(pnl_pct, 2), "bid": bid, "entry": entry,
                 "stopLossPct": stop_loss_pct, "profitTargetPct": profit_target_pct,
                 "peakPct": round(peak_pct, 2), "pendingExit": pos.get("pendingExitReason"),
                 "confirmCount": pos.get("exitConfirmCount")}, symbol)
        state["activePositions"] = [pos]; save_state(state, symbol)
        print(json.dumps({"action": "HOLD", "occSymbol": pos["occSymbol"], **pos["lastQuote"]}, indent=2)); return 0
    except Exception as exc:
        stale_quote_failure = str(exc) == "invalid_exit_quote:stale_quote"
        if stale_quote_failure:
            log({
                "event": "MONITOR_STALE_QUOTE",
                "positionId": pos.get("positionId"),
                "symbol": symbol,
                "quoteTimestamp": quote.get("timestamp") if quote else None,
                "quoteAgeSeconds": quote_freshness_seconds(quote or {}, now=now_et()),
                "quoteRefreshAttempted": quote_refresh_attempted,
                "quoteRefreshError": quote_refresh_error,
            }, symbol)
        else:
            log({"event": "MONITOR_QUOTE_ERROR", "positionId": pos.get("positionId"), "error": str(exc)}, symbol)
        # QUOTE BRIDGE FAILURE FALLBACK: When the quote bridge is down, we still
        # must process time-based exits (hard_close, time_stop) and catastrophic
        # stop-loss using the last known bid. Without this, a position can be held
        # indefinitely past max_hold while the monitor fails to get fresh quotes.
        # Bug: paper-20260827T115006 was held 43.8 min (12 min past 31.8 min max_hold)
        # because MCP dispatch failures prevented the monitor from getting quotes.
        now = now_et()
        entry = float(pos.get("entryAsk", 0))
        held_minutes = (now - datetime.fromisoformat(pos["entryTime"]).astimezone(ET)).total_seconds() / 60
        max_hold = dynamic_max_hold_minutes(pos)
        time_exit_due = hard_close_due(now, symbol) or held_minutes >= max_hold
        # Use last known bid from the position's lastQuote, or a conservative fallback
        last_quote = pos.get("lastQuote") or {}
        fallback_bid = float(last_quote.get("bid", entry * 0.85))  # assume -15% if no quote
        fallback_pnl = round((fallback_bid - entry) * 100, 2)
        fallback_pnl_pct = round((fallback_bid / entry - 1) * 100, 2) if entry > 0 else -100.0
        stop_loss_pct = stop_loss_pct_for_position(pos)
        catastrophic_loss = fallback_pnl_pct <= -stop_loss_pct * 2  # double the stop-loss threshold
        if time_exit_due or catastrophic_loss:
            reason = "hard_close" if hard_close_due(now, symbol) else ("time_stop" if held_minutes >= max_hold else "stop_loss_fallback")
            log({"event": "QUOTE_FAILURE_EXIT", "positionId": pos.get("positionId"), "symbol": symbol,
                 "reason": reason, "heldMinutes": round(held_minutes, 1), "maxHold": round(max_hold, 1),
                 "fallbackBid": fallback_bid, "fallbackPnl": fallback_pnl, "fallbackPnlPct": fallback_pnl_pct,
                 "quoteError": str(exc)[:200]}, symbol)
            # Execute the exit with fallback bid
            exit_event = {"event": "PAPER_EXIT_REAL_BID", "positionId": pos["positionId"], "occSymbol": pos["occSymbol"],
                          "reason": reason, "entryAsk": entry, "exitBid": fallback_bid, "exitAskAtCheck": fallback_bid,
                          "pnl": fallback_pnl, "pnlPct": fallback_pnl_pct, "heldMinutes": round(held_minutes, 1),
                          "dynamicThresholds": {
                              "signalStrength": round(_entry_signal_strength(pos), 3),
                              "breakevenHoldMin": round(dynamic_breakeven_hold_minutes(pos), 1),
                              "maxHoldMin": round(max_hold, 1),
                              "trailStartPct": round(dynamic_trail_start_pct(pos), 1),
                              "trailGivebackPct": round(dynamic_trail_giveback_pct(pos), 1),
                          },
                          "quoteSource": "fallback_last_known_bid", "quoteError": str(exc)[:100],
                          "engineVersion": ENGINE_VERSION, "strategyId": STRATEGY_ID,
                          "sourceCodeSha256": source_code_sha256(), "configSha256": config_sha256()}
            log(exit_event, symbol); state["activePositions"] = []; state["lastExitAt"] = now.isoformat()
            state["dailyPnl"] = round(state["dailyPnl"] + fallback_pnl, 2)
            state["dailyLoss"] = round(max(0.0, -state["dailyPnl"]), 2)
            if fallback_pnl < 0:
                state["consecutiveLosses"] = state.get("consecutiveLosses", 0) + 1
                state["dailyGrossLoss"] = round(state.get("dailyGrossLoss", 0.0) + abs(fallback_pnl), 2)
            else:
                state["consecutiveLosses"] = 0
            record_exit_reentry_state(state, pos, fallback_pnl, now)
            save_state(state, symbol)
            print(json.dumps({"action": "PAPER_EXIT", **exit_event}, indent=2)); return 0
        if stale_quote_failure:
            # A stale provider timestamp is not executable pricing. Keep the
            # last trusted bid/ask for diagnostics, mark the quote unavailable,
            # and let the next normal monitor tick retry without emitting an
            # ERROR or advancing price-driven exit state.
            stale_last_quote = dict(last_quote)
            stale_last_quote.update({
                "at": now.isoformat(),
                "staleQuote": True,
                "quoteTimestamp": quote.get("timestamp") if quote else stale_last_quote.get("quoteTimestamp"),
                "quoteAgeSeconds": quote_freshness_seconds(quote or {}, now=now) if quote else None,
                "quoteRefreshAttempted": quote_refresh_attempted,
            })
            if quote_refresh_error:
                stale_last_quote["quoteRefreshError"] = quote_refresh_error
            pos["lastQuote"] = stale_last_quote
            state["activePositions"] = [pos]
            save_state(state, symbol)
            print(json.dumps({
                "action": "HOLD",
                "occSymbol": pos["occSymbol"],
                "staleQuote": True,
                "quoteReason": "stale_quote",
                "quoteRefreshAttempted": quote_refresh_attempted,
                "quoteTimestamp": stale_last_quote.get("quoteTimestamp"),
                "quoteAgeSeconds": stale_last_quote.get("quoteAgeSeconds"),
                "quoteRefreshError": quote_refresh_error,
                "lastTrustedBid": stale_last_quote.get("bid"),
            }, indent=2))
            return 0
        # No time-based exit due and no catastrophic loss — log the error and hold
        print(json.dumps({"action": "ERROR", "error": str(exc)})); save_state(state, symbol); return 1


def _report_locked(symbol: str = SYMBOL) -> int:
    # Reports are read-only consumers. The daemon owns daily resets and state
    # persistence; never bump the revision or compete as a second writer here.
    state = load_state(symbol)
    reset_daily(state, symbol=symbol)
    print(json.dumps({"date": trading_session_key(symbol), "mode": "dry_run", "symbol": symbol,
                      "engineVersion": ENGINE_VERSION, "strategyId": STRATEGY_ID,
                      "releaseManifestSha256": RELEASE_MANIFEST_SHA256,
                      "sourceCodeSha256": source_code_sha256(), "configSha256": config_sha256(),
                      "scope": "current symbol state snapshot; not version-cohort performance",
                      "dailyTrades": state["dailyTrades"],
                      "dailyPnl": state["dailyPnl"], "dailyLoss": state["dailyLoss"],
                      "dailyGrossLoss": state.get("dailyGrossLoss", 0.0),
                      "consecutiveLosses": state.get("consecutiveLosses", 0),
                      "openPositions": len(state["activePositions"]), "liveOrdersEnabled": False}, indent=2))
    return 0


def _run_state_locked(command: str, callback, symbol: str = SYMBOL) -> int:
    try:
        with exclusive_state_lock(symbol):
            return callback(symbol)
    except StateLockBusy:
        print(json.dumps({"action": "SKIP", "reason": "concurrent_state_owner", "command": command, "symbol": symbol}))
        return 0
    except StateCorruptionError as exc:
        print(json.dumps({"action": "ERROR", "reason": "state_corruption", "error": str(exc), "command": command, "symbol": symbol}))
        return 2


def scan(symbol: str = SYMBOL) -> int:
    try:
        return _scan_transaction(symbol)
    except StateLockBusy:
        print(json.dumps({"action": "SKIP", "reason": "concurrent_state_owner", "command": "scan", "symbol": symbol}))
        return 0
    except StateCorruptionError as exc:
        print(json.dumps({"action": "ERROR", "reason": "state_corruption", "error": str(exc), "command": "scan", "symbol": symbol}))
        return 2


def monitor(symbol: str = SYMBOL) -> int:
    return _run_state_locked("monitor", _monitor_locked, symbol)

def report(symbol: str = SYMBOL) -> int:
    return _run_state_locked("report", _report_locked, symbol)


def main() -> int:
    parser = argparse.ArgumentParser(description=f"V{ENGINE_VERSION} multi-symbol paper-only 1DTE scalp")
    parser.add_argument("command", choices=["scan", "monitor", "report"])
    parser.add_argument("--symbol", default=SYMBOL, help="Symbol to scan/monitor (default: SPY)")
    args = parser.parse_args()
    return {"scan": scan, "monitor": monitor, "report": report}[args.command](args.symbol)

if __name__ == "__main__":
    raise SystemExit(main())
