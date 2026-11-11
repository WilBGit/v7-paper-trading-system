#!/usr/bin/env python3
"""Proactive, fail-closed V7 operational recovery.

This is deliberately separate from the V7 strategy engine and daemon.  It
may only perform bounded operational actions:

* preserve/create the health-gate kill switch on any unhealthy report;
* repair a stale daily state-reset marker through the existing preflight fix;
* restart the daemon only when every symbol's state proves there are no open
  positions;
* retry transient heartbeat/quote failures after one bounded restart; and
* remove the kill switch only after two clean health evaluations, no open
  positions, and a successful explicit ``--resume`` check.

It never edits strategy source, scoring, thresholds, entry/exit rules, order
state, or broker credentials.  Unknown state, data-integrity failures, live
order flags, and open positions remain paused for manual review.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SCRIPTS_DIR = Path(__file__).resolve().parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
STATE_DIR = HERMES_HOME / "state"
HEALTH_SCRIPT = SCRIPTS_DIR / "v7_system_health_check.py"
PREFLIGHT_SCRIPT = SCRIPTS_DIR / "v7_preflight_check.py"
RELEASE_SYNC_SCRIPT = SCRIPTS_DIR / "v7_release_sync.py"
RESTART_SCRIPT = SCRIPTS_DIR / "v73_restart_daemon.sh"
KILL_SWITCH_PATH = STATE_DIR / "v7_kill_switch.json"
sys.path.insert(0, str(SCRIPTS_DIR))
from v7_live_auth import live_authorized  # noqa: E402
RECOVERY_STATE_PATH = STATE_DIR / "v7_health_recovery_state.json"
RECOVERY_LATEST_PATH = STATE_DIR / "v7_health_recovery_latest.json"
RECOVERY_LOG_PATH = STATE_DIR / "v7_health_recovery.jsonl"
LOCK_PATH = STATE_DIR / "v7_health_recovery.lock"
DAEMON_LABEL = "com.hermes.v73-paper-live-daemon"

RESTART_COOLDOWN_SECONDS = 15 * 60
CLEAN_CONFIRMATIONS = 2
CLEAN_CONFIRMATION_DELAY_SECONDS = 2
HEARTBEAT_WAIT_SECONDS = 30

# Only transient operational seams are eligible for a bounded restart.  Any
# failure outside this set is intentionally left paused for manual review.
RESTARTABLE_ROOTS = {
    "launchagent_not_found",
    "daemon_not_running",
    "launchctl_error",
    "heartbeat_stale",
    "heartbeat_invalid",
    "consecutive_failures",
    "live_equity_missing",
    "equity_quote_missing",
    "equity_quote_invalid",
    "equity_quote_stale",
}
RELEASE_MISMATCH_PREFIXES = (
    "engineSourceSha256_",
    "daemonSourceSha256_",
    "configSha256_",
    "releaseManifestSha256_",
)
HEALTH_OWNED_KILL_SWITCH_PREFIXES = (
    "health_gate:",
    "health_responder:",
)

# These are data-integrity or safety-boundary failures.  They are listed
# explicitly so a future restartable check cannot accidentally override them.
NON_AUTOFIX_ROOTS = {
    "state_unavailable",
    "active_positions_invalid",
    "state_symbol_mismatch",
    "state_timestamp_invalid",
    "orphaned_entry",
    "exit_without_entry",
    "malformed_ledger",
    "position_invalid",
    "position_quote_invalid",
    "position_quote_stale",
    "position_monitor_interval_slow",
    "position_monitor_interval_invalid",
    "live_orders_enabled",
    "engine_unavailable",
    "engine_registry_unavailable",
    "release_identity_unavailable",
    "calendar_check_error",
    "calendar_not_healthy",
    "premarket_target_unhealthy",
    "premarket_check_unavailable",
    "state_session_error",
}


# ---------------------------------------------------------------------------
# Safe filesystem and subprocess helpers
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
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name}_must_be_object")
    return value


def _redact(text: str) -> str:
    """Keep diagnostics useful without persisting credential-like values."""
    text = re.sub(
        r"(?i)(authorization|bearer|token|secret|password|api[_-]?key)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )
    return text[-2000:]


def _run(command: list[str], *, timeout: int) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "returncode": result.returncode,
            "stdout": _redact(result.stdout or ""),
            "stderr": _redact(result.stderr or ""),
        }
    except Exception as exc:
        return {"returncode": None, "stdout": "", "stderr": _redact(str(exc))}


@contextlib.contextmanager
def _single_runner():
    """Prevent the health gate and recovery task from overlapping recovery."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_recovery_state() -> dict[str, Any]:
    if not RECOVERY_STATE_PATH.exists():
        return {}
    try:
        return _read_json(RECOVERY_STATE_PATH)
    except Exception:
        # A corrupt responder state must not be used to justify a restart.
        return {"state_invalid": True}


