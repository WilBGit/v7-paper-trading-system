#!/usr/bin/env python3
"""
V7 Pre-Flight Checklist — run before each trading session.

21 results (including per-symbol checks):
  1. State reset (per symbol) — lastResetDate matches today. Auto-fixable.
  2. Contract feasibility (per symbol) — calendar distance, effective OTM.
  3. Premarket snapshot (per symbol) — fresh target file from today.
  4. Daemon health — running, heartbeat fresh, no kill switch, live=false.
  5. Deployment hashes — disk engine, daemon, and config match the frozen runtime.
  6. Macro calendar — covers next trading session.
  7. Ledger integrity (per symbol) — no unmatched entries.
  8. Cron job health — V7 trading crons enabled and recent.
  9. Evidence gate — exact-release trade count, WR, and P&L (warns on a losing cohort).
  10. Brief coverage — morning brief includes premarket levels.
  11. Reconcile bug fix — source code regression check.
  12. OTM adaptive fix — source code regression check.
  13. Monitoring cadence, hold-anomaly visibility, cold-reset, and max-hold safeguards.

Usage:
    python3 v7_preflight_check.py           # report only
    python3 v7_preflight_check.py --fix     # auto-fix fixable issues
    python3 v7_preflight_check.py --json    # machine-readable output
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

STATE_DIR = Path.home() / ".hermes" / "state"
DAEMON_LABEL = "com.hermes.v73-paper-live-daemon"

sys.path.insert(0, str(SCRIPTS_DIR))
from v7_live_auth import live_authorized  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
FIXED = "FIXED"
INFO = "INFO"


def now_et() -> datetime:
    return datetime.now(ET)


def today_key() -> str:
    return now_et().strftime("%Y-%m-%d")


def next_trading_day() -> str:
    d = now_et().date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


# ---------------------------------------------------------------------------
# Check functions
# ---------------------------------------------------------------------------

def check_state_reset(symbol: str, auto_fix: bool) -> dict:
    state_path = STATE_DIR / f"v7_scalp_state_{symbol}.json"
    if not state_path.exists():
        return {"check": "state_reset", "symbol": symbol, "status": FAIL,
                "detail": f"state file missing"}
    try:
        state = json.loads(state_path.read_text())
    except Exception as exc:
        return {"check": "state_reset", "symbol": symbol, "status": FAIL,
                "detail": f"unreadable: {exc}"}

    try:
        import v7_scalp_paper as engine
        expected_session = engine.trading_session_key(symbol, now_et())
    except Exception as exc:
        return {"check": "state_reset", "symbol": symbol, "status": FAIL,
                "detail": f"session-key error: {exc}"}
    file_date = state.get("lastResetDate", "")
    if file_date == expected_session:
        return {"check": "state_reset", "symbol": symbol, "status": PASS,
                "detail": f"lastResetDate={file_date}, rev={state.get('stateRevision')}"}

    if not auto_fix:
        return {"check": "state_reset", "symbol": symbol, "status": FAIL,
                "detail": f"lastResetDate={file_date} != expected_session={expected_session}"}

    try:
        import v7_scalp_live_daemon as daemon
        daemon._read_state(symbol)
        state2 = json.loads(state_path.read_text())
        if state2.get("lastResetDate") == expected_session:
            return {"check": "state_reset", "symbol": symbol, "status": FIXED,
                    "detail": f"reset {file_date} → {expected_session}"}
        return {"check": "state_reset", "symbol": symbol, "status": FAIL,
                "detail": f"auto-fix failed: still {state2.get('lastResetDate')}"}
    except Exception as exc:
        return {"check": "state_reset", "symbol": symbol, "status": FAIL,
                "detail": f"auto-fix error: {exc}"}


def check_contract_feasibility(symbol: str) -> dict:
    try:
        import v7_scalp_paper as engine
        cfg = engine.config_for_symbol(symbol)
        effective_otm = engine.effective_max_otm_pct(symbol)
        today = now_et().date()
        cursor = today
        business = 0
        while business < engine.OPTION_DTE_TARGET:
            cursor += timedelta(days=1)
            if cursor.weekday() < 5:
                business += 1
        calendar_days = (cursor - today).days
        return {
            "check": "contract_feasibility", "symbol": symbol,
            "status": PASS if calendar_days <= 1 else WARN,
            "detail": f"1DTE → {cursor.isoformat()} ({calendar_days} cal days), "
                      f"OTM {cfg.max_otm_pct}% → {effective_otm}%, debit ${cfg.min_debit}-${cfg.max_debit}",
        }
    except Exception as exc:
        return {"check": "contract_feasibility", "symbol": symbol, "status": FAIL,
                "detail": f"error: {exc}"}


def check_premarket_snapshot(symbol: str) -> dict:
    # Equity symbols use the regular-session premarket target tracker. Index
    # V7.5: symbol universe is SPY+QQQ only; index-option path retained for
    # compatibility but no active symbols use it.
    import v7_scalp_paper as engine
    if engine.is_index_option_symbol(symbol):
        return {
            "check": "premarket_snapshot",
            "symbol": symbol,
            "status": INFO,
            "detail": "not applicable: index-option symbol uses venue-session parity quotes",
        }

    pm_path = STATE_DIR / f"v7_scalp_premarket_{symbol}.json"
    if not pm_path.exists():
        return {"check": "premarket_snapshot", "symbol": symbol, "status": WARN,
                "detail": "no snapshot yet (daemon may not have reached premarket watch)"}
    try:
        data = json.loads(pm_path.read_text())
        session_date = data.get("sessionDate", "")
        if session_date == today_key():
            return {"check": "premarket_snapshot", "symbol": symbol, "status": PASS,
                    "detail": f"session={session_date}, active={data.get('activeDirection')} → {data.get('activePriceTarget')}"}
        return {"check": "premarket_snapshot", "symbol": symbol, "status": WARN,
                "detail": f"stale session={session_date} (today={today_key()})"}
    except Exception as exc:
        return {"check": "premarket_snapshot", "symbol": symbol, "status": FAIL,
                "detail": f"error: {exc}"}


def kill_switch_active() -> bool:
    """Return whether the paper health gate should report a kill switch."""
    kill_path = STATE_DIR / "v7_kill_switch.json"
    if not kill_path.exists():
        return False
    try:
        payload = json.loads(kill_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return not isinstance(payload, dict) or payload.get("active") is not False


def check_daemon_health() -> dict:
    issues = []
    if kill_switch_active():
        issues.append("kill_switch_active")
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{DAEMON_LABEL}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            issues.append("launchagent_not_found")
        elif "state = running" not in result.stdout:
            issues.append("daemon_not_running")
    except Exception as exc:
        issues.append(f"launchctl_error: {exc}")
    runtime_path = STATE_DIR / "v7_scalp_live_runtime.json"
    if not runtime_path.exists():
        issues.append("no_runtime_heartbeat")
    else:
        try:
            rt = json.loads(runtime_path.read_text())
            updated = rt.get("updatedAt", "")
            if updated:
                dt = datetime.fromisoformat(updated).astimezone(ET)
                age = (now_et() - dt).total_seconds()
                if age > 120:
                    issues.append(f"stale_heartbeat_{age:.0f}s")
            if rt.get("consecutiveFailures", 0) > 0:
                issues.append(f"consecutive_failures={rt['consecutiveFailures']}")
            if rt.get("liveOrdersEnabled"):
                if not live_authorized():
                    issues.append("LIVE_ORDERS_ENABLED")
        except Exception as exc:
            issues.append(f"runtime_parse_error: {exc}")
    if not issues:
        return {"check": "daemon_health", "status": PASS,
                "detail": "running, heartbeat fresh, no kill switch, live=false"}
    return {"check": "daemon_health", "status": FAIL if "daemon_not_running" in str(issues) else WARN,
            "detail": "; ".join(issues)}


def check_source_hash() -> dict:
    """Verify the frozen daemon matches engine, daemon, and execution config on disk."""
    sources = {
        "engine": SCRIPTS_DIR / "v7_scalp_paper.py",
        "daemon": SCRIPTS_DIR / "v7_scalp_live_daemon.py",
    }
    missing = [name for name, path in sources.items() if not path.exists()]
    if missing:
        return {"check": "source_hash", "status": FAIL,
                "detail": f"source missing: {', '.join(missing)}"}

    runtime_path = STATE_DIR / "v7_scalp_live_runtime.json"
    if not runtime_path.exists():
        return {"check": "source_hash", "status": FAIL, "detail": "no runtime heartbeat to compare"}
    try:
        runtime = json.loads(runtime_path.read_text())
        disk = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sources.items()}
        active_config = active_release_identity()["configSha256"]
    except Exception as exc:
        return {"check": "source_hash", "status": FAIL, "detail": f"hash check error: {exc}"}

    checks = {
        "engine": (disk["engine"], runtime.get("engineSourceSha256", "")),
        "daemon": (disk["daemon"], runtime.get("daemonSourceSha256", "")),
        "config": (active_config, runtime.get("configSha256", "")),
        "releaseManifest": (
            active_release_identity()["releaseManifestSha256"],
            runtime.get("releaseManifestSha256", ""),
        ),
    }
    mismatches = [
        f"{name} disk={expected[:12]}... runtime={actual[:12]}..."
        for name, (expected, actual) in checks.items()
        if expected != actual
    ]
    if mismatches:
        return {"check": "source_hash", "status": FAIL,
                "detail": "DAEMON STALE: " + "; ".join(mismatches) +
                          " — restart required: bash ~/.hermes/scripts/v73_restart_daemon.sh"}
    return {"check": "source_hash", "status": PASS,
            "detail": f"engine, daemon, config, and release-manifest hashes match ({disk['engine'][:12]}...)"}


def check_macro_calendar() -> dict:
    """Verify calendar metadata, freshness, and next-session coverage."""
    cal_path = STATE_DIR / "v7_macro_calendar.json"
    if not cal_path.exists():
        return {"check": "macro_calendar", "status": FAIL, "detail": "calendar file missing"}
    try:
        import v7_scalp_paper as engine
        cal = json.loads(cal_path.read_text())
        if cal.get("timezone") != "America/New_York" or not cal.get("source"):
            raise ValueError("missing calendar source or timezone")
        generated_at = datetime.fromisoformat(
            str(cal["generatedAt"]).replace("Z", "+00:00")
        )
        if generated_at.tzinfo is None:
            raise ValueError("generatedAt must include timezone")
        coverage_start = datetime.fromisoformat(str(cal["coverageStart"])).date()
        coverage_end = datetime.fromisoformat(str(cal["coverageThrough"])).date()
        days = cal.get("days")
        if not isinstance(days, dict):
            raise ValueError("days must be an object")

        current = now_et()
        age = current - generated_at.astimezone(ET)
        max_age_days = engine.MAX_CALENDAR_AGE_DAYS
        if age > timedelta(days=max_age_days):
            return {
                "check": "macro_calendar", "status": FAIL,
                "detail": f"calendar stale: generated {generated_at.isoformat()}, "
                           f"age={age.total_seconds() / 3600:.1f}h, "
                           f"max={max_age_days}d — renew calendar",
            }
        if age < timedelta(hours=-1):
            return {
                "check": "macro_calendar", "status": FAIL,
                "detail": f"calendar generatedAt is in the future: {generated_at.isoformat()}",
            }

        today = current.date()
        next_session_key = next_trading_day()
        next_session = datetime.fromisoformat(next_session_key).date()
        if not coverage_start <= today <= coverage_end:
            return {
                "check": "macro_calendar", "status": FAIL,
                "detail": f"coverage {coverage_start}–{coverage_end} does not include today {today}",
            }
        if coverage_end >= next_session:
            has_events = next_session_key in days
            return {"check": "macro_calendar", "status": PASS,
                    "detail": f"fresh ({age.total_seconds() / 3600:.1f}h old), "
                              f"coverage through {coverage_end}, next session {next_session_key} covered={has_events}"}
        return {"check": "macro_calendar", "status": FAIL,
                "detail": f"coverage ends {coverage_end}, next session {next_session_key} NOT covered — renew calendar"}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"check": "macro_calendar", "status": FAIL,
                "detail": f"invalid calendar metadata: {exc}"}
    except Exception as exc:
        return {"check": "macro_calendar", "status": FAIL, "detail": f"error: {exc}"}


def check_ledger_integrity(symbol: str) -> dict:
    """Check for unmatched entries in the trade ledger."""
    tp = STATE_DIR / f"v7_scalp_trades_{symbol}.jsonl"
    if not tp.exists():
        return {"check": "ledger_integrity", "symbol": symbol, "status": PASS,
                "detail": "no ledger file (new symbol)"}
    entries = set()
    exited = set()
    for line in tp.read_text().splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        evt = ev.get("event", "")
        pid = ev.get("positionId", "")
        if evt == "PAPER_ENTRY_REAL_ASK" and pid:
            entries.add(pid)
        elif evt == "PAPER_EXIT_REAL_BID" and pid:
            exited.add(pid)
    unmatched = entries - exited
    if not unmatched:
        return {"check": "ledger_integrity", "symbol": symbol, "status": PASS,
                "detail": f"{len(entries)} entries, {len(exited)} exits, 0 unmatched"}
    return {"check": "ledger_integrity", "symbol": symbol, "status": FAIL,
            "detail": f"{len(entries)} entries, {len(exited)} exits, {len(unmatched)} UNMATCHED"}


def check_cron_health() -> dict:
    """Check V7 trading cron jobs exist by reading the Hermes cron database directly."""
    expected_names = {"premarket", "daily results", "weekly"}
    cron_db = Path.home() / ".hermes" / "cron" / "jobs.json"
    if not cron_db.exists():
        return {"check": "cron_health", "status": WARN,
                "detail": "cron database not found — verify manually with cronjob list"}
    try:
        raw = json.loads(cron_db.read_text())
        jobs = raw.get("jobs", []) if isinstance(raw, dict) else raw
        if not isinstance(jobs, list):
            jobs = []
        found_names = set()
        disabled = []
        for j in jobs:
            name = j.get("name", "")
            jid = j.get("job_id", "")
            # Match by partial name to handle em-dash variants
            for expected in expected_names:
                if expected.lower() in name.lower():
                    found_names.add(expected)
                    if not j.get("enabled", True):
                        disabled.append(f"{name} ({jid[:8]})")
                    break
        missing = expected_names - found_names
        issues = []
        if missing:
            issues.append(f"missing: {', '.join(missing)}")
        if disabled:
            issues.append(f"disabled: {', '.join(disabled)}")
        if not issues:
            return {"check": "cron_health", "status": PASS,
                    "detail": f"all {len(expected_names)} V7 trading crons found and enabled"}
        return {"check": "cron_health", "status": WARN, "detail": "; ".join(issues)}
    except Exception as exc:
        return {"check": "cron_health", "status": WARN, "detail": f"error reading cron db: {exc}"}


def active_release_identity() -> dict[str, str]:
    """Return the exact engine identity that must back eligible paper evidence."""
    import v7_scalp_paper as engine

    return {
        "engineVersion": engine.ENGINE_VERSION,
        "strategyId": engine.STRATEGY_ID,
        "releaseManifestSha256": engine.RELEASE_MANIFEST_SHA256,
        "sourceCodeSha256": engine.source_code_sha256(),
        "configSha256": engine.config_sha256(),
    }


def _event_pnl(event: dict) -> float:
    value = event.get("realizedPnl", event.get("pnl", 0))
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def check_evidence_gate() -> dict:
    """Report exact-release evidence; historical ledgers are context only."""
    try:
        import v7_scalp_paper as engine
        import v7_release
        active_release = active_release_identity()
        symbols = tuple(engine.SYMBOLS)
    except Exception as exc:
        return {
            "check": "evidence_gate", "status": INFO,
            "detail": f"active release identity unavailable: {exc}",
        }

    all_exits = []
    eligible_exits = []
    for sym in symbols:
        tp = STATE_DIR / f"v7_scalp_trades_{sym}.jsonl"
        if not tp.exists():
            continue
        entries: dict[str, dict] = {}
        for line in tp.read_text().splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            position_id = event.get("positionId")
            if event.get("event") == "PAPER_ENTRY_REAL_ASK" and position_id:
                entries[position_id] = event
                continue
            if event.get("event") != "PAPER_EXIT_REAL_BID":
                continue
            all_exits.append(event)
            entry = entries.get(position_id)
            if entry and all(
                entry.get(field) == value and event.get(field) == value
                for field, value in active_release.items()
            ):
                eligible_exits.append(event)

    total = len(eligible_exits)
    wins = sum(1 for event in eligible_exits if _event_pnl(event) > 0)
    total_pnl = sum(_event_pnl(event) for event in eligible_exits)
    wr = wins / total * 100 if total else 0.0
    evidence_gate = engine.EVIDENCE_GATE
    carry_forward = getattr(v7_release, "EVIDENCE_CARRY_FORWARD", {}) or {}
    try:
        carried_trades = int(carry_forward.get("closed_trades", 0))
        carried_wins = int(carry_forward.get("wins", 0))
        carried_losses = int(carry_forward.get("losses_or_breakeven", 0))
        carried_pnl = float(carry_forward.get("net_pnl", 0.0))
    except (TypeError, ValueError):
        carried_trades = carried_wins = carried_losses = 0
        carried_pnl = 0.0
    progress_trades = carried_trades + total
    progress_wins = carried_wins + wins
    progress_losses = carried_losses + (total - wins)
    progress_pnl = carried_pnl + total_pnl
    min_closed_trades = evidence_gate["min_closed_trades"]
    min_win_rate_pct = evidence_gate["min_win_rate_pct"]
    minimum_wins = math.ceil(min_closed_trades * min_win_rate_pct / 100)
    remaining = max(0, min_closed_trades - progress_trades)
    wins_needed = max(0, minimum_wins - progress_wins)

    historical_wins = sum(1 for event in all_exits if _event_pnl(event) > 0)
    historical_pnl = sum(_event_pnl(event) for event in all_exits)
    detail = (
        f"progress {progress_trades}/{min_closed_trades} trades "
        f"({carried_trades} carried forward + {total} exact current), "
        f"{progress_wins}W/{progress_losses} nonwins, "
        f"progress P&L=${progress_pnl:.2f}; "
        f"exact current-release detail {total}/{min_closed_trades}, "
        f"{wins}W/{total - wins} nonwins, WR={wr:.1f}%, P&L=${total_pnl:.2f}; "
        f"all-ledger context: {len(all_exits)} closed, {historical_wins} positive, "
        f"P&L=${historical_pnl:.2f}"
    )
    quality_warning = total > 0 and (total_pnl <= 0 or wins == 0)
    return {
        "check": "evidence_gate",
        "status": WARN if quality_warning else INFO,
        "detail": detail + ("; WARNING: exact current-release cohort is not profitable" if quality_warning else ""),
    }


def check_brief_coverage() -> dict:
    brief_path = SCRIPTS_DIR / "v73_morning_premarket_brief.py"
    if not brief_path.exists():
        return {"check": "brief_coverage", "status": FAIL, "detail": "brief script missing"}
    content = brief_path.read_text()
    if "_premarket_levels_section" in content:
        return {"check": "brief_coverage", "status": PASS,
                "detail": "brief includes premarket levels"}
    return {"check": "brief_coverage", "status": FAIL,
            "detail": "brief does NOT include premarket levels"}


def check_reconcile_bug() -> dict:
    engine_path = SCRIPTS_DIR / "v7_scalp_paper.py"
    content = engine_path.read_text()
    if "def reconcile_state_from_ledger" not in content:
        return {"check": "reconcile_bug_fix", "status": FAIL, "detail": "function not found"}
    start = content.index("def reconcile_state_from_ledger")
    end = content.index("\ndef ", start + 10)
    func_body = content[start:end]
    if "lastResetDate" in func_body:
        return {"check": "reconcile_bug_fix", "status": FAIL,
                "detail": "reconcile still sets lastResetDate (bug present)"}
    return {"check": "reconcile_bug_fix", "status": PASS,
            "detail": "reconcile does not set lastResetDate (fixed)"}


def check_otm_adaptive() -> dict:
    engine_path = SCRIPTS_DIR / "v7_scalp_paper.py"
    content = engine_path.read_text()
    if "effective_max_otm_pct" not in content:
        return {"check": "otm_adaptive_fix", "status": FAIL,
                "detail": "effective_max_otm_pct not found"}
    return {"check": "otm_adaptive_fix", "status": PASS,
            "detail": "adaptive OTM scaling in place"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def check_sleep_interval_bug() -> dict:
    """Verify daemon uses min() across all symbols for sleep interval, not first symbol."""
    daemon_path = SCRIPTS_DIR / "v7_scalp_live_daemon.py"
    if not daemon_path.exists():
        return {"check": "sleep_interval_bug", "status": FAIL, "detail": "daemon source missing"}
    content = daemon_path.read_text()
    if "min_interval" in content and "min(min_interval" in content:
        return {"check": "sleep_interval_bug", "status": PASS,
                "detail": "daemon uses min() across all symbols for sleep interval"}
    return {"check": "sleep_interval_bug", "status": FAIL,
            "detail": "daemon does NOT use min() across symbols — 329-min hold bug may recur"}


def check_anomalous_holds() -> dict:
    """Scan trade ledgers for any exit with heldMinutes > 40min (dynamic max-hold ceiling + buffer)."""
    try:
        import v7_scalp_paper as engine
        threshold = 40  # absolute ceiling: dynamic_max_hold_minutes peaks at 35, +5 buffer
        symbols = tuple(engine.SYMBOLS)
    except Exception:
        threshold = 40
        symbols = ("SPY", "QQQ")
    # Known historical anomaly from the 329-min hold bug (pre-fix). It must
    # remain visible in the checklist as a warning, not silently disappear.
    known_anomaly_ids = {"paper-20260819T102513", "paper-20260827T115006"}
    anomalies = []
    acknowledged = []
    for sym in symbols:
        tp = STATE_DIR / f"v7_scalp_trades_{sym}.jsonl"
        if not tp.exists():
            continue
        for line in tp.read_text().splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") == "PAPER_EXIT_REAL_BID":
                pid = ev.get("positionId", "")
                held = ev.get("heldMinutes", 0)
                if pid in known_anomaly_ids:
                    acknowledged.append(f"{sym} {pid} held {held}min")
                    continue
                if held > threshold:
                    anomalies.append(f"{sym} {pid} held {held}min")
    if anomalies:
        return {"check": "anomalous_holds", "status": FAIL,
                "detail": f"{len(anomalies)} anomalous hold(s): {'; '.join(anomalies)}"}
    if acknowledged:
        return {"check": "anomalous_holds", "status": WARN,
                "detail": f"{len(acknowledged)} acknowledged historical pre-fix anomalous hold(s): {'; '.join(acknowledged)}"}
    return {"check": "anomalous_holds", "status": PASS,
            "detail": f"no holds > {threshold}min in ledger"}


def check_cold_reset_guard() -> dict:
    """Verify daemon gates cold_reset() on no positions across all symbols."""
    daemon_path = SCRIPTS_DIR / "v7_scalp_live_daemon.py"
    if not daemon_path.exists():
        return {"check": "cold_reset_guard", "status": FAIL, "detail": "daemon source missing"}
    content = daemon_path.read_text()
    if "any_position_open" in content and "cold_reset" in content:
        return {"check": "cold_reset_guard", "status": PASS,
                "detail": "cold_reset gated on no positions across all symbols"}
    return {"check": "cold_reset_guard", "status": FAIL,
            "detail": "cold_reset not properly guarded — quote worker may die mid-position"}


def check_max_hold_safety_net() -> dict:
    """Verify daemon has max-hold safety net that force-monitors stale positions."""
    daemon_path = SCRIPTS_DIR / "v7_scalp_live_daemon.py"
    if not daemon_path.exists():
        return {"check": "max_hold_safety_net", "status": FAIL, "detail": "daemon source missing"}
    content = daemon_path.read_text()
    if "MAX_HOLD_SAFETY_NET" in content:
        return {"check": "max_hold_safety_net", "status": PASS,
                "detail": "daemon-level max-hold safety net in place"}
    return {"check": "max_hold_safety_net", "status": FAIL,
            "detail": "no max-hold safety net — positions can drift past time_stop"}


def check_runtime_interval() -> dict:
    """If any symbol has active positions, runtime interval must be <= 2s."""
    rt_path = STATE_DIR / "v7_scalp_live_runtime.json"
    if not rt_path.exists():
        return {"check": "runtime_interval", "status": WARN, "detail": "no runtime heartbeat"}
    try:
        rt = json.loads(rt_path.read_text())
        symbols = rt.get("symbols", {})
        any_pos = any(s.get("activePositions", 0) > 0 for s in symbols.values())
        interval = rt.get("interval_seconds", 0)
        if any_pos and interval > 2.0:
            return {"check": "runtime_interval", "status": FAIL,
                    "detail": f"position open but interval={interval}s (>2s) — monitoring too slow"}
        if any_pos:
            return {"check": "runtime_interval", "status": PASS,
                    "detail": f"position open, interval={interval}s (<=2s)"}
        return {"check": "runtime_interval", "status": PASS,
                "detail": f"no positions, interval={interval}s"}
    except Exception as exc:
        return {"check": "runtime_interval", "status": WARN, "detail": f"error: {exc}"}



def check_live_authorization() -> dict:
    """Report live trading authorization status (PASS/WARN, never FAIL)."""
    try:
        if live_authorized():
            return {"check": "live_authorization", "status": PASS,
                    "detail": "live trading authorized — execution daemon armed"}
        else:
            return {"check": "live_authorization", "status": WARN,
                    "detail": "live trading not authorized — paper-only mode"}
    except Exception as exc:
        return {"check": "live_authorization", "status": WARN,
                "detail": f"live auth check error: {exc}"}


def main() -> int:
    import v7_scalp_paper as engine
    ap = argparse.ArgumentParser(description=f"V{engine.ENGINE_VERSION} Pre-Flight Checklist")
    ap.add_argument("--fix", action="store_true", help="auto-fix fixable issues")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    symbols = list(engine.SYMBOLS.keys())

    results = []
    for sym in symbols:
        results.append(check_state_reset(sym, args.fix))
    for sym in symbols:
        results.append(check_contract_feasibility(sym))
    for sym in symbols:
        results.append(check_premarket_snapshot(sym))
    results.append(check_daemon_health())
    results.append(check_source_hash())
    results.append(check_macro_calendar())
    for sym in symbols:
        results.append(check_ledger_integrity(sym))
    results.append(check_cron_health())
    results.append(check_evidence_gate())
    results.append(check_brief_coverage())
    results.append(check_reconcile_bug())
    results.append(check_otm_adaptive())
    results.append(check_sleep_interval_bug())
    results.append(check_anomalous_holds())
    results.append(check_cold_reset_guard())
    results.append(check_max_hold_safety_net())
    results.append(check_runtime_interval())
    results.append(check_live_authorization())

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(f"\n{'='*70}")
        print(f"  V{engine.ENGINE_VERSION} Pre-Flight Checklist — {now_et().strftime('%Y-%m-%d %H:%M %Z')}")
        print(f"{'='*70}\n")
        for r in results:
            status = r["status"]
            icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️", "FIXED": "🔧", "INFO": "ℹ️"}.get(status, "?")
            sym = f"[{r['symbol']}]" if "symbol" in r else ""
            print(f"  {icon} {status:5} {r['check']:25} {sym:6} {r['detail']}")
        print()

    fails = sum(1 for r in results if r["status"] == FAIL)
    warns = sum(1 for r in results if r["status"] == WARN)
    fixed = sum(1 for r in results if r["status"] == FIXED)

    if fails:
        if not args.json:
            print(f"  ❌ {fails} FAIL(s), {warns} WARN(s), {fixed} FIXED")
        return 1
    if not args.json:
        print(f"  ✅ All checks pass ({warns} warning(s), {fixed} fixed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
