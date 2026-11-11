#!/usr/bin/env python3
"""V7 Anomaly Watchdog — scans daemon logs and trade state for bugs.

Runs every 5 minutes during market hours. Alerts on:
  1. Positions held past the dynamic max-hold ceiling (safety net failure)
  2. Consecutive daemon failures (quote/monitor breakdown)
  3. hard_close exits (positions that weren't properly managed)
  4. Stale heartbeat (daemon not updating runtime)
  5. Unmatched ledger entries (entry with no exit)
  6. Exit P&L < -30% (catastrophic loss)

Exits 0 silently if everything is normal.
Exits 1 and prints alerts if anomalies found.
"""
import json, math, sys, os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
STATE_DIR = Path.home() / ".hermes" / "state"
SCRIPTS_DIR = Path.home() / ".hermes" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# Graceful import: if the release manifest is mid-deploy (validator updated
# before manifest JSON), the engine import will fail.  Catch that specific
# error so the watchdog can still report the problem instead of silently
# crashing.  The daemon itself will hit the same error and pause trading,
# so this is informational, not a safety gap.
try:
    import v7_scalp_paper as engine
except RuntimeError as _import_err:
    if "release_manifest_missing" in str(_import_err):
        print(f"ALERT:release_manifest_import_failed:{_import_err}")
        print("INFO:Daemon likely paused for same reason; deploy manifest JSON before validator.")
        sys.exit(1)
    raise


def now_et():
    return datetime.now(ET)


def format_pnl(event: dict) -> str:
    """Format anomaly P&L in dollars, with percent as secondary context."""
    raw_usd = event.get("pnl")
    if raw_usd is None:
        raw_usd = event.get("pnl_usd")
    try:
        pnl_usd = float(raw_usd)
        dollar_text = f"${pnl_usd:+.2f}" if math.isfinite(pnl_usd) else "$N/A"
    except (TypeError, ValueError):
        dollar_text = "$N/A"

    raw_pct = event.get("pnlPct")
    if raw_pct is None:
        raw_pct = event.get("pnl_pct")
    try:
        pnl_pct = float(raw_pct)
        percent_text = f" ({pnl_pct:+.1f}%)" if math.isfinite(pnl_pct) else ""
    except (TypeError, ValueError):
        percent_text = ""
    return f"P&L={dollar_text}{percent_text}"


def check_positions_held_too_long():
    """Flag any position held past the dynamic max-hold ceiling (35 min) + 2 minutes."""
    alerts = []
    threshold = 37  # dynamic_max_hold_minutes ceiling (35) + buffer (2)
    for sym in engine.SYMBOLS:
        state_path = STATE_DIR / f"v7_scalp_state_{sym}.json"
        if not state_path.exists():
            continue
        try:
            state = json.loads(state_path.read_text())
        except Exception:
            continue
        for pos in state.get("activePositions", []):
            entry_t = datetime.fromisoformat(pos["entryTime"]).astimezone(ET)
            held = (now_et() - entry_t).total_seconds() / 60
            if held > threshold:
                alerts.append(
                    f"🚨 {sym} {pos.get('positionId','')} held {held:.1f}min "
                    f"(max 35min dynamic) — safety net should fire"
                )
    return alerts


def check_consecutive_failures():
    """Flag if daemon has consecutive failures."""
    alerts = []
    rt_path = STATE_DIR / "v7_scalp_live_runtime.json"
    if not rt_path.exists():
        return ["🚨 No runtime heartbeat file"]
    try:
        rt = json.loads(rt_path.read_text())
    except Exception:
        return ["🚨 Cannot parse runtime heartbeat"]
    failures = rt.get("consecutiveFailures", 0)
    if failures >= 3:
        alerts.append(f"🚨 {failures} consecutive daemon failures — monitor may be broken")
    # Stale heartbeat
    updated = rt.get("updatedAt", "")
    if updated:
        dt = datetime.fromisoformat(updated).astimezone(ET)
        age = (now_et() - dt).total_seconds()
        if age > 120:
            alerts.append(f"🚨 Heartbeat stale: {age:.0f}s old (threshold 120s)")
    return alerts


