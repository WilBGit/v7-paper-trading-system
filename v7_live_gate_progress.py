#!/usr/bin/env python3
"""Promotion evidence gate for the active V7 release.

Historical carry-forward progress from the V7.4.20/V7.4.21 lineage is shown
for context without rewriting historical ledger events. The active gate is
met only when the exact current-release sample reaches the configured minimum
with positive current-release P&L. Combined totals remain diagnostic only.
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from v7_scalp_weekly_report import ACTIVE_RELEASE, EVIDENCE_GATE, SYMBOLS
from v7_release import EVIDENCE_CARRY_FORWARD

STATE_DIR = Path("/path/to/project/.hermes/state")
PROGRESS_PATH = STATE_DIR / "v7_live_gate_progress.json"
PT = ZoneInfo("America/Los_Angeles")
IDENTITY_FIELDS = tuple(ACTIVE_RELEASE)


def _normalize_carry_forward(value: dict[str, object] | None) -> dict[str, object]:
    """Normalize a manifest carry-forward record without treating it as exact evidence."""
    raw = value or {}

    def number(*keys: str, default: float = 0.0) -> float:
        for key in keys:
            if key in raw:
                try:
                    parsed = float(raw[key])
                except (TypeError, ValueError):
                    return default
                return parsed if math.isfinite(parsed) else default
        return default

    closed = max(int(number("closedTrades", "closed_trades")), 0)
    wins = max(int(number("wins")), 0)
    losses = max(int(number("lossesOrBreakeven", "losses_or_breakeven")), 0)
    if wins + losses != closed:
        return {
            "closedTrades": 0,
            "wins": 0,
            "lossesOrBreakeven": 0,
            "netPnl": 0.0,
            "source": {},
        }
    return {
        "closedTrades": closed,
        "wins": wins,
        "lossesOrBreakeven": losses,
        "netPnl": round(number("netPnl", "net_pnl"), 2),
        "source": {
            "engineVersion": raw.get("sourceEngineVersion", raw.get("source_engine_version")),
            "strategyId": raw.get("sourceStrategyId", raw.get("source_strategy_id")),
            "releaseManifestSha256": raw.get(
                "sourceReleaseManifestSha256",
                raw.get("source_release_manifest_sha256"),
            ),
        },
    }


def _matches_identity(event: dict, identity: dict[str, str]) -> bool:
    return all(event.get(field) == value for field, value in identity.items())


def _load_events() -> list[dict]:
    events: list[dict] = []
    for symbol in SYMBOLS:
        path = STATE_DIR / f"v7_scalp_trades_{symbol}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def _fallback_priced_exit(event: dict) -> bool:
    """Reject exits priced from an estimate rather than a live bid."""
    if any(event.get(key) is True for key in (
        "fallback", "fallbackPriced", "usedLastKnownBid", "quoteFallback",
    )):
        return True
    text = " ".join(str(event.get(key, "")) for key in (
        "reason", "quoteSource", "exitSource", "pricingSource",
    )).lower()
    return any(token in text for token in (
        "fallback", "last_known", "estimated", "assumed", "quote_failure",
    ))


def _valid_exit_evidence(event: dict, entry: dict) -> bool:
    """Require finite, quote-derived evidence tied to the paired entry."""
    if _fallback_priced_exit(event):
        return False
    try:
        pnl = float(event["pnl"])
        entry_ask = float(entry["entryAsk"])
        exit_bid = float(event["exitBid"])
    except (KeyError, TypeError, ValueError):
        return False
    if not all(math.isfinite(value) for value in (pnl, entry_ask, exit_bid)):
        return False
    if entry_ask <= 0 or exit_bid <= 0:
        return False
    if "entryAsk" in event:
        try:
            copied_entry_ask = float(event["entryAsk"])
        except (TypeError, ValueError):
            return False
        if not math.isfinite(copied_entry_ask) or not math.isclose(
            copied_entry_ask, entry_ask, abs_tol=0.0001, rel_tol=0.0
        ):
            return False
    recomputed = (exit_bid - entry_ask) * 100.0
    return math.isclose(pnl, recomputed, abs_tol=0.05, rel_tol=0.0)


def summarize_events(
    events: list[dict],
    active_release: dict[str, str],
    evidence_gate: dict[str, object],
    carry_forward: dict[str, object] | None = None,
) -> dict[str, object]:
    """Summarize exact evidence plus a non-gating carry-forward baseline."""
    entries: dict[str, dict] = {}
    for event in events:
        if event.get("event") != "PAPER_ENTRY_REAL_ASK":
            continue
        position_id = event.get("positionId")
        if position_id and _matches_identity(event, active_release):
            entries.setdefault(str(position_id), event)

    closed: list[dict] = []
    excluded_evidence = 0
    seen_exits: set[str] = set()
    for event in events:
        if event.get("event") != "PAPER_EXIT_REAL_BID":
            continue
        position_id = event.get("positionId")
        position_key = str(position_id) if position_id else ""
        if (
            position_key
            and position_key in entries
            and position_key not in seen_exits
            and _matches_identity(event, active_release)
        ):
            if not _valid_exit_evidence(event, entries[position_key]):
                excluded_evidence += 1
                continue
            pnl = float(event["pnl"])
            closed.append({"positionId": position_key, "pnl": pnl, "symbol": event.get("symbol")})
            seen_exits.add(position_key)

    pnls = [float(item["pnl"]) for item in closed]
    exact_closed_trades = len(pnls)
    exact_wins = sum(pnl > 0 for pnl in pnls)
    exact_losses = sum(pnl <= 0 for pnl in pnls)
    exact_net_pnl = round(sum(pnls), 2)
    exact_win_rate = round(exact_wins / exact_closed_trades * 100, 1) if exact_closed_trades else 0.0
    minimum = int(evidence_gate["min_closed_trades"])
    minimum_wr = float(evidence_gate["min_win_rate_pct"])
    positive_required = bool(evidence_gate["require_positive_pnl"])

    if exact_closed_trades < minimum:
        exact_gate_state = "NOT ELIGIBLE"
    elif exact_win_rate < minimum_wr:
        exact_gate_state = "NOT ELIGIBLE"
    elif positive_required and exact_net_pnl <= 0:
        exact_gate_state = "NOT ELIGIBLE"
    else:
        exact_gate_state = "REVIEW REQUIRED"
    exact_gate_met = exact_gate_state == "REVIEW REQUIRED"

    carry = _normalize_carry_forward(carry_forward)
    carried_trades = int(carry["closedTrades"])
    carried_wins = int(carry["wins"])
    carried_losses = int(carry["lossesOrBreakeven"])
    carried_net_pnl = float(carry["netPnl"])
    reported_trades = carried_trades + exact_closed_trades
    reported_wins = carried_wins + exact_wins
    reported_losses = carried_losses + exact_losses
    reported_net_pnl = round(carried_net_pnl + exact_net_pnl, 2)
    reported_win_rate = round(reported_wins / reported_trades * 100, 1) if reported_trades else 0.0

    combined_gate_met = (
        reported_trades >= minimum
        and (not positive_required or reported_net_pnl > 0)
    )

    if exact_gate_met:
        gate_state = "REVIEW REQUIRED"
    elif exact_closed_trades < minimum:
        gate_state = (
            f"NOT ELIGIBLE — exact current-release {exact_closed_trades}/{minimum} progress"
            f" ({carried_trades} carried forward for context)"
        )
    elif minimum_wr > 0 and exact_win_rate < minimum_wr:
        gate_state = "NOT ELIGIBLE — exact current-release win rate required"
    elif positive_required and exact_net_pnl <= 0:
        gate_state = "NOT ELIGIBLE — exact current-release positive P&L required"
    else:
        gate_state = "NOT ELIGIBLE"

    return {
        "closedTrades": reported_trades,
        "wins": reported_wins,
        "lossesOrBreakeven": reported_losses,
        "winRatePct": reported_win_rate,
        "netPnl": reported_net_pnl,
        "remainingTrades": max(minimum - exact_closed_trades, 0),
        "excludedEvidence": excluded_evidence,
        "gateState": gate_state,
        "gateMet": exact_gate_met,
        "exactGateMet": exact_gate_met,
        "exactGateState": exact_gate_state,
        "combinedGateMet": combined_gate_met,
        "exactClosedTrades": exact_closed_trades,
        "exactWins": exact_wins,
        "exactLossesOrBreakeven": exact_losses,
        "exactWinRatePct": exact_win_rate,
        "exactNetPnl": exact_net_pnl,
        "exactRemainingTrades": max(minimum - exact_closed_trades, 0),
        "carriedForwardTrades": carried_trades,
        "carriedForwardWins": carried_wins,
        "carriedForwardLossesOrBreakeven": carried_losses,
        "carriedForwardNetPnl": carried_net_pnl,
        "carryForwardSource": carry["source"],
        "enableLiveAutomatically": False,
        "symbols": {
            symbol: sum(1 for item in closed if item.get("symbol") == symbol)
            for symbol in SYMBOLS
        },
    }


def build_snapshot() -> dict[str, object]:
    snapshot = summarize_events(
        _load_events(),
        ACTIVE_RELEASE,
        EVIDENCE_GATE,
        EVIDENCE_CARRY_FORWARD,
    )
    promotion_gate = {
        "minClosedTrades": int(EVIDENCE_GATE["min_closed_trades"]),
        "minWinRatePct": float(EVIDENCE_GATE["min_win_rate_pct"]),
        "requirePositivePnl": bool(EVIDENCE_GATE["require_positive_pnl"]),
    }
    return {
        "checkedAt": datetime.now(PT).isoformat(),
        "engineVersion": ACTIVE_RELEASE["engineVersion"],
        "strategyId": ACTIVE_RELEASE["strategyId"],
        "releaseManifestSha256": ACTIVE_RELEASE["releaseManifestSha256"],
        "gatePolicy": "operator_promotion",
        "promotionGate": promotion_gate,
        # Compatibility field: existing consumers read the operator gate here.
        "evidenceGate": promotion_gate,
        "liveOrdersEnabled": False,
        **snapshot,
    }


def should_notify(previous: dict[str, object] | None, current: dict[str, object]) -> bool:
    if not previous:
        return True
    fields = (
        "engineVersion",
        "strategyId",
        "closedTrades",
        "exactClosedTrades",
        "carriedForwardTrades",
        "wins",
        "lossesOrBreakeven",
        "winRatePct",
        "netPnl",
        "gateState",
        "gateMet",
    )
    return any(previous.get(field) != current.get(field) for field in fields)


def _atomic_write(payload: dict[str, object]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{PROGRESS_PATH.name}.", dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, PROGRESS_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _read_previous() -> dict[str, object] | None:
    try:
        value = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def format_update(snapshot: dict[str, object]) -> str:
    closed = int(snapshot["closedTrades"])
    wins = int(snapshot["wins"])
    losses = int(snapshot["lossesOrBreakeven"])
    pnl = float(snapshot["netPnl"])
    wr = float(snapshot["winRatePct"])
    version = snapshot["engineVersion"]
    minimum = int(snapshot["evidenceGate"]["minClosedTrades"])
    carried = int(snapshot.get("carriedForwardTrades", 0))
    exact = int(snapshot.get("exactClosedTrades", closed))
    if snapshot["gateMet"]:
        return (
            f"V7 LIVE EVIDENCE GATE REACHED — {version}: {exact} exact current-release "
            f"trades, {snapshot.get('exactWins', wins)} wins, "
            f"exact P&L ${float(snapshot.get('exactNetPnl', pnl)):+.2f}. "
            "Statistical gate met. Live remains disabled pending the full pre-live "
            "broker-readiness audit and final activation."
        )
    if carried:
        return (
            f"V7 live-gate progress — {version}: {closed}/{minimum} progress "
            f"({carried} carried forward, {exact} exact current-release), "
            f"{wins}W/{losses} nonwins, {wr:.1f}% WR, P&L ${pnl:+.2f}; "
            f"{snapshot['remainingTrades']} exact current-release trades remaining. "
            f"Combined context is {closed} trades and ${pnl:+.2f}; live remains disabled."
        )
    return (
        f"V7 live-gate progress — {version}: {closed}/{minimum} exact closed trades, "
        f"{wins}W/{losses} nonwins, {wr:.1f}% WR, P&L ${pnl:+.2f}; "
        f"{snapshot['remainingTrades']} trades remaining. Live remains disabled."
    )


def main() -> int:
    previous = _read_previous()
    snapshot = build_snapshot()
    notify = should_notify(previous, snapshot)
    snapshot["notificationSent"] = notify
    _atomic_write(snapshot)
    if notify:
        print(format_update(snapshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
