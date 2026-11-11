#!/usr/bin/env python3
"""Fail-closed V7 system health gate.

This module is intentionally separate from the V7 strategy engine. It reads
runtime, release, calendar, state, quote, and ledger evidence. A critical
failure creates the existing kill switch so new paper entries stop. It never
changes strategy code and never removes the kill switch automatically.

Use ``--resume`` only after the reported issue has been fixed and a clean
health evaluation is available.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SCRIPTS_DIR = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".hermes" / "state"
DAEMON_LABEL = "com.hermes.v73-paper-live-daemon"
RUNTIME_PATH = STATE_DIR / "v7_scalp_live_runtime.json"
KILL_SWITCH_PATH = STATE_DIR / "v7_kill_switch.json"
HEALTH_STATE_PATH = STATE_DIR / "v7_system_health.json"
HEALTH_LOG_PATH = STATE_DIR / "v7_system_health.jsonl"
HEARTBEAT_MAX_AGE_SECONDS = 120
STATE_MAX_AGE_SECONDS = 300
ACTIVE_QUOTE_MAX_AGE_SECONDS = 30
HEALTH_OWNED_KILL_SWITCH_PREFIXES = ("health_gate:", "health_responder:")
# Shared live-trading authorization — single source of truth in v7_live_auth.py
sys.path.insert(0, str(SCRIPTS_DIR))
from v7_live_auth import live_authorized  # noqa: E402


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def now_et() -> datetime:
    return datetime.now(ET)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp_missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp_missing_timezone")
    return parsed.astimezone(ET)


def _age_seconds(value: Any, *, now: datetime) -> float:
    return (now - _parse_timestamp(value)).total_seconds()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name}_must_be_object")
    return value


def health_owned_kill_switch() -> bool:
    """Return true only for a valid kill switch created by health automation."""
    try:
        payload = _read_json(KILL_SWITCH_PATH)
    except Exception:
        return False
    reason = payload.get("reason")
    return (
        payload.get("active") is True
        and isinstance(reason, str)
        and any(reason.startswith(prefix) for prefix in HEALTH_OWNED_KILL_SWITCH_PREFIXES)
    )


def kill_switch_active() -> bool:
    """Return whether the shared kill switch currently blocks trading."""
    if not KILL_SWITCH_PATH.exists():
        return False
    try:
        payload = _read_json(KILL_SWITCH_PATH)
    except Exception:
        return True
    return payload.get("active") is not False


def _health_result(name: str, failures: list[str], detail: str = "") -> dict[str, Any]:
    return {
        "check": name,
        "healthy": not failures,
        "failures": failures,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def inspect_runtime(
    runtime: dict[str, Any],
    *,
    now: datetime | None = None,
    max_age_seconds: int = HEARTBEAT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Validate the daemon heartbeat and paper-only invariants."""
    current = now or now_et()
    failures: list[str] = []
    details: list[str] = []

    try:
        age = _age_seconds(runtime.get("updatedAt"), now=current)
        if age < -60:
            failures.append("heartbeat_in_future")
            details.append(f"heartbeat is {abs(age):.0f}s in the future")
        elif age > max_age_seconds:
            failures.append("heartbeat_stale")
            details.append(f"heartbeat age={age:.0f}s > {max_age_seconds}s")
        else:
            details.append(f"heartbeat age={max(0.0, age):.1f}s")
    except (TypeError, ValueError) as exc:
        failures.append("heartbeat_invalid")
        details.append(str(exc))

    if not isinstance(runtime.get("processId"), int) or runtime.get("processId", 0) <= 0:
        failures.append("process_missing")
    if runtime.get("consecutiveFailures") != 0:
        failures.append("consecutive_failures")
        details.append(f"consecutiveFailures={runtime.get('consecutiveFailures')}")
    if runtime.get("liveOrdersEnabled") is not False:
        if not live_authorized():
            failures.append("live_orders_enabled")
    if not isinstance(runtime.get("mode"), str) or not runtime.get("mode"):
        failures.append("runtime_mode_missing")

    return _health_result("runtime", failures, "; ".join(details))


def check_launchd() -> dict[str, Any]:
    """Confirm launchd owns a running daemon process."""
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{DAEMON_LABEL}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return _health_result("launchd", ["launchctl_error"], str(exc))

    failures: list[str] = []
    if result.returncode != 0:
        failures.append("launchagent_not_found")
    elif "state = running" not in result.stdout:
        failures.append("daemon_not_running")
    return _health_result("launchd", failures, "running" if not failures else result.stderr.strip())