def _save_recovery_state(state: dict[str, Any]) -> None:
    _atomic_write_json(RECOVERY_STATE_PATH, state)


def _record_event(event: dict[str, Any]) -> None:
    safe_event = dict(event)
    _atomic_write_json(RECOVERY_LATEST_PATH, safe_event)
    RECOVERY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RECOVERY_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe_event, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


# ---------------------------------------------------------------------------
# Health, position, and action decisions
# ---------------------------------------------------------------------------


def _failure_root(failure: str) -> str:
    return str(failure).split(":", 1)[0]


def _is_release_mismatch(failure: str) -> bool:
    return any(str(failure).startswith(prefix) for prefix in RELEASE_MISMATCH_PREFIXES)


def _is_restartable_failure(failure: str) -> bool:
    root = _failure_root(failure)
    return root in RESTARTABLE_ROOTS or _is_release_mismatch(str(failure))


def _has_non_autofix_failure(failures: list[str]) -> bool:
    for failure in failures:
        root = _failure_root(failure)
        if root in NON_AUTOFIX_ROOTS:
            return True
    return False


def _failure_fingerprint(failures: list[str]) -> str:
    return "|".join(sorted(str(item) for item in failures)) or "healthy"


def _positions_snapshot() -> dict[str, Any]:
    """Prove whether all registered symbol state files have no open positions."""
    try:
        sys.path.insert(0, str(SCRIPTS_DIR))
        import v7_scalp_paper as engine

        symbols = tuple(engine.SYMBOLS)
    except Exception as exc:
        return {"known": False, "positions": [], "errors": [f"engine_unavailable:{exc}"]}

    positions: list[dict[str, Any]] = []
    errors: list[str] = []
    for symbol in symbols:
        path = STATE_DIR / f"v7_scalp_state_{symbol}.json"
        if not path.exists():
            errors.append(f"state_missing:{symbol}")
            continue
        try:
            state = _read_json(path)
        except Exception as exc:
            errors.append(f"state_unreadable:{symbol}:{exc}")
            continue
        active = state.get("activePositions")
        if not isinstance(active, list):
            errors.append(f"active_positions_invalid:{symbol}")
            continue
        for position in active:
            if not isinstance(position, dict) or not position.get("positionId"):
                errors.append(f"position_invalid:{symbol}")
                continue
            positions.append(
                {"symbol": symbol, "positionId": str(position["positionId"])}
            )

    return {
        "known": not errors,
        "positions": positions,
        "errors": errors,
        "symbols": list(symbols),
    }


def _is_clean_report(report: dict[str, Any]) -> bool:
    runtime = report.get("runtime")
    return (
        report.get("healthy") is True
        and not report.get("failures")
        and isinstance(runtime, dict)
        and runtime.get("liveOrdersEnabled") is False
    )


def _health_owned_kill_switch() -> bool:
    """Return true only for a pause marker created by this responder."""
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


def _kill_switch_active() -> bool:
    """Return whether the shared control currently blocks paper entries."""
    if not KILL_SWITCH_PATH.exists():
        return False
    try:
        payload = _read_json(KILL_SWITCH_PATH)
    except Exception:
        return True
    return payload.get("active") is not False


def can_auto_resume(report: dict[str, Any], positions: dict[str, Any]) -> bool:
    """Return true only when the responder can prove a safe resume."""
    return (
        _is_clean_report(report)
        and positions.get("known") is True
        and not positions.get("positions")
    )


def _restart_cooldown_active(state: dict[str, Any], current: datetime) -> bool:
    value = state.get("lastActionAt")
    if not value:
        return False
    try:
        age = (current - _parse_timestamp(value)).total_seconds()
    except (TypeError, ValueError):
        return True
    return age < RESTART_COOLDOWN_SECONDS


