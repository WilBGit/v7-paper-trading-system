#!/usr/bin/env python3
"""Report the active V7 evidence gate without enabling live trading.

A met statistical gate is evidence for review only. This scheduled entrypoint
never writes authorization or runtime state. Manual activation must use the
separate, explicitly confirmed path in ``v7_live_enable_monitor.py`` after a
fresh broker-readiness check.
"""
from __future__ import annotations

import sys
from pathlib import Path
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

def main() -> int:
    try:
        import v7_live_gate_progress as progress
        snapshot = progress.build_snapshot()
    except Exception as exc:
        print(f"v7_live_auto_activate: gate_check_failed:{exc}", file=sys.stderr)
        return 1

    if not snapshot.get("gateMet"):
        return 0  # silent — gate not met
    if snapshot.get("enableLiveAutomatically") is not False:
        return 0  # safety: auto-enable flag must be explicitly false
    if snapshot.get("liveOrdersEnabled") is not False:
        return 0  # safety: must not be already enabled
    try:
        if float(snapshot.get("exactNetPnl", snapshot.get("netPnl", 0))) <= 0:
            return 0  # positive exact current-release P&L required
    except (TypeError, ValueError):
        return 0

    # The gate is evidence only. It must never mutate authorization or live
    # state; manual activation is handled by the explicitly invoked monitor
    # command after a fresh broker-readiness check.
    exact_count = int(snapshot.get("exactClosedTrades", snapshot.get("closedTrades", 0)))
    exact_wins = int(snapshot.get("exactWins", snapshot.get("wins", 0)))
    exact_pnl = float(snapshot.get("exactNetPnl", snapshot.get("netPnl", 0)))
    print(
        f"V7 LIVE EVIDENCE GATE REACHED — {snapshot['engineVersion']}: "
        f"{exact_count} exact current-release trades, {exact_wins} wins, "
        f"exact P&L ${exact_pnl:+.2f}. "
        "REVIEW_REQUIRED; live remains disabled."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