def check_release_identity(runtime: dict[str, Any]) -> dict[str, Any]:
    """Require frozen runtime identity to match the current deployed files."""
    try:
        sys.path.insert(0, str(SCRIPTS_DIR))
        import v7_scalp_paper as engine

        engine_path = SCRIPTS_DIR / "v7_scalp_paper.py"
        daemon_path = SCRIPTS_DIR / "v7_scalp_live_daemon.py"
        expected = {
            "engineVersion": engine.ENGINE_VERSION,
            "strategyId": engine.STRATEGY_ID,
            "engineSourceSha256": hashlib.sha256(engine_path.read_bytes()).hexdigest(),
            "daemonSourceSha256": hashlib.sha256(daemon_path.read_bytes()).hexdigest(),
            "configSha256": engine.config_sha256(),
            "releaseManifestSha256": engine.RELEASE_MANIFEST_SHA256,
        }
    except Exception as exc:
        return _health_result("release_identity", ["release_identity_unavailable"], str(exc))

    failures = [
        f"{key}_mismatch"
        for key, value in expected.items()
        if runtime.get(key) != value
    ]
    return _health_result(
        "release_identity",
        failures,
        "all frozen runtime hashes match disk" if not failures else "; ".join(failures),
    )


def check_calendar() -> dict[str, Any]:
    """Reuse the engine-aligned preflight calendar validator."""
    try:
        sys.path.insert(0, str(SCRIPTS_DIR))
        import v7_preflight_check as preflight

        result = preflight.check_macro_calendar()
    except Exception as exc:
        return _health_result("macro_calendar", ["calendar_check_error"], str(exc))
    if result.get("status") != preflight.PASS:
        return _health_result("macro_calendar", ["calendar_not_healthy"], result.get("detail", ""))
    return _health_result("macro_calendar", [], result.get("detail", ""))


