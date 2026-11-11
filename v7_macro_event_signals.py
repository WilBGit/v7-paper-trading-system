#!/usr/bin/env python3
"""Deterministic, paper-only macro-event signal rules for SPY and QQQ.

This module is intentionally pure: it consumes caller-supplied event and quote
snapshots and never contacts a provider, broker, scheduler, or state store.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import math
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

try:
    from v7_rh_quote_bridge import is_market_holiday
except ImportError:  # pragma: no cover - the V7 scripts directory supplies it
    def is_market_holiday(day: date) -> bool:
        """Fail closed when the shared market calendar cannot be imported."""
        raise RuntimeError("market calendar unavailable")


ET = ZoneInfo("America/New_York")
EVENT_FAMILIES = ("CPI", "PPI", "FOMC_DECISION", "FOMC_MINUTES")
SYMBOLS = ("SPY", "QQQ")

DEFAULTS: dict[str, float | int] = {
    "range_seconds": 60.0,
    "min_range_samples": 6,
    "range_early_seconds": 5.0,
    "range_late_seconds": 55.0,
    "max_range_gap_seconds": 15.0,
    "evaluation_delay_seconds": 65.0,
    "confirmation_seconds": 5.0,
    "breakout_bps": 2.0,
    "chase_bps": 15.0,
    "max_spread_bps": 5.0,
    "max_quote_future_seconds": 1.0,
    "max_quote_stale_seconds": 5.0,
    "confirmation_lookback_seconds": 30.0,
    "fomc_decision_deadline_minutes": 25,
    "other_event_deadline_minutes": 30,
    "entry_cutoff_hour": 15,
    "entry_cutoff_minute": 30,
}

_OFFICIAL_HOSTS = {
    "CPI": ("bls.gov",),
    "PPI": ("bls.gov",),
    "FOMC_DECISION": ("federalreserve.gov",),
    "FOMC_MINUTES": ("federalreserve.gov",),
}


def _parse_aware(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}_invalid")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field}_naive")
    return parsed


def _official_source(family: str, source_url: Any) -> bool:
    if not isinstance(source_url, str) or not source_url:
        return False
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or not host or parsed.username or parsed.password:
        return False
    return any(host == root or host.endswith("." + root) for root in _OFFICIAL_HOSTS[family])


def _validate_event(event: Mapping[str, Any]) -> tuple[str, datetime]:
    if not isinstance(event, Mapping):
        raise ValueError("event_invalid")
    family = event.get("family")
    if family not in EVENT_FAMILIES:
        raise ValueError("family_unknown")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id_invalid")
    release_at = _parse_aware(event.get("release_at"), "release_at")
    _parse_aware(event.get("verified_at"), "verified_at")
    if not _official_source(family, event.get("source_url")):
        raise ValueError("source_url_unofficial")
    return event_id, release_at


def _event_window_with_config(
    event: Mapping[str, Any],
    cfg: Mapping[str, float | int],
) -> tuple[datetime, datetime]:
    _, release_at = _validate_event(event)
    release_et = release_at.astimezone(ET)
    session_day = release_et.date()
    if session_day.weekday() >= 5:
        raise ValueError("market_closed")
    try:
        holiday = is_market_holiday(session_day)
    except Exception as exc:
        raise ValueError("market_calendar_unavailable") from exc
    if holiday:
        raise ValueError("market_closed")

    market_open = datetime.combine(session_day, time(9, 30), tzinfo=ET)
    start = max(release_et, market_open)
    deadline_minutes = (
        int(cfg["fomc_decision_deadline_minutes"])
        if event["family"] == "FOMC_DECISION"
        else int(cfg["other_event_deadline_minutes"])
    )
    event_deadline = start + timedelta(minutes=deadline_minutes)
    cutoff = datetime.combine(
        session_day,
        time(int(cfg["entry_cutoff_hour"]), int(cfg["entry_cutoff_minute"])),
        tzinfo=ET,
    )
    end = min(event_deadline, cutoff)
    if end <= start:
        raise ValueError("entry_window_closed")
    return start, end


def event_window(event: Mapping[str, Any]) -> tuple[datetime, datetime]:
    """Return the ET event start and paper-entry deadline.

    CPI/PPI releases before the regular session begin at 09:30 ET. FOMC
    decisions receive a 25-minute deadline; the other families receive 30
    minutes. Every deadline is capped at the 15:30 ET entry cutoff.

    ``ValueError`` is fail-closed behavior for malformed, unofficial, weekend,
    holiday, or outside-session events.
    """
    return _event_window_with_config(event, DEFAULTS)


@dataclass(frozen=True)
class _Quote:
    timestamp: datetime
    bid: float
    ask: float
    mid: float


def _finite_positive(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field}_invalid")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field}_invalid")
    return number


def _config(config: Mapping[str, Any] | None) -> dict[str, float | int]:
    if config is None:
        return dict(DEFAULTS)
    if not isinstance(config, Mapping):
        raise ValueError("config_invalid")
    unknown = set(config) - set(DEFAULTS)
    if unknown:
        raise ValueError("config_unknown")
    merged: dict[str, float | int] = dict(DEFAULTS)
    integer_keys = {
        "min_range_samples",
        "fomc_decision_deadline_minutes",
        "other_event_deadline_minutes",
        "entry_cutoff_hour",
        "entry_cutoff_minute",
    }
    for key, value in config.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("config_invalid")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("config_invalid")
        if key in integer_keys and int(number) != number:
            raise ValueError("config_invalid")
        merged[key] = int(number) if key in integer_keys else number

    if (
        merged["range_seconds"] <= 0
        or merged["min_range_samples"] < 2
        or not 0 <= merged["range_early_seconds"] < merged["range_late_seconds"] <= merged["range_seconds"]
        or merged["max_range_gap_seconds"] <= 0
        or merged["evaluation_delay_seconds"] < merged["range_seconds"]
        or merged["confirmation_seconds"] <= 0
        or merged["breakout_bps"] <= 0
        or merged["chase_bps"] < merged["breakout_bps"]
        or merged["max_spread_bps"] <= 0
        or merged["max_quote_future_seconds"] < 0
        or merged["max_quote_stale_seconds"] < 0
        or merged["confirmation_lookback_seconds"] < merged["confirmation_seconds"]
        or merged["fomc_decision_deadline_minutes"] <= 0
        or merged["other_event_deadline_minutes"] <= 0
        or not 0 <= merged["entry_cutoff_hour"] <= 23
        or not 0 <= merged["entry_cutoff_minute"] <= 59
    ):
        raise ValueError("config_invalid")
    return merged


def _safe_event_id(event: Any) -> Any:
    return event.get("event_id") if isinstance(event, Mapping) else None


def _decision(
    action: str,
    reason: str,
    event_id: Any,
    evidence: Mapping[str, Any] | None = None,
    *,
    direction: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "action": action,
        "reason": reason,
        "event_id": event_id,
        "evidence": dict(evidence or {}),
    }
    if direction is not None:
        result["direction"] = direction
        result["symbol"] = "QQQ"
    return result


def _quote_histories(
    histories: Mapping[str, Any],
    now: datetime,
    cfg: Mapping[str, float | int],
) -> dict[str, list[_Quote]]:
    if not isinstance(histories, Mapping) or set(histories) != set(SYMBOLS):
        raise ValueError("symbols_invalid")
    now_utc = now.astimezone(timezone.utc)
    parsed: dict[str, list[_Quote]] = {}
    for symbol in SYMBOLS:
        rows = histories.get(symbol)
        if not isinstance(rows, (list, tuple)) or not rows:
            raise ValueError("history_invalid")
        quotes: list[_Quote] = []
        previous: datetime | None = None
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("quote_invalid")
            timestamp = _parse_aware(row.get("timestamp"), "timestamp")
            timestamp_utc = timestamp.astimezone(timezone.utc)
            if previous is not None and timestamp_utc <= previous:
                raise ValueError("history_not_strictly_increasing")
            if timestamp_utc - now_utc > timedelta(seconds=float(cfg["max_quote_future_seconds"])):
                raise ValueError("quote_future")
            bid = _finite_positive(row.get("bid"), "bid")
            ask = _finite_positive(row.get("ask"), "ask")
            if ask < bid:
                raise ValueError("quote_crossed")
            mid = (bid + ask) / 2.0
            spread_bps = (ask - bid) / mid * 10000.0
            if not math.isfinite(spread_bps) or spread_bps > float(cfg["max_spread_bps"]):
                raise ValueError("quote_spread_wide")
            quotes.append(_Quote(timestamp, bid, ask, mid))
            previous = timestamp_utc
        if now_utc - quotes[-1].timestamp.astimezone(timezone.utc) > timedelta(
            seconds=float(cfg["max_quote_stale_seconds"])
        ):
            raise ValueError("quote_stale")
        parsed[symbol] = quotes
    return parsed


def _opening_range(
    quotes: list[_Quote],
    start: datetime,
    cfg: Mapping[str, float | int],
) -> tuple[datetime, float, float, list[_Quote]]:
    range_end = start + timedelta(seconds=float(cfg["range_seconds"]))
    opening = [quote for quote in quotes if start <= quote.timestamp <= range_end]
    if len(opening) < int(cfg["min_range_samples"]):
        raise ValueError("initial_range_incomplete")
    first_offset = (opening[0].timestamp - start).total_seconds()
    last_offset = (opening[-1].timestamp - start).total_seconds()
    if (
        first_offset > float(cfg["range_early_seconds"])
        or last_offset < float(cfg["range_late_seconds"])
    ):
        raise ValueError("initial_range_incomplete")
    if any(
        (right.timestamp - left.timestamp).total_seconds() > float(cfg["max_range_gap_seconds"])
        for left, right in zip(opening, opening[1:])
    ):
        raise ValueError("initial_range_gap")
    low = min(quote.mid for quote in opening)
    high = max(quote.mid for quote in opening)
    return range_end, low, high, opening


def _confirmation(
    quotes: list[_Quote],
    range_end: datetime,
    low: float,
    high: float,
    cfg: Mapping[str, float | int],
) -> tuple[str | None, _Quote | None, _Quote | None]:
    post_range = [quote for quote in quotes if quote.timestamp > range_end]
    if not post_range:
        return None, None, None
    latest = post_range[-1]
    breakout_fraction = float(cfg["breakout_bps"]) / 10000.0
    bullish_boundary = high * (1.0 + breakout_fraction)
    bearish_boundary = low * (1.0 - breakout_fraction)
    if latest.mid >= bullish_boundary:
        direction = "bullish"
        boundary = bullish_boundary
    elif latest.mid <= bearish_boundary:
        direction = "bearish"
        boundary = bearish_boundary
    else:
        return None, None, latest

    confirmation_start = latest.timestamp - timedelta(
        seconds=float(cfg["confirmation_lookback_seconds"])
    )
    bounded = [
        quote
        for quote in post_range
        if confirmation_start <= quote.timestamp <= latest.timestamp
    ]
    eligible_anchors = [
        quote
        for quote in bounded
        if (latest.timestamp - quote.timestamp).total_seconds()
        >= float(cfg["confirmation_seconds"])
        and (
            quote.mid >= boundary if direction == "bullish" else quote.mid <= boundary
        )
    ]
    if not eligible_anchors:
        return None, None, latest
    anchor = eligible_anchors[-1]
    for quote in bounded:
        if quote.timestamp < anchor.timestamp:
            continue
        remains_breakout = (
            quote.mid >= boundary if direction == "bullish" else quote.mid <= boundary
        )
        if not remains_breakout:
            return None, anchor, latest
    return direction, anchor, latest


def evaluate_signal(
    event: Mapping[str, Any],
    histories: Mapping[str, Any],
    now: datetime,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate one deterministic paper-only macro event signal.

    The function never fetches data. ``histories`` must contain strictly
    increasing venue timestamps for exactly SPY and QQQ. A signal is emitted
    only after a complete opening range and a bounded, five-second breakout
    confirmation agree across both symbols.
    """
    event_id = _safe_event_id(event)
    try:
        cfg = _config(config)
        _, release_at = _validate_event(event)
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now_invalid")
        start, end = _event_window_with_config(event, cfg)
    except ValueError as exc:
        return _decision("SKIP", str(exc), event_id)

    now_et = now.astimezone(ET)
    release_et = release_at.astimezone(ET)
    base_evidence = {
        "family": event["family"],
        "release_at": release_et.isoformat(),
        "event_window": {"start": start.isoformat(), "end": end.isoformat()},
    }
    if release_et > now_et:
        return _decision("WAIT", "pre_release", event_id, base_evidence)
    if now_et < start:
        return _decision("WAIT", "premarket", event_id, base_evidence)
    if now_et > end:
        return _decision("SKIP", "entry_window_closed", event_id, base_evidence)
    if now_et < start + timedelta(seconds=float(cfg["evaluation_delay_seconds"])):
        return _decision("WAIT", "range_not_ready", event_id, base_evidence)

    try:
        parsed = _quote_histories(histories, now, cfg)
        references: dict[str, dict[str, Any]] = {}
        directions: dict[str, str | None] = {}
        for symbol in SYMBOLS:
            range_end, low, high, opening = _opening_range(parsed[symbol], start, cfg)
            direction, anchor, latest = _confirmation(parsed[symbol], range_end, low, high, cfg)
            if latest is not None:
                chase_fraction = float(cfg["chase_bps"]) / 10000.0
                if latest.mid > high * (1.0 + chase_fraction) or latest.mid < low * (1.0 - chase_fraction):
                    raise ValueError("chasing")
            directions[symbol] = direction
            latest_quote = parsed[symbol][-1]
            references[symbol] = {
                "range_start": start.isoformat(),
                "range_end": range_end.isoformat(),
                "range_low": low,
                "range_high": high,
                "range_sample_count": len(opening),
                "latest_mid": latest_quote.mid,
                "latest_timestamp": latest_quote.timestamp.isoformat(),
                "confirmation_anchor_mid": anchor.mid if anchor is not None else None,
                "confirmation_anchor_timestamp": (
                    anchor.timestamp.isoformat() if anchor is not None else None
                ),
            }
    except ValueError as exc:
        return _decision("SKIP", str(exc), event_id, base_evidence)

    evidence = {
        **base_evidence,
        "directions": directions,
        "reference_prices": references,
        "latest_venue_timestamps": {
            symbol: references[symbol]["latest_timestamp"] for symbol in SYMBOLS
        },
    }
    if any(directions[symbol] is None for symbol in SYMBOLS):
        return _decision("WAIT", "breakout_not_confirmed", event_id, evidence)
    if directions["SPY"] != directions["QQQ"]:
        return _decision("SKIP", "cross_symbol_disagreement", event_id, evidence)
    return _decision(
        "SIGNAL",
        "confirmed_breakout",
        event_id,
        evidence,
        direction=directions["QQQ"],
    )
