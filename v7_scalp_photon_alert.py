#!/usr/bin/env python3
"""Run the active V7 paper scanner and emit Photon text only for real paper entries.

Normal skips are silent so the five-minute cron does not spam iMessage. The
underlying scanner remains paper-only and has no broker order path.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import v7_scalp_paper

SCAN_AUDIT = Path.home() / ".hermes/state/v7_scalp_scan_attempts.jsonl"


def _money(value: Any) -> str:
    return f"${float(value):,.2f}"


def alert_quality_gate(position: dict[str, Any]) -> tuple[bool, list[str]]:
    """Verify the scanner's hard signal and contract gates before alerting."""
    reasons: list[str] = []
    signal = position.get("signal") or {}
    direction = str(position.get("direction", "")).lower()
    symbol = str(position.get("symbol", v7_scalp_paper.SYMBOL)).upper()
    try:
        config = v7_scalp_paper.config_for_symbol(symbol)
    except ValueError:
        return False, [f"unknown_symbol:{symbol}"]

    required_signal = ("score", "ema9", "ema21", "vwap", "rsi", "macdHist",
                       "supertrend", "momentumPct", "relativeVolume")
    missing = [name for name in required_signal if signal.get(name) is None]
    if missing:
        reasons.append("missing_indicators:" + ",".join(missing))
        return False, reasons

    score = int(signal["score"])
    ema9, ema21 = float(signal["ema9"]), float(signal["ema21"])
    macd = float(signal["macdHist"])
    supertrend = str(signal["supertrend"]).lower()
    underlying = float(position.get("entryUnderlying", 0) or 0)
    vwap = float(signal["vwap"])
    rsi = float(signal["rsi"])
    momentum = float(signal["momentumPct"])
    rel_volume = float(signal["relativeVolume"])

    required_score = v7_scalp_paper.effective_entry_score_floor(symbol, setup=signal)
    if score < required_score:
        reasons.append(f"score_below_{required_score}")
    if rel_volume < v7_scalp_paper.MIN_RELATIVE_VOLUME:
        reasons.append(
            f"relative_volume_below_{v7_scalp_paper.MIN_RELATIVE_VOLUME:.2f}"
        )

    structure_signal = dict(signal)
    structure_signal.setdefault("direction", direction)
    structural_breakout = v7_scalp_paper._structural_breakout_qualified(
        structure_signal
    )

    if direction == "bullish":
        if str(position.get("optionType", "")).lower() != "call":
            reasons.append("option_direction_mismatch")
        if rsi > v7_scalp_paper.RSI_OVERBOUGHT and not structural_breakout:
            reasons.append("rsi_overbought")
    elif direction == "bearish":
        if str(position.get("optionType", "")).lower() != "put":
            reasons.append("option_direction_mismatch")
        if rsi < v7_scalp_paper.RSI_OVERSOLD and not structural_breakout:
            reasons.append("rsi_oversold")
    else:
        reasons.append("unknown_direction")
    if not structural_breakout:
        reasons.append("structural_breakout_required")

    ask = float(position.get("entryAsk", 0) or 0)
    bid = float(position.get("entryBid", 0) or 0)
    cost = float(position.get("entryCost", ask * 100) or 0)
    strike = float(position.get("strike", 0) or 0)
    if not config.min_debit <= cost <= config.max_debit:
        reasons.append(
            f"contract_debit_outside_{config.min_debit:.0f}_{config.max_debit:.0f}"
        )
    mark = (ask + bid) / 2 if ask > 0 and bid > 0 else 0
    if mark <= 0 or (ask - bid) / mark > v7_scalp_paper.MAX_QUOTE_SPREAD_PCT / 100.0:
        reasons.append(f"contract_spread_above_{int(v7_scalp_paper.MAX_QUOTE_SPREAD_PCT)}pct")
    if underlying <= 0 or strike <= 0:
        reasons.append("missing_contract_price")
    else:
        otm_pct = max(0.0, (strike - underlying) / underlying if direction == "bullish"
                      else (underlying - strike) / underlying)
        _max_otm = v7_scalp_paper.effective_max_otm_pct(symbol) / 100.0
        if otm_pct > _max_otm:
            reasons.append(f"contract_more_than_{_max_otm*100:.2f}pct_otm")
    if not position.get("expiry"):
        reasons.append("missing_expiry")

    return not reasons, reasons