def _ledger_events(events: str) -> tuple[set[str], set[str], list[str]]:
    entries: set[str] = set()
    exits: set[str] = set()
    malformed: list[str] = []
    for line_number, line in enumerate(events.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed.append(str(line_number))
            continue
        if not isinstance(event, dict):
            malformed.append(str(line_number))
            continue
        position_id = event.get("positionId")
        if not position_id:
            continue
        if event.get("event") == "PAPER_ENTRY_REAL_ASK":
            entries.add(str(position_id))
        elif event.get("event") == "PAPER_EXIT_REAL_BID":
            exits.add(str(position_id))
    return entries, exits, malformed


def inspect_ledger(symbol: str, state: dict[str, Any], events: str) -> dict[str, Any]:
    """Allow open positions, but reject true orphan or malformed ledger data."""
    entries, exits, malformed = _ledger_events(events)
    active_ids = {
        str(position.get("positionId"))
        for position in state.get("activePositions", [])
        if isinstance(position, dict) and position.get("positionId")
    }
    orphaned = sorted((entries - exits) - active_ids)
    unmatched_active = sorted((entries - exits) & active_ids)
    exit_without_entry = sorted(exits - entries)
    failures: list[str] = []
    if orphaned:
        failures.append("orphaned_entry")
    if exit_without_entry:
        failures.append("exit_without_entry")
    if malformed:
        failures.append("malformed_ledger")
    detail = (
        f"{len(entries)} entries, {len(exits)} exits, "
        f"{len(unmatched_active)} active unmatched, {len(orphaned)} orphaned"
    )
    return {
        "check": "ledger",
        "symbol": symbol,
        "healthy": not failures,
        "failures": failures,
        "detail": detail,
        "entries": sorted(entries),
        "exits": sorted(exits),
        "active": sorted(active_ids),
        "unmatched_active": unmatched_active,
        "orphaned": orphaned,
        "exit_without_entry": exit_without_entry,
        "malformed_lines": malformed,
    }


def _check_position_quotes(symbol: str, state: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    failures: list[str] = []
    details: list[str] = []
    for position in state.get("activePositions", []):
        if not isinstance(position, dict):
            failures.append("position_invalid")
            continue
        try:
            age = _age_seconds(
                (position.get("lastQuote") or {}).get("quoteTimestamp")
                or (position.get("lastQuote") or {}).get("at"),
                now=now,
            )
        except (TypeError, ValueError) as exc:
            failures.append(f"position_quote_invalid:{symbol}")
            details.append(str(exc))
            continue
        if age < -60 or age > ACTIVE_QUOTE_MAX_AGE_SECONDS:
            failures.append(f"position_quote_stale:{symbol}")
            details.append(f"age={age:.1f}s")
    return _health_result(f"position_quotes:{symbol}", failures, "; ".join(details))


def check_states_and_ledgers(
    runtime: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate every registered symbol without mutating any strategy state."""
    current = now or now_et()
    try:
        sys.path.insert(0, str(SCRIPTS_DIR))
        import v7_scalp_paper as engine
        symbols = tuple(engine.SYMBOLS)
    except Exception as exc:
        return _health_result("states", ["engine_registry_unavailable"], str(exc)), []

    failures: list[str] = []
    details: list[str] = []
    child_results: list[dict[str, Any]] = []
    any_position = False
    for symbol in symbols:
        state_path = STATE_DIR / f"v7_scalp_state_{symbol}.json"
        ledger_path = STATE_DIR / f"v7_scalp_trades_{symbol}.jsonl"
        try:
            state = _read_json(state_path)
        except Exception as exc:
            child = _health_result(f"state:{symbol}", [f"state_unavailable:{symbol}"], str(exc))
            child_results.append(child)
            failures.extend(child["failures"])
            continue

        active = state.get("activePositions")
        if not isinstance(active, list):
            failures.append(f"active_positions_invalid:{symbol}")
        any_position = any_position or bool(active)
        if state.get("symbol") != symbol:
            failures.append(f"state_symbol_mismatch:{symbol}")
        if state.get("liveOrdersEnabled") is not False:
            if not live_authorized():
                failures.append(f"live_orders_enabled:{symbol}")
        try:
            expected_session = engine.trading_session_key(symbol, current)
            if state.get("lastResetDate") != expected_session:
                failures.append(f"state_reset_stale:{symbol}")
        except Exception as exc:
            failures.append(f"state_session_error:{symbol}")
            details.append(str(exc))
        if active:
            try:
                age = _age_seconds(state.get("updatedAt"), now=current)
                if age < -60 or age > STATE_MAX_AGE_SECONDS:
                    failures.append(f"state_stale:{symbol}")
            except (TypeError, ValueError) as exc:
                failures.append(f"state_timestamp_invalid:{symbol}")
                details.append(str(exc))

        events = ledger_path.read_text(encoding="utf-8") if ledger_path.exists() else ""
        ledger = inspect_ledger(symbol, state, events)
        child_results.append(ledger)
        failures.extend(f"{symbol}:{failure}" for failure in ledger["failures"])
        if active:
            quote_result = _check_position_quotes(symbol, state, now=current)
            child_results.append(quote_result)
            failures.extend(quote_result["failures"])

    if any_position:
        try:
            interval = float(runtime.get("interval_seconds"))
            if interval > 2.0:
                failures.append("position_monitor_interval_slow")
                details.append(f"interval={interval}s")
        except (TypeError, ValueError):
            failures.append("position_monitor_interval_invalid")

    return _health_result("states", failures, "; ".join(details)), child_results


def _quote_timestamp(quote: dict[str, Any]) -> Any:
    return (
        quote.get("venueTimestamp")
        or quote.get("venue_timestamp")
        or quote.get("quoteTimestamp")
        or quote.get("providerTimestamp")
        or quote.get("timestamp")
        or quote.get("fetchedAt")
        or quote.get("fetched_at")
    )


def check_live_equity_quotes(
    runtime: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Require fresh quotes only for symbols currently consuming them."""
    current = now or now_et()
    mode = runtime.get("mode")
    if mode not in {"active_scan", "opening_watch"}:
        return _health_result("live_equity_quotes", [], f"not required in mode={mode}")

    try:
        sys.path.insert(0, str(SCRIPTS_DIR))
        import v7_scalp_paper as engine
    except Exception as exc:
        return _health_result("live_equity_quotes", ["engine_unavailable"], str(exc))

    symbol_statuses = runtime.get("symbols")

    def symbol_consumes_equity_quote(symbol: str) -> bool:
        # The daemon publishes a per-symbol action. Require an equity quote only
        # when that symbol is about to scan or track a premarket target. A symbol
        # being monitored from an open option position consumes option quotes,
        # not the underlying equity quote, and a post-trade/off-hours symbol is
        # intentionally idle.
        status = symbol_statuses.get(symbol) if isinstance(symbol_statuses, dict) else None
        if isinstance(status, dict) and "action" in status:
            if status.get("activePositions", 0) > 0:
                return False
            return status.get("action") in {"scan", "premarket"}
        if mode == "opening_watch":
            if engine.is_index_option_symbol(symbol):
                return engine.xsp_market_open(current)
            return True
        return engine.entry_window_open(current, symbol)

    required = [symbol for symbol in engine.SYMBOLS if symbol_consumes_equity_quote(symbol)]
    if not required:
        return _health_result(
            "live_equity_quotes", [], f"no symbol quote window open in mode={mode}"
        )

    quotes = runtime.get("liveEquity")
    if not isinstance(quotes, dict) or not quotes:
        return _health_result(
            "live_equity_quotes",
            ["live_equity_missing"],
            f"quote windows open for {','.join(required)} but runtime has no live equity quotes",
        )

    failures: list[str] = []
    details: list[str] = []
    for symbol in required:
        quote = quotes.get(symbol)
        if not isinstance(quote, dict):
            failures.append(f"equity_quote_missing:{symbol}")
            continue
        try:
            age = _age_seconds(_quote_timestamp(quote), now=current)
        except (TypeError, ValueError) as exc:
            failures.append(f"equity_quote_invalid:{symbol}")
            details.append(str(exc))
            continue
        if age < -60 or age > ACTIVE_QUOTE_MAX_AGE_SECONDS:
            failures.append(f"equity_quote_stale:{symbol}")
            details.append(f"{symbol} age={age:.1f}s")

    return _health_result(
        "live_equity_quotes",
        failures,
        "; ".join(details) if details else f"required={','.join(required)}",
    )


def check_premarket_targets(
    runtime: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Require target snapshots only for equity symbols using them now."""
    if runtime.get("mode") not in {"premarket_watch", "opening_watch", "active_scan"}:
        return _health_result("premarket_targets", [], f"not required in mode={runtime.get('mode')}")
    try:
        sys.path.insert(0, str(SCRIPTS_DIR))
        import v7_preflight_check as preflight
        import v7_scalp_paper as engine
    except Exception as exc:
        return _health_result("premarket_targets", ["premarket_check_unavailable"], str(exc))

    current = (now or now_et()).astimezone(ET)

    def symbol_consumes_premarket_target(symbol: str) -> bool:
        if engine.is_index_option_symbol(symbol) or current.weekday() >= 5:
            return False
        tm = current.hour * 100 + current.minute
        if engine.PREMARKET_WATCH_START <= tm < engine.MARKET_OPEN:
            return True
        if engine.MARKET_OPEN <= tm < engine.ENTRY_START:
            return True
        return engine.entry_window_open(current, symbol)

    required = [
        symbol
        for symbol in engine.SYMBOLS
        if symbol_consumes_premarket_target(symbol)
    ]
    if not required:
        return _health_result(
            "premarket_targets",
            [],
            f"not required for symbol session windows in mode={runtime.get('mode')}",
        )

    failures: list[str] = []
    details: list[str] = []
    for symbol in required:
        result = preflight.check_premarket_snapshot(symbol)
        if result.get("status") != preflight.PASS:
            failures.append(f"premarket_target_unhealthy:{symbol}")
            details.append(f"{symbol}: {result.get('detail', '')}")
    return _health_result("premarket_targets", failures, "; ".join(details))


def check_live_execution() -> dict[str, Any]:
    """Report live execution daemon and authorization status.

    WARN-only: never adds to failures. A transient execution-daemon restart
    during a KeepAlive respawn must not trigger pause_trading() and halt paper
    entries via the shared kill switch.
    """
    detail_parts: list[str] = []
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/com.hermes.v7-live-execution-daemon"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        daemon_running = result.returncode == 0 and "state = running" in result.stdout
        detail_parts.append(f"execution_daemon={'running' if daemon_running else 'not_running'}")
    except Exception as exc:
        daemon_running = False
        detail_parts.append(f"execution_daemon_check_error:{exc}")
    try:
        from v7_live_auth import live_authorized
        authorized = live_authorized()
        detail_parts.append(f"live_authorized={authorized}")
    except Exception as exc:
        authorized = False
        detail_parts.append(f"live_authorized_check_error:{exc}")
    return _health_result("live_execution", [], "; ".join(detail_parts))


def evaluate_health(*, now: datetime | None = None) -> dict[str, Any]:
    """Return a complete read-only health report."""
    current = now or now_et()
    checks: list[dict[str, Any]] = []
    failures: list[str] = []

    launchd = check_launchd()
    checks.append(launchd)
    failures.extend(launchd["failures"])

    try:
        runtime = _read_json(RUNTIME_PATH)
    except Exception as exc:
        runtime = {}
        runtime_result = _health_result("runtime", ["runtime_unavailable"], str(exc))
    else:
        runtime_result = inspect_runtime(runtime, now=current)
    checks.append(runtime_result)
    failures.extend(runtime_result["failures"])

    release = check_release_identity(runtime)
    checks.append(release)
    failures.extend(release["failures"])

    calendar = check_calendar()
    checks.append(calendar)
    failures.extend(calendar["failures"])

    states, children = check_states_and_ledgers(runtime, now=current)
    checks.append(states)
    checks.extend(children)
    failures.extend(states["failures"])

    quote_result = check_live_equity_quotes(runtime, now=current)
    checks.append(quote_result)
    failures.extend(quote_result["failures"])

    premarket = check_premarket_targets(runtime, now=current)
    checks.append(premarket)
    failures.extend(premarket["failures"])

    live_exec = check_live_execution()
    checks.append(live_exec)
    # Intentionally do NOT extend failures — live execution is WARN-only.

    live_authorized_status = False
    try:
        from v7_live_auth import live_authorized
        live_authorized_status = live_authorized()
    except Exception:
        pass

    return {
        "checkedAt": current.isoformat(),
        "healthy": not failures,
        "failures": failures,
        "checks": checks,
        "runtime": {
            key: runtime.get(key)
            for key in ("processId", "engineVersion", "strategyId", "mode", "interval_seconds", "consecutiveFailures", "liveOrdersEnabled")
        },
        "liveAuthorized": live_authorized_status,
    }


# ---------------------------------------------------------------------------
# Fail-closed control
# ---------------------------------------------------------------------------


def pause_trading(failures: list[str], *, now: datetime | None = None) -> None:
    """Create the shared kill switch, preserving an existing active reason."""
    current = now or now_et()
    if KILL_SWITCH_PATH.exists():
        try:
            existing = _read_json(KILL_SWITCH_PATH)
            if existing.get("active") is True:
                return
        except Exception:
            pass
    payload = {
        "active": True,
        "reason": "health_gate:" + ",".join(failures[:12]),
        "timestamp": current.isoformat(),
    }
    _atomic_write_json(KILL_SWITCH_PATH, payload)


def resume_trading(report: dict[str, Any]) -> bool:
    """Remove the kill switch only when a clean report was explicitly supplied."""
    if report.get("healthy") is not True or report.get("failures"):
        return False
    if kill_switch_active():
        KILL_SWITCH_PATH.unlink()
    return True


def _record_report(report: dict[str, Any]) -> None:
    report = dict(report)
    report["tradingPaused"] = kill_switch_active()
    _atomic_write_json(HEALTH_STATE_PATH, report)
    HEALTH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HEALTH_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, separators=(",", ":")) + "\n")


def _print_failure(report: dict[str, Any]) -> None:
    print(f"V7 SYSTEM HEALTH GATE PAUSED — {report['checkedAt']}")
    for failure in report.get("failures", []):
        print(f"FAIL: {failure}")
    print("Trading is paused. Fix the reported issue, rerun the health check, then use --resume.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V7 fail-closed system health gate")
    parser.add_argument("--json", action="store_true", help="print the complete JSON report")
    parser.add_argument("--resume", action="store_true", help="remove the kill switch only after a clean check")
    args = parser.parse_args(argv)

    report = evaluate_health()
    if not report["healthy"]:
        pause_trading(report["failures"])
        _record_report(report)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_failure(report)
        return 1

    if args.resume:
        resumed = resume_trading(report)
        report["tradingPaused"] = not resumed
        _record_report(report)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print("V7 SYSTEM HEALTH GATE HEALTHY — trading resumed" if resumed else "V7 SYSTEM HEALTH GATE PAUSED")
        return 0 if resumed else 1

    if KILL_SWITCH_PATH.exists() and health_owned_kill_switch():
        # The proactive responder owns health-created pauses.  Keep the
        # marker in place until it completes its two clean confirmations and
        # explicit --resume, but do not turn this expected handoff into a
        # recurring cron failure.
        report["tradingPaused"] = True
        _record_report(report)
        if args.json:
            print(json.dumps(report, indent=2))
        return 0

    if kill_switch_active():
        report["healthy"] = False
        report["failures"] = ["manual_resume_required"]
        _record_report(report)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_failure(report)
        return 1

    _record_report(report)
    if args.json:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