def _load_acknowledged_alerts() -> set[str]:
    """Load previously acknowledged alert IDs so we don't re-alert on known issues."""
    ack_path = STATE_DIR / "v7_watchdog_acked.json"
    if ack_path.exists():
        try:
            return set(json.loads(ack_path.read_text()))
        except Exception:
            pass
    return set()


def _save_acknowledged_alerts(acked: set[str]) -> None:
    """Persist acknowledged alert IDs."""
    ack_path = STATE_DIR / "v7_watchdog_acked.json"
    ack_path.write_text(json.dumps(sorted(acked)))


def check_recent_hard_closes(acked: set[str] | None = None) -> list[str]:
    """Flag any hard_close or catastrophic loss in recent trades.

    Skips alerts for position IDs already in ``acked`` (dedup).
    New alerts are added to ``acked`` so the caller can persist them.
    """
    if acked is None:
        acked = set()
    alerts = []
    for sym in engine.SYMBOLS:
        tp = STATE_DIR / f"v7_scalp_trades_{sym}.jsonl"
        if not tp.exists():
            continue
        # Paper position IDs use YYYYMMDD (for example paper-20260821T...).
        today = now_et().strftime("%Y%m%d")
        for line in tp.read_text().splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("event") != "PAPER_EXIT_REAL_BID":
                continue
            # Only today's trades
            pid = ev.get("positionId", "")
            if today not in pid:
                continue
            # Dedup: skip alerts we've already reported for this position
            alert_key = f"{pid}:{ev.get('reason', 'unknown')}"
            if alert_key in acked:
                continue
            reason = ev.get("reason", "")
            pnl_pct = ev.get("pnlPct", 0)
            held = ev.get("heldMinutes", 0)
            if reason == "hard_close":
                alerts.append(f"🚨 {sym} {pid} hard_close at {held:.0f}min {format_pnl(ev)}")
                acked.add(alert_key)
            elif pnl_pct < -30:
                alerts.append(f"🚨 {sym} {pid} catastrophic loss {format_pnl(ev)}")
                acked.add(alert_key)
            elif held > 40:
                alerts.append(f"⚠️ {sym} {pid} held {held:.0f}min (>40min absolute ceiling)")
                acked.add(alert_key)
    return alerts


def check_unmatched_ledger():
    """Flag orphaned entries (entry with no exit and NOT currently open)."""
    alerts = []
    for sym in engine.SYMBOLS:
        tp = STATE_DIR / f"v7_scalp_trades_{sym}.jsonl"
        if not tp.exists():
            continue
        entries = set()
        exited = set()
        for line in tp.read_text().splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            pid = ev.get("positionId", "")
            if ev.get("event") == "PAPER_ENTRY_REAL_ASK" and pid:
                entries.add(pid)
            elif ev.get("event") == "PAPER_EXIT_REAL_BID" and pid:
                exited.add(pid)
        # Exclude currently open positions — they have entries but exits are expected later
        active_ids = set()
        state_path = STATE_DIR / f"v7_scalp_state_{sym}.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
                active_ids = {p["positionId"] for p in state.get("activePositions", [])}
            except Exception:
                pass
        orphaned = (entries - exited) - active_ids
        if orphaned:
            alerts.append(f"🚨 {sym}: {len(orphaned)} orphaned entries (no exit, not open): {', '.join(list(orphaned)[:3])}")
    return alerts


def main():
    acked = _load_acknowledged_alerts()
    all_alerts = []
    all_alerts.extend(check_positions_held_too_long())
    all_alerts.extend(check_consecutive_failures())
    all_alerts.extend(check_recent_hard_closes(acked))
    all_alerts.extend(check_unmatched_ledger())

    # Persist any newly-acknowledged alert keys (even if they were the only alerts)
    _save_acknowledged_alerts(acked)

    if all_alerts:
        print(f"V7 ANOMALY WATCHDOG — {now_et().strftime('%Y-%m-%d %H:%M %Z')}")
        for a in all_alerts:
            print(a)
        sys.exit(1)

    # Silent exit if all clear
    sys.exit(0)


if __name__ == "__main__":
    main()