def format_entry_alert(position: dict[str, Any]) -> str:
    signal = position.get("signal", {})
    option_type = str(position.get("optionType", "option")).upper()
    expiry_raw = str(position.get("expiry", ""))
    try:
        expiry = datetime.fromisoformat(expiry_raw).strftime("%b %d")
    except ValueError:
        expiry = expiry_raw

    ema9 = float(signal.get("ema9", 0) or 0)
    ema21 = float(signal.get("ema21", 0) or 0)
    ema_text = "EMA9 above EMA21" if ema9 >= ema21 else "EMA9 below EMA21"
    macd = float(signal.get("macdHist", 0) or 0)
    macd_text = "MACD bullish" if macd >= 0 else "MACD bearish"
    supertrend = str(signal.get("supertrend", "unknown"))
    underlying = float(position.get("entryUnderlying", 0) or 0)
    vwap = float(signal.get("vwap", 0) or 0)
    vwap_text = "above VWAP" if underlying >= vwap else "below VWAP"

    rsi = float(signal.get("rsi", 0) or 0)
    rel_volume = float(signal.get("relativeVolume", 0) or 0)
    momentum = float(signal.get("momentumPct", 0) or 0)
    warnings = []
    if rsi > v7_scalp_paper.RSI_OVERBOUGHT:
        warnings.append(f"RSI {rsi:.0f} is overbought—pullback risk")
    elif rsi < v7_scalp_paper.RSI_OVERSOLD:
        warnings.append(f"RSI {rsi:.0f} is oversold—reversal risk")
    else:
        warnings.append(f"RSI {rsi:.0f} is neutral")
    if rel_volume < v7_scalp_paper.MIN_RELATIVE_VOLUME:
        warnings.append(f"relative volume {rel_volume:.2f}x is weak confirmation")
    else:
        warnings.append(f"relative volume {rel_volume:.2f}x confirms activity")
    warnings.append(f"momentum {momentum:+.2f}%")

    return "\n".join([
        "HIGH-QUALITY PAPER POTENTIAL ENTRY — No live order",
        (
            f"{position.get('symbol', 'SPY')} {option_type} • "
            f"${float(position.get('strike', 0)):g} strike • {expiry} expiry"
        ),
        (
            f"{position.get('symbol', 'SPY')} {_money(underlying)} • option ask "
            f"{_money(position.get('entryAsk', 0))} "
            f"({_money(position.get('entryCost', 0))}/contract)"
        ),
        (
            f"Score {int(signal.get('score', 0) or 0)} • {ema_text} • "
            f"{macd_text} • Supertrend {supertrend} • {vwap_text}"
        ),
        "Watch: " + "; ".join(warnings) + ".",
        "Paper signal only. 1DTE options can lose value quickly.",
    ])