def select_action(
    report: dict[str, Any],
    positions: dict[str, Any],
    state: dict[str, Any],
    *,
    current: datetime | None = None,
) -> str | None:
    """Choose at most one bounded operational action for this run."""
    if _is_clean_report(report):
        return None
    if state.get("state_invalid") is True:
        return None
    if positions.get("known") is not True or positions.get("positions"):
        return None
    # A restart that did not clear the incident is a bounded attempt, not a
    # license to cycle the daemon forever.  The clean-health path above can
    # still confirm and resume; otherwise leave the kill switch in place for
    # manual review.
    if state.get("lastAction") == "restart_daemon" and state.get("lastOutcome") == "blocked":
        return None

    failures = [str(item) for item in report.get("failures", [])]
    roots = {_failure_root(item) for item in failures}
    if "state_reset_stale" in roots and not _has_non_autofix_failure(failures):
        return "preflight_fix"

    if not failures or _has_non_autofix_failure(failures):
        return None
    if not all(_is_restartable_failure(item) for item in failures):
        return None
    if _restart_cooldown_active(state, current or now_et()):
        return None
    return "restart_daemon"


def _load_health_module():
    sys.path.insert(0, str(SCRIPTS_DIR))
    import v7_system_health_check as health

    return health


def _ensure_kill_switch(failures: list[str]) -> None:
    """Fail closed if the health module itself cannot be imported."""
    if KILL_SWITCH_PATH.exists():
        try:
            existing = _read_json(KILL_SWITCH_PATH)
            if existing.get("active") is True:
                return
        except Exception:
            pass
    _atomic_write_json(
        KILL_SWITCH_PATH,
        {
            "active": True,
            "reason": "health_responder:" + ",".join(failures[:12]),
            "timestamp": now_et().isoformat(),
        },
    )


def evaluate_health_once() -> tuple[dict[str, Any], str | None]:
    """Evaluate and record health while preserving fail-closed behavior."""
    try:
        health = _load_health_module()
        report = health.evaluate_health()
        if not isinstance(report, dict):
            raise ValueError("health_report_not_object")
        if report.get("healthy") is not True:
            failures = [str(item) for item in report.get("failures", [])]
            health.pause_trading(failures or ["health_unhealthy"])
        # This is the same report writer used by the gate.  It records the
        # current kill-switch state but never removes it.
        health._record_report(report)
        return report, None
    except Exception as exc:
        failure = f"health_responder_unavailable:{type(exc).__name__}"
        _ensure_kill_switch([failure])
        report = {
            "checkedAt": now_et().isoformat(),
            "healthy": False,
            "failures": [failure],
            "checks": [],
            "runtime": {"liveOrdersEnabled": False},
        }
        return report, str(exc)


def _run_release_sync_if_needed(failures: list[str]) -> dict[str, Any] | None:
    if not any(_is_release_mismatch(item) for item in failures):
        return None
    # A full release check intentionally fails while the daemon still has the
    # previous process-frozen identity.  Validate disk consistency first, then
    # let the bounded zero-position restart repair the runtime identity.
    return _run([sys.executable, str(RELEASE_SYNC_SCRIPT), "static-check"], timeout=90)


def _read_runtime() -> dict[str, Any] | None:
    path = STATE_DIR / "v7_scalp_live_runtime.json"
    try:
        return _read_json(path)
    except Exception:
        return None


def _wait_for_heartbeat(previous: dict[str, Any] | None) -> bool:
    previous_updated = previous.get("updatedAt") if previous else None
    deadline = time.monotonic() + HEARTBEAT_WAIT_SECONDS
    while time.monotonic() < deadline:
        runtime = _read_runtime()
        if runtime:
            updated = runtime.get("updatedAt")
            try:
                age = (now_et() - _parse_timestamp(updated)).total_seconds()
            except (TypeError, ValueError):
                age = HEARTBEAT_WAIT_SECONDS + 1
            if (
                runtime.get("processId", 0)
                and isinstance(runtime.get("mode"), str)
                and runtime.get("mode") != "stopped"
                and runtime.get("consecutiveFailures") == 0
                and age <= 30
                and updated != previous_updated
            ):
                return True
        time.sleep(1)
    return False


