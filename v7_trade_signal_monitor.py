#!/usr/bin/env python3
"""V7 Trade Signal Monitor — checks for new qualified signals and trades.

Cron: every 3 minutes during market hours.
Alerts on:
  1. New trade entry (PAPER_ENTRY)
  2. New trade exit (PAPER_EXIT)
  3. Qualified signal detected (score ≥ threshold, direction set)
  4. Daemon not scanning (mode not active_scan during market hours)

Silent exit if nothing new.
"""
import json, math, sys, os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")
STATE_DIR = Path.home() / ".hermes" / "state"
SCRIPTS_DIR = Path.home() / ".hermes" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import v7_scalp_paper as engine


def now_pt():
    return datetime.now(PT)


def format_exit_pnl(symbol: str, event: dict) -> str:
    """Format realized P&L in dollars, with the percentage as context."""
    raw_usd = event.get("pnl")
    if raw_usd is None:
        raw_usd = event.get("pnl_usd")
    try:
        pnl_usd = float(raw_usd)
        if not math.isfinite(pnl_usd):
            raise ValueError
        dollar_text = f"${pnl_usd:+.2f}"
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

    try:
        held_minutes = float(event.get("heldMinutes", 0) or 0)
        if not math.isfinite(held_minutes):
            held_minutes = 0.0
    except (TypeError, ValueError):
        held_minutes = 0.0
    reason = event.get("reason", "")
    return f"{symbol} EXIT: P&L={dollar_text}{percent_text} held={held_minutes:.0f}min reason={reason}"


def is_market_hours():
    """Return True only while the V7 daemon is expected to be active.

    The monitor must use the engine's hard-close boundary, not the broader
    exchange session close. After V7 hard-closes at 15:55 ET, the daemon
    intentionally publishes ``off_hours`` and the monitor must stay silent.
    """
    now = datetime.now(ET)
    tm = now.hour * 100 + now.minute
    weekday = now.weekday()
    return weekday < 5 and engine.MARKET_OPEN <= tm < engine.HARD_CLOSE


def check_daemon_active():
    """Alert if daemon is not active_scan during market hours."""
    alerts = []
    if not is_market_hours():
        return alerts

    rt_path = STATE_DIR / "v7_scalp_live_runtime.json"
    if not rt_path.exists():
        return ["🚨 No runtime file — daemon may be down"]

    try:
        rt = json.loads(rt_path.read_text())
    except Exception:
        return ["🚨 Cannot parse runtime file"]

    mode = rt.get("mode", "unknown")
    failures = rt.get("consecutiveFailures", 0)
    engine_ver = rt.get("engineVersion", "?")

    # Check heartbeat staleness
    updated = rt.get("updatedAt", "")
    if updated:
        dt = datetime.fromisoformat(updated).astimezone(PT)
        age = (now_pt() - dt).total_seconds()
        if age > 120:
            alerts.append(f"🚨 Heartbeat stale: {age:.0f}s old (v{engine_ver})")

    # During market hours, mode should be active_scan or position
    if mode not in ("active_scan", "position"):
        reason = rt.get("reason", "unknown")
        # Allow lunch break
        now_et = datetime.now(ET)
        tm = now_et.hour * 100 + now_et.minute
        if 1200 <= tm <= 1230:
            pass  # Lunch break, fine
        elif mode == "stopped" or (mode == "off_hours" and reason == "unknown"):
            alerts.append(f"🚨 Daemon in {mode}/{reason} during market hours! (v{engine_ver})")

    if failures >= 3:
        alerts.append(f"🚨 {failures} consecutive daemon failures")

    return alerts


def check_new_trades():
    """Check for trades entered/exited today."""
    alerts = []
    today = datetime.now(ET).strftime("%Y%m%d")
    now = now_pt()

    for sym in engine.SYMBOLS:
        tp = STATE_DIR / f"v7_scalp_trades_{sym}.jsonl"
        if not tp.exists():
            continue

        entries_today = 0
        exits_today = 0
        latest_pnl = None

        for line in tp.read_text().splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue

            pid = ev.get("positionId", "")
            if today not in pid:
                continue

            event = ev.get("event", "")
            if event == "PAPER_ENTRY_REAL_ASK":
                entries_today += 1
            elif event in ("PAPER_EXIT_REAL_BID", "QUOTE_FAILURE_EXIT"):
                exits_today += 1
                latest_pnl = format_exit_pnl(sym, ev)

        if entries_today > 0:
            state_path = STATE_DIR / f"v7_scalp_state_{sym}.json"
            open_count = 0
            if state_path.exists():
                try:
                    state = json.loads(state_path.read_text())
                    open_count = len(state.get("activePositions", []))
                except Exception:
                    pass
            alerts.append(f"📊 {sym}: {entries_today} entries, {exits_today} exits today, {open_count} open now")
            if latest_pnl:
                alerts.append(f"   Latest: {latest_pnl}")

    return alerts


def check_qualified_signals():
    """Check for any qualified signals in the last 10 minutes."""
    alerts = []
    cutoff = (now_pt() - timedelta(minutes=10)).isoformat()

    qualified = []
    with open(STATE_DIR / "v7_scalp_scan_attempts.jsonl") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                ts = ev.get("timestamp", "")
                if ts < cutoff:
                    continue
                s = ev.get("result", {}).get("signal", {})
                direction = s.get("direction")
                if direction:
                    qualified.append(
                        f"{ev.get('symbol','?')} {direction} "
                        f"bull={s.get('bullScore',0)} bear={s.get('bearScore',0)} "
                        f"score={s.get('score',0)}"
                    )
            except Exception:
                continue

    if qualified:
        # Dedupe by symbol+direction+score so we don't spam 5 identical alerts
        seen = set()
        for q in qualified:
            key = q.rsplit(' ', 1)[0] if ' bull=' in q or ' bear=' in q else q
            # key like "SPY bullish bull=7 bear=3 score=7" — collapse to symbol+direction+score
            parts = q.split()
            compact = f"{parts[0]} {parts[1]} {parts[-1]}" if len(parts) >= 3 else q
            if compact in seen:
                continue
            seen.add(compact)
            alerts.append(f"🎯 Signal: {q}")
        if len(qualified) > len(seen):
            alerts.append(f"   ({len(qualified)} scans, {len(seen)} unique)")

    return alerts


def main():
    # Hard notification boundary: the daemon's active session only,
    # 09:30–15:55 ET (06:30–12:55 PT on weekdays). Keep this guard at the
    # entry point so trade and signal checks cannot emit after hard close.
    if not is_market_hours():
        return

    health_alerts = check_daemon_active()
    trade_alerts = check_new_trades()
    signal_alerts = check_qualified_signals()

    all_alerts = health_alerts + trade_alerts + signal_alerts

    if all_alerts:
        print(f"V{engine.ENGINE_VERSION} Monitor — {now_pt().strftime('%I:%M %p PT')}")
        for a in all_alerts:
            print(a)
        # Exit 0 so cron delivers stdout as a normal alert, not a "job failed" error.
        # Only real daemon health problems use exit 1.
        sys.exit(1 if health_alerts and not (trade_alerts or signal_alerts) else 0)

    # Silent if all clear
    sys.exit(0)


if __name__ == "__main__":
    main()
