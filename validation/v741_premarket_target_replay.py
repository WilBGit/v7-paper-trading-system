#!/usr/bin/env python3
"""Read-only replay for V7.4.1 premarket-target opening continuation.

This evaluates signal eligibility only. It does not quote options, mutate engine
state, place orders, or estimate P&L. Premarket context is frozen at 09:29 ET;
regular-session signals are observed only after each five-minute bar completes.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import v7_scalp_paper as engine

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = "Mozilla/5.0 (V7.4.1 premarket target replay)"
OPENING_BARS = 6


def fetch_history(symbol: str, period: str = "1mo") -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({
        "range": period,
        "interval": "5m",
        "includePrePost": "true",
        "events": "div,splits",
    })
    request = urllib.request.Request(
        f"{YAHOO_CHART.format(symbol=symbol)}?{query}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(f"{symbol}:yahoo_chart_error:{chart['error']}")
    results = chart.get("result") or []
    if not results:
        raise RuntimeError(f"{symbol}:yahoo_chart_missing_result")
    result = results[0]
    timestamps = result.get("timestamp") or []
    quote_blocks = ((result.get("indicators") or {}).get("quote") or [])
    if not quote_blocks:
        raise RuntimeError(f"{symbol}:yahoo_chart_missing_quotes")
    quote = quote_blocks[0]
    bars: list[dict[str, Any]] = []
    for index, stamp in enumerate(timestamps):
        values = {
            key: (quote.get(key) or [None] * len(timestamps))[index]
            for key in ("open", "high", "low", "close", "volume")
        }
        if any(values[key] is None for key in ("open", "high", "low", "close")):
            continue
        when = datetime.fromtimestamp(stamp, engine.ET)
        bars.append({
            "dt": when,
            "date": when.date().isoformat(),
            "time": when.hour * 100 + when.minute,
            "open": float(values["open"]),
            "high": float(values["high"]),
            "low": float(values["low"]),
            "close": float(values["close"]),
            "volume": float(values["volume"] or 0.0),
        })
    if not bars:
        raise RuntimeError(f"{symbol}:yahoo_chart_no_usable_bars")
    return sorted(bars, key=lambda bar: bar["dt"])


def blocker_for(setup: dict[str, Any], *, target_broken: bool) -> str:
    reasons = [str(value) for value in setup.get("rejectionReasons") or []]
    if not target_broken:
        return "premarket_target_not_broken"
    for reason in reasons:
        if "opening_breakout_already_extended" in reason:
            return "opening_breakout_already_extended"
        if "relative_volume_below" in reason:
            return "relative_volume_below_gate"
        if reason.startswith("rsi_") or "rsi_" in reason:
            return "rsi_extreme"
        if "chase_" in reason:
            return "vwap_anti_chase"
        if "trigger" in reason:
            return "fresh_trigger_missing"
    if float(setup.get("score") or 0) < engine.MIN_ENTRY_SCORE:
        return "score_below_gate"
    return reasons[0] if reasons else "direction_not_qualified"


def replay_symbol(symbol: str, bars: list[dict[str, Any]]) -> dict[str, Any]:
    session_dates = sorted({
        bar["date"] for bar in bars
        if engine.MARKET_OPEN <= engine.bar_hhmm(bar) <= 1555
    })
    evaluated_sessions = 0
    opening_bars_evaluated = 0
    qualifying: list[dict[str, Any]] = []
    blockers: Counter[str] = Counter()
    session_rows: list[dict[str, Any]] = []
    safety_violations: list[dict[str, Any]] = []

    for session_date in session_dates:
        session_day = [
            bar for bar in bars
            if bar["date"] == session_date
            and engine.MARKET_OPEN <= engine.bar_hhmm(bar) <= 1555
        ]
        history = engine.regular_history_bars(bars, session_date=session_date)
        if not session_day or len(history) < 35:
            continue
        cutoff = datetime.combine(
            datetime.fromisoformat(session_date).date(), time(9, 29, 59), tzinfo=engine.ET,
        )
        context = engine.build_premarket_context(
            bars, now=cutoff, symbol=symbol,
        )
        if not context.get("ready"):
            continue
        evaluated_sessions += 1
        session_qualified: list[dict[str, Any]] = []
        for index, bar in enumerate(session_day[:OPENING_BARS]):
            prefix = session_day[:index + 1]
            rel_volume, rel_source = engine.relative_volume_for_session(prefix, history)
            setup = engine.signal(
                prefix,
                warmup_bars=history[-engine.OPENING_WARMUP_BARS:],
                relative_volume=rel_volume,
                premarket_context=context,
            )
            observable_at = bar["dt"] + timedelta(minutes=engine.BAR_INTERVAL_MINUTES)
            opening_bars_evaluated += 1
            bullish_break = float(bar["close"]) > float(context["premarketHigh"])
            bearish_break = float(bar["close"]) < float(context["premarketLow"])
            target_broken = bullish_break or bearish_break
            if setup.get("direction"):
                row = {
                    "sessionDate": session_date,
                    "observableAt": observable_at.isoformat(),
                    "signalBarStart": bar["dt"].isoformat(),
                    "direction": setup["direction"],
                    "close": round(float(setup["close"]), 4),
                    "score": int(setup["score"]),
                    "relativeVolume": round(float(setup["relativeVolume"]), 4),
                    "relativeVolumeSource": rel_source,
                    "premarketHigh": context["premarketHigh"],
                    "premarketLow": context["premarketLow"],
                    "target": setup.get("premarketBreakoutTarget"),
                    "openingAtr": round(float(setup.get("openingAtr") or 0.0), 4),
                    "extensionAtr": round(
                        abs(float(setup["close"]) - float(setup["premarketBreakoutTarget"]))
                        / float(setup["openingAtr"]),
                        4,
                    ) if float(setup.get("openingAtr") or 0.0) > 0 else None,
                }
                violations: list[str] = []
                if observable_at.hour * 100 + observable_at.minute < engine.ENTRY_START:
                    violations.append("entry_before_first_completed_bar")
                if setup.get("openingContinuation") is not True:
                    violations.append("missing_opening_continuation_flag")
                extension_failure = engine.opening_breakout_extension_failure(
                    setup, float(setup["close"]),
                )
                if extension_failure:
                    violations.append(extension_failure)
                if float(setup.get("relativeVolume") or 0.0) < engine.MIN_RELATIVE_VOLUME:
                    violations.append("relative_volume_below_gate")
                if float(setup.get("score") or 0.0) < engine.MIN_ENTRY_SCORE:
                    violations.append("score_below_gate")
                if violations:
                    safety_violations.append({**row, "violations": violations})
                qualifying.append(row)
                session_qualified.append(row)
            else:
                blockers[blocker_for(setup, target_broken=target_broken)] += 1
        session_rows.append({
            "sessionDate": session_date,
            "premarketHigh": context["premarketHigh"],
            "premarketLow": context["premarketLow"],
            "premarketLast": context["premarketLast"],
            "previousClose": context["previousClose"],
            "qualifyingBars": len(session_qualified),
            "firstQualified": session_qualified[0] if session_qualified else None,
        })

    sessions_with_setup = sorted({row["sessionDate"] for row in qualifying})
    return {
        "symbol": symbol,
        "sessionsAvailable": len(session_dates),
        "sessionsEvaluated": evaluated_sessions,
        "openingBarsEvaluated": opening_bars_evaluated,
        "qualifyingBars": len(qualifying),
        "sessionsWithQualifiedSetup": len(sessions_with_setup),
        "earliestQualifiedAt": min(
            (row["observableAt"] for row in qualifying), default=None,
        ),
        "primaryBlockers": dict(blockers.most_common()),
        "safetyViolations": safety_violations,
        "qualifiedSetups": qualifying,
        "recentSessions": session_rows[-10:],
    }


def build_report(symbols: list[str], period: str) -> dict[str, Any]:
    results = []
    for symbol in symbols:
        results.append(replay_symbol(symbol, fetch_history(symbol, period)))
    return {
        "generatedAt": datetime.now(engine.ET).isoformat(),
        "mode": "read_only_premarket_target_signal_replay",
        "periodRequested": period,
        "engineVersion": engine.ENGINE_VERSION,
        "strategyId": engine.STRATEGY_ID,
        "sourceCodeSha256": engine.source_code_sha256(),
        "configSha256": engine.config_sha256(),
        "convention": {
            "premarketContextCutoff": "09:29:59 America/New_York",
            "earliestObservableEntry": "09:35:00 America/New_York",
            "openingBarsEvaluated": OPENING_BARS,
            "warmup": "prior regular-session bars only",
            "relativeVolume": "prior sessions at matching five-minute clock labels only",
            "premarketBarsInIndicators": False,
            "optionExecutionOrPnlSimulated": False,
        },
        "limits": {
            "scoreAtLeast": engine.MIN_ENTRY_SCORE,
            "relativeVolumeAtLeast": engine.MIN_RELATIVE_VOLUME,
            "maxBreakoutExtensionAtr": engine.OPENING_MAX_BREAKOUT_EXTENSION_ATR,
        },
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", default="1mo")
    parser.add_argument("--symbols", nargs="+", default=list(engine.SYMBOLS))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_report([symbol.upper() for symbol in args.symbols], args.period)
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