def _restart_daemon() -> dict[str, Any]:
    previous = _read_runtime()
    label = f"gui/{os.getuid()}/{DAEMON_LABEL}"
    result = _run(["/bin/launchctl", "kickstart", "-k", label], timeout=20)
    result["method"] = "launchctl_kickstart"
    if result.get("returncode") != 0:
        # If the label is not loaded, use the existing operational bootstrap
        # wrapper.  This is still gated by the zero-position check in main().
        fallback = _run([str(RESTART_SCRIPT)], timeout=90)
        result["fallback"] = {
            "method": "v73_restart_daemon.sh",
            "returncode": fallback.get("returncode"),
            "stderr": fallback.get("stderr", ""),
        }
        if fallback.get("returncode") == 0:
            result["returncode"] = 0
    result["heartbeat_fresh"] = (
        _wait_for_heartbeat(previous) if result.get("returncode") == 0 else False
    )
    return result


def _run_preflight_fix() -> dict[str, Any]:
    return _run([sys.executable, str(PREFLIGHT_SCRIPT), "--fix", "--json"], timeout=120)


def _resume_gate() -> dict[str, Any]:
    return _run([sys.executable, str(HEALTH_SCRIPT), "--resume"], timeout=90)


def _confirm_and_resume() -> tuple[bool, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Require two clean checks and no positions before removing the marker."""
    reports: list[dict[str, Any]] = []
    last_report: dict[str, Any] = {}
    last_positions: dict[str, Any] = {}
    for index in range(CLEAN_CONFIRMATIONS):
        last_report, error = evaluate_health_once()
        last_positions = _positions_snapshot()
        reports.append(
            {
                "healthy": _is_clean_report(last_report),
                "failures": list(last_report.get("failures", [])),
                "positions_known": last_positions.get("known") is True,
                "open_positions": list(last_positions.get("positions", [])),
                "error": error,
            }
        )
        if not can_auto_resume(last_report, last_positions):
            return False, last_report, last_positions, reports
        if index + 1 < CLEAN_CONFIRMATIONS:
            time.sleep(CLEAN_CONFIRMATION_DELAY_SECONDS)

    resume_result = _resume_gate()
    final_report, final_error = evaluate_health_once()
    final_positions = _positions_snapshot()
    reports.append(
        {
            "resume_returncode": resume_result.get("returncode"),
            "healthy": _is_clean_report(final_report),
            "failures": list(final_report.get("failures", [])),
            "positions_known": final_positions.get("known") is True,
            "open_positions": list(final_positions.get("positions", [])),
            "error": final_error,
            "kill_switch_present": KILL_SWITCH_PATH.exists(),
        }
    )
    success = (
        resume_result.get("returncode") == 0
        and not KILL_SWITCH_PATH.exists()
        and can_auto_resume(final_report, final_positions)
    )
    return success, final_report, final_positions, reports


def _summary(report: dict[str, Any], positions: dict[str, Any]) -> dict[str, Any]:
    return {
        "healthy": _is_clean_report(report),
        "failures": list(report.get("failures", [])),
        "positions_known": positions.get("known") is True,
        "open_positions": list(positions.get("positions", [])),
        "kill_switch_present": KILL_SWITCH_PATH.exists(),
    }


def _print_unresolved(report: dict[str, Any], positions: dict[str, Any], actions: list[str]) -> None:
    failures = ", ".join(str(item) for item in report.get("failures", [])) or "unknown"
    action_text = ", ".join(actions) if actions else "no safe automatic action"
    print("V7 PROACTIVE RESPONDER — trading remains paused")
    print(f"FAIL: {failures}")
    print(f"ACTION: {action_text}")
    if positions.get("known") is not True:
        print("SAFETY: could not prove zero open positions")
    elif positions.get("positions"):
        print("SAFETY: open position detected; no restart or resume attempted")


def run_once() -> int:
    current = now_et()
    state = _load_recovery_state()
    actions: list[str] = []

    report, health_error = evaluate_health_once()
    positions = _positions_snapshot()

    if health_error:
        _record_event(
            {
                "checkedAt": current.isoformat(),
                "outcome": "blocked",
                "reason": "health_evaluation_unavailable",
                "summary": _summary(report, positions),
            }
        )
        _print_unresolved(report, positions, actions)
        return 1

    if _is_clean_report(report) and not _kill_switch_active():
        state["lastOutcome"] = "healthy"
        _save_recovery_state(state)
        return 0

    if _is_clean_report(report) and _health_owned_kill_switch():
        if can_auto_resume(report, positions):
            success, final_report, final_positions, confirmations = _confirm_and_resume()
            if success:
                state.update(
                    {
                        "lastOutcome": "recovered",
                        "lastResumeAt": now_et().isoformat(),
                        "lastFailureFingerprint": "healthy_after_pause",
                    }
                )
                _save_recovery_state(state)
                _record_event(
                    {
                        "checkedAt": current.isoformat(),
                        "outcome": "recovered",
                        "trigger": "existing_kill_switch_with_clean_health",
                        "actions": ["two_clean_confirmations", "explicit_health_resume"],
                        "summary": _summary(final_report, final_positions),
                        "confirmations": confirmations,
                    }
                )
                print("V7 proactive recovery: clean twice, no positions, trading resumed")
                return 0
            actions.append("resume withheld after failed confirmation")
            report, positions = final_report, final_positions
        else:
            actions.append("resume withheld: zero open positions not proven")

    if not _is_clean_report(report):
        # Any unhealthy report is already paused by evaluate_health_once().
        action = select_action(report, positions, state, current=current)
        if action == "preflight_fix":
            result = _run_preflight_fix()
            actions.append(f"preflight_fix rc={result.get('returncode')}")
            state.update(
                {
                    "lastActionAt": now_et().isoformat(),
                    "lastAction": "preflight_fix",
                    "lastFailureFingerprint": _failure_fingerprint(list(report.get("failures", []))),
                }
            )
            _save_recovery_state(state)
            report, health_error = evaluate_health_once()
            positions = _positions_snapshot()
        elif action == "restart_daemon":
            release_check = _run_release_sync_if_needed(
                [str(item) for item in report.get("failures", [])]
            )
            if release_check is not None and release_check.get("returncode") != 0:
                actions.append(
                    f"release_static_check_blocked rc={release_check.get('returncode')}"
                )
            else:
                result = _restart_daemon()
                actions.append(
                    f"daemon_restart rc={result.get('returncode')} heartbeat={result.get('heartbeat_fresh')}"
                )
                state.update(
                    {
                        "lastActionAt": now_et().isoformat(),
                        "lastAction": "restart_daemon",
                        "lastFailureFingerprint": _failure_fingerprint(list(report.get("failures", []))),
                    }
                )
                _save_recovery_state(state)
                report, health_error = evaluate_health_once()
                positions = _positions_snapshot()

        if health_error:
            _record_event(
                {
                    "checkedAt": current.isoformat(),
                    "outcome": "blocked",
                    "actions": actions,
                    "summary": _summary(report, positions),
                }
            )
            _print_unresolved(report, positions, actions)
            return 1

    if _is_clean_report(report) and _health_owned_kill_switch() and can_auto_resume(report, positions):
        success, final_report, final_positions, confirmations = _confirm_and_resume()
        if success:
            state.update(
                {
                    "lastOutcome": "recovered",
                    "lastResumeAt": now_et().isoformat(),
                    "lastFailureFingerprint": _failure_fingerprint(list(report.get("failures", []))),
                }
            )
            _save_recovery_state(state)
            _record_event(
                {
                    "checkedAt": current.isoformat(),
                    "outcome": "recovered",
                    "actions": actions + ["two_clean_confirmations", "explicit_health_resume"],
                    "summary": _summary(final_report, final_positions),
                    "confirmations": confirmations,
                }
            )
            print("V7 proactive recovery: operational fault cleared and trading resumed")
            return 0
        actions.append("resume withheld after failed confirmation")
        report, positions = final_report, final_positions

    if _is_clean_report(report) and not _kill_switch_active():
        state["lastOutcome"] = "healthy"
        _save_recovery_state(state)
        return 0

    state["lastOutcome"] = "blocked"
    _save_recovery_state(state)
    _record_event(
        {
            "checkedAt": current.isoformat(),
            "outcome": "blocked",
            "actions": actions,
            "summary": _summary(report, positions),
        }
    )
    _print_unresolved(report, positions, actions)
    return 1


def main() -> int:
    with _single_runner() as acquired:
        if not acquired:
            return 0
        return run_once()


if __name__ == "__main__":
    raise SystemExit(main())