def build_scan_diagnostic(
    result: dict[str, Any] | None, *, exit_code: int, symbol: str = v7_scalp_paper.SYMBOL,
) -> dict[str, Any]:
    result = result or {}
    position = result.get("position") if isinstance(result.get("position"), dict) else {}
    signal = result.get("signal") if isinstance(result.get("signal"), dict) else position.get("signal", {})
    if not isinstance(signal, dict):
        signal = {}
    reason = str(result.get("reason") or ("scanner_error" if exit_code else "unknown"))
    base_reason = reason.split(":", 1)[-1]
    action = str(result.get("action") or "UNKNOWN")
    if action == "PAPER_ENTRY":
        stage = "entry"
    elif exit_code or action == "ERROR":
        stage = "error"
    elif base_reason in {
        "market_closed", "outside_entry_window", "lunch_chop_block", "daily_trade_limit",
        "daily_loss_limit", "position_already_open", "pending_entry_exists", "kill_switch",
        "macro_calendar_unavailable", "macro_calendar_stale_or_incomplete",
    } or base_reason.startswith("macro_block_"):
        stage = "market_control"
    elif base_reason == "no_qualified_signal" or base_reason.startswith("signal_"):
        stage = "signal"
    elif "quote" in base_reason or base_reason in {
        "no_contract_candidate", "debit_out_of_range", "spread_too_wide", "too_far_otm",
        "occ_identity_mismatch", "stale_quote",
    }:
        stage = "quote"
    elif "risk" in base_reason or "loss_cooldown" in base_reason:
        stage = "risk"
    else:
        stage = "other"

    score = signal.get("score")
    relative_volume = signal.get("relativeVolume")
    rejection_reasons = list(signal.get("rejectionReasons") or [])
    direction = signal.get("direction") or position.get("direction")
    raw_direction = signal.get("rawDirection") or direction
    signal_bar_start = signal.get("signalBarStart")
    episode_id = None
    if signal_bar_start and raw_direction:
        fingerprint = "|".join([
            v7_scalp_paper.ENGINE_VERSION,
            v7_scalp_paper.config_sha256(),
            str(signal_bar_start),
            str(raw_direction),
        ])
        episode_id = hashlib.sha256(fingerprint.encode()).hexdigest()[:20]

    return {
        "symbol": symbol,
        "stage": stage,
        "action": action,
        "primaryBlocker": reason,
        "engineVersion": v7_scalp_paper.ENGINE_VERSION,
        "strategyId": v7_scalp_paper.STRATEGY_ID,
        "releaseManifestSha256": v7_scalp_paper.RELEASE_MANIFEST_SHA256,
        "sourceCodeSha256": v7_scalp_paper.source_code_sha256(),
        "configSha256": v7_scalp_paper.config_sha256(),
        "detector": signal.get("detector", "normal"),
        "direction": direction,
        "rawDirection": raw_direction,
        "score": score,
        "relativeVolume": relative_volume,
        "gates": {
            "score": isinstance(score, (int, float)) and score >= v7_scalp_paper.MIN_ENTRY_SCORE,
            "relativeVolume": isinstance(relative_volume, (int, float))
                              and relative_volume >= v7_scalp_paper.MIN_RELATIVE_VOLUME,
            "direction": direction in {"bullish", "bearish"},
            "rsi": not any("rsi_" in item for item in rejection_reasons),
            "freshTrigger": "fresh_trigger_required" not in rejection_reasons,
            "antiChase": not any("chase_" in item for item in rejection_reasons),
        },
        "rejectionReasons": rejection_reasons,
        "signalBarStart": signal_bar_start,
        "signalBarEnd": signal.get("signalBarEnd"),
        "completedBar": bool(signal_bar_start and signal.get("signalBarEnd")),
        "opportunityEpisodeId": episode_id,
        "quoteDiagnostics": result.get("quoteDiagnostics"),
    }


def _audit(
    raw: str, parsed: dict[str, Any] | None, exit_code: int,
    symbol: str = v7_scalp_paper.SYMBOL,
) -> None:
    SCAN_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "symbol": symbol,
        "engineVersion": v7_scalp_paper.ENGINE_VERSION,
        "strategyId": v7_scalp_paper.STRATEGY_ID,
        "releaseManifestSha256": v7_scalp_paper.RELEASE_MANIFEST_SHA256,
        "sourceCodeSha256": v7_scalp_paper.source_code_sha256(),
        "configSha256": v7_scalp_paper.config_sha256(),
        "exit_code": exit_code,
        "result": parsed,
        "diagnostic": build_scan_diagnostic(parsed, exit_code=exit_code, symbol=symbol),
        "raw": raw.strip() if parsed is None else None,
    }
    with SCAN_AUDIT.open("a") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def main() -> int:
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            exit_code = v7_scalp_paper.scan()
    except Exception as exc:
        print(f"V{v7_scalp_paper.ENGINE_VERSION} paper scanner error: {exc}")
        return 1

    raw = output.getvalue().strip()
    try:
        result = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        result = None
    if result and result.get("action") == "PAPER_ENTRY":
        passed, reasons = alert_quality_gate(result["position"])
        result["photonAlert"] = {"passed": passed, "reasons": reasons}
        _audit(raw, result, exit_code)
        if passed:
            print(format_entry_alert(result["position"]))
    else:
        _audit(raw, result, exit_code)
        if exit_code != 0 or (result and result.get("action") == "ERROR"):
            detail = result.get("error", "unknown scanner failure") if result else raw
            print(f"V{v7_scalp_paper.ENGINE_VERSION} paper scanner error: {detail}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
