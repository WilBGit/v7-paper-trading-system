#!/usr/bin/env python3
"""Read-only opportunity replay for the exact active V7.3 paper signal code.

This intentionally stops before option selection, quotes, state, cooldown, and risk
controls. It measures qualified SPY signal episodes, not executable trades or P&L.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import v7_scalp_paper as engine

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/SPY"
USER_AGENT = "Mozilla/5.0 (V7.3 read-only opportunity replay)"


def fetch_history(period: str = "60d") -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"range": period, "interval": "5m", "includePrePost": "false"})
    request = urllib.request.Request(f"{YAHOO_CHART}?{query}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(f"yahoo_chart_error:{chart['error']}")
    results = chart.get("result") or []
    if not results:
        raise RuntimeError("yahoo_chart_missing_result")
    result = results[0]
    timestamps = result.get("timestamp") or []
    quote_blocks = ((result.get("indicators") or {}).get("quote") or [])
    if not quote_blocks:
        raise RuntimeError("yahoo_chart_missing_quotes")
    quote = quote_blocks[0]
    bars: list[dict[str, Any]] = []
    for index, stamp in enumerate(timestamps):
        values = {key: (quote.get(key) or [None] * len(timestamps))[index]
                  for key in ("open", "high", "low", "close", "volume")}
        if any(values[key] is None for key in ("open", "high", "low", "close")):
            continue
        when = datetime.fromtimestamp(stamp, engine.ET)
        bars.append({
            "dt": when,
            "date": when.strftime("%Y-%m-%d"),
            "time": when.hour * 100 + when.minute,
            "open": float(values["open"]),
            "high": float(values["high"]),
            "low": float(values["low"]),
            "close": float(values["close"]),
            "volume": float(values["volume"] or 0.0),
        })
    if not bars:
        raise RuntimeError("yahoo_chart_no_usable_bars")
    return bars


def _clock_minutes(value: str | int) -> int:
    if isinstance(value, int):
        return (value // 100) * 60 + value % 100
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def is_eligible_observation(when: datetime) -> bool:
    minute = when.hour * 60 + when.minute
    return (_clock_minutes(engine.ENTRY_START) <= minute <= _clock_minutes(engine.ENTRY_END)
            and not _clock_minutes(engine.LUNCH_START) <= minute <= _clock_minutes(engine.LUNCH_END))


def primary_signal_blocker(setup: dict[str, Any]) -> str:
    score = setup.get("score")
    relative_volume = setup.get("relativeVolume")
    if not isinstance(score, (int, float)):
        return "insufficient_bars_or_no_detector"
    if score < engine.MIN_ENTRY_SCORE:
        return "score_below_gate"
    if not isinstance(relative_volume, (int, float)) or relative_volume < engine.MIN_RELATIVE_VOLUME:
        return "relative_volume_below_gate"
    reasons = setup.get("rejectionReasons") or []
    if reasons:
        return str(reasons[0])
    return "direction_not_qualified"


def group_opportunities(rows: list[dict[str, Any]], *, max_gap_minutes: int = 15) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: datetime.fromisoformat(str(row["observableAt"])))
    episodes: list[dict[str, Any]] = []
    for row in ordered:
        observed = datetime.fromisoformat(str(row["observableAt"]))
        row_detector = row.get("detector") or "normal"
        extend = False
        if episodes:
            previous = episodes[-1]
            previous_end = datetime.fromisoformat(previous["end"])
            extend = (
                previous["date"] == row["date"]
                and previous["direction"] == row["direction"]
                and previous.get("detector") == row_detector
                and observed - previous_end <= timedelta(minutes=max_gap_minutes)
            )
        if extend:
            episode = episodes[-1]
            episode["end"] = observed.isoformat()
            episode["barCount"] += 1
            episode["maxScore"] = max(episode["maxScore"], float(row.get("score") or 0))
            episode["maxRelativeVolume"] = max(
                episode["maxRelativeVolume"], float(row.get("relativeVolume") or 0),
            )
            episode["signalBars"].append(row.get("signalBarStart", row["observableAt"]))
            continue
        fingerprint = "|".join([
            str(row["date"]), str(row["direction"]), row_detector,
            str(row["observableAt"]), engine.config_sha256(),
        ])
        episodes.append({
            "episodeId": hashlib.sha256(fingerprint.encode()).hexdigest()[:20],
            "date": row["date"],
            "direction": row["direction"],
            "detector": row_detector,
            "start": observed.isoformat(),
            "end": observed.isoformat(),
            "barCount": 1,
            "maxScore": float(row.get("score") or 0),
            "maxRelativeVolume": float(row.get("relativeVolume") or 0),
            "signalBars": [row.get("signalBarStart", row["observableAt"])],
        })
    return episodes


def replay_convention(
    bars: list[dict[str, Any]], *, completed_only: bool,
) -> dict[str, Any]:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bar in bars:
        by_date[bar["date"]].append(bar)
    qualified: list[dict[str, Any]] = []
    blockers: Counter[str] = Counter()
    eligible_scans = 0
    for session_date in sorted(by_date):
        day = sorted(by_date[session_date], key=lambda bar: bar["dt"])
        for index, bar in enumerate(day):
            observable_at = bar["dt"] + (timedelta(minutes=engine.BAR_INTERVAL_MINUTES)
                                         if completed_only else timedelta())
            if not is_eligible_observation(observable_at):
                continue
            eligible_scans += 1
            sample = day[:index + 1]
            setup = engine.detect_setup_for_recheck(sample)
            if setup.get("direction"):
                qualified.append({
                    "date": session_date,
                    "observableAt": observable_at.isoformat(),
                    "signalBarStart": bar["dt"].isoformat(),
                    "signalBarEnd": (bar["dt"] + timedelta(minutes=engine.BAR_INTERVAL_MINUTES)).isoformat(),
                    "direction": setup["direction"],
                    "detector": setup.get("detector", "normal"),
                    "score": setup.get("score"),
                    "relativeVolume": setup.get("relativeVolume"),
                    "rsi": setup.get("rsi"),
                    "rejectionReasons": setup.get("rejectionReasons") or [],
                })
            else:
                blockers[primary_signal_blocker(setup)] += 1
    episodes = group_opportunities(qualified)
    daily = {date: {"qualifyingBars": 0, "episodes": 0} for date in sorted(by_date)}
    for row in qualified:
        daily[row["date"]]["qualifyingBars"] += 1
    for episode in episodes:
        daily[episode["date"]]["episodes"] += 1
    session_count = len(by_date)
    return {
        "convention": "completed_bar" if completed_only else "same_label_final_bar_lookahead",
        "sessions": session_count,
        "eligibleScans": eligible_scans,
        "qualifyingBars": len(qualified),
        "episodes": len(episodes),
        "episodesPerSession": round(len(episodes) / session_count, 4) if session_count else 0.0,
        "sessionsWithOpportunity": sum(1 for values in daily.values() if values["episodes"]),
        "maximumEpisodesInSession": max((values["episodes"] for values in daily.values()), default=0),
        "primaryBlockers": dict(blockers.most_common()),
        "daily": daily,
        "opportunities": qualified,
        "episodeDetails": episodes,
    }


def build_report(period: str = "60d") -> dict[str, Any]:
    bars = fetch_history(period)
    sessions = sorted({bar["date"] for bar in bars})
    return {
        "generatedAt": datetime.now(engine.ET).isoformat(),
        "mode": "read_only_signal_opportunity_replay",
        "symbol": engine.SYMBOL,
        "periodRequested": period,
        "firstSession": sessions[0],
        "lastSession": sessions[-1],
        "sessions": len(sessions),
        "bars": len(bars),
        "engineVersion": engine.ENGINE_VERSION,
        "strategyId": engine.STRATEGY_ID,
        "sourceCodeSha256": engine.source_code_sha256(),
        "configSha256": engine.config_sha256(),
        "limits": {
            "scoreAtLeast": engine.MIN_ENTRY_SCORE,
            "relativeVolumeAtLeast": engine.MIN_RELATIVE_VOLUME,
            "entryWindow": [engine.ENTRY_START, engine.ENTRY_END],
            "lunchBlock": [engine.LUNCH_START, engine.LUNCH_END],
            "episodeGapMinutes": 15,
        },
        "scopeWarning": (
            "Signal opportunities only. Counts precede option identity, quote freshness, spread, "
            "debit, OTM, open-position, cooldown, macro-calendar, and daily-risk controls."
        ),
        "sameLabelFinalBar": replay_convention(bars, completed_only=False),
        "completedBar": replay_convention(bars, completed_only=True),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", default="60d")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_report(args.period)
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
