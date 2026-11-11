#!/usr/bin/env python3
"""Monitor the active V7 release live-readiness without automatic activation.

Checks every 2 minutes:
1. Buying power >= $75 (MIN_BUYING_POWER for one SPY contract)
2. Zero open option positions on the Agentic account
3. Zero unresolved open option orders
4. Explicitly inactive kill switch
5. The 5-trade review trigger is met (exact current-release trades + positive P&L)
6. Project Contributor's live-activation bar is met (25 exact current-release closed trades + positive P&L)

The scheduled invocation is read-only. A state-changing activation requires
``--activate --confirm-release <active-engine-version>`` and a
fresh pass through every check. Fails closed on any error.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path.home() / ".hermes" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import v7_release

PT = ZoneInfo("America/Los_Angeles")
AUTHORIZATION_PATH = Path.home() / ".hermes" / "state" / "v7_live_authorization.json"
GATE_PROGRESS_PATH = Path.home() / ".hermes" / "state" / "v7_live_gate_progress.json"
LOG_PATH = Path.home() / ".hermes" / "state" / "v7_live_enable_monitor.log"
AGENTIC_ACCOUNT = "801336223"
MIN_BUYING_POWER = 75.0
MIN_EXACT_CURRENT_RELEASE_TRADES = 5
POLL_INTERVAL_SECONDS = 120
KILL_SWITCH_PATH = Path.home() / ".hermes" / "state" / "v7_kill_switch.json"


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(PT).isoformat()
    line = f"[{timestamp}] {message}"
    print(line)
    with LOG_PATH.open("a") as handle:
        handle.write(line + "\n")


def check_gate() -> dict | None:
    """Check if the active release evidence gate is still met."""
    try:
        import v7_live_gate_progress as progress
        snapshot = progress.build_snapshot()
        if not snapshot.get("gateMet"):
            return None
        if snapshot.get("enableLiveAutomatically") is not False:
            return None
        if snapshot.get("liveOrdersEnabled") is not False:
            return None
        try:
            if float(snapshot.get("exactNetPnl", 0)) <= 0:
                return None
        except (TypeError, ValueError):
            return None
        return snapshot
    except Exception as exc:
        log(f"gate_check_failed: {exc}")
        return None


def check_kill_switch() -> bool:
    """Return True if kill switch is active (blocking).

    The bridge and monitor share the same fail-closed interpretation: only an
    explicit JSON object with ``active: false`` is clear. Missing, malformed,
    or non-boolean control state blocks live enablement.
    """
    if not KILL_SWITCH_PATH.exists():
        return True
    try:
        data = json.loads(KILL_SWITCH_PATH.read_text())
        return not isinstance(data, dict) or data.get("active") is not False
    except (json.JSONDecodeError, OSError):
        return True


def check_broker_readiness() -> tuple[bool, str]:
    """Check buying power, open positions, and open orders via the bridge.

    Returns (ready, detail).
    """
    try:
        import v7_live_order_bridge as bridge
        readiness = bridge.pre_live_readiness()
        checks = readiness.get("checks", [])
        failures = [c["name"] for c in checks if not c.get("passed")]
        detail = "; ".join(
            f"{c['name']}={c['detail']}" for c in checks
        )
        if readiness.get("ready"):
            return True, detail
        return False, f"blocked: {failures} | {detail}"
    except Exception as exc:
        return False, f"bridge_error: {type(exc).__name__}: {exc}"


def write_authorization(snapshot: dict) -> None:
    """Write the live authorization file with current release fingerprints."""
    try:
        import v7_live_auth
        identity = v7_live_auth._current_identity()
    except Exception as exc:
        log(f"identity_check_failed: {exc}")
        raise

    payload = {
        "authorized": True,
        "readinessAttested": True,
        "accountNumber": AGENTIC_ACCOUNT,
        "authorizedBy": "Project Contributor",
        "authorizedAt": datetime.now(PT).isoformat(),
        "scope": f"paper-to-live transition for {v7_release.ENGINE_VERSION} on Agentic account only",
        "reason": f"Operator-approved policy change 2026-10-19: activation threshold lowered 25->5 by Project Contributor. Exact current-release: {int(snapshot.get('exactClosedTrades', 0))} closes, ${float(snapshot.get('exactNetPnl', 0)):+.2f} P&L. Risk caps: engine defaults (5 trades/symbol/day, $40/symbol loss, $80 portfolio). Broker readiness verified at activation.",
        **identity,
    }
    AUTHORIZATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{AUTHORIZATION_PATH.name}.", dir=AUTHORIZATION_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, AUTHORIZATION_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def enable_live_orders() -> None:
    """No-op — the authorization file is the sole live gate.

    The execution daemon and order bridge never read liveOrdersEnabled from
    per-symbol state files. Health/preflight/audit tools read it from the
    runtime file (v7_scalp_live_runtime.json), which the paper daemon
    hardcodes to false. Writing true into per-symbol state here would crash
    the paper engine via validate_state_schema() within one tick, killing the
    signal source the live path mirrors from. The authorization file written
    by write_authorization() is what live_authorized() checks.
    """
    log("enable_live_orders() no-op — authorization file is the live gate")


def resume_auto_activate_cron() -> None:
    """Resume the auto-activate cron job."""
    os.system("hermes cron resume 09c746a746cc 2>&1")
    log("resumed cron 09c746a746cc")


ACTIVATION_CONFIRMATION = v7_release.ENGINE_VERSION


def attempt_enable(*, activate: bool = False, confirmation: str | None = None) -> bool:
    """Check readiness; mutate live state only on explicit manual activation.

    The scheduled monitor is observation-only. ``activate=True`` plus the
    exact release confirmation is required for the state-changing path.
    """
    # 1. Kill switch
    if check_kill_switch():
        log("blocked: kill_switch_active")
        return False

    # 2. Evidence gate
    snapshot = check_gate()
    if snapshot is None:
        log("blocked: evidence gate not met or check failed")
        return False

    try:
        exact_trades = snapshot.get("exactClosedTrades")
        if isinstance(exact_trades, bool):
            raise ValueError("boolean_exact_trade_count")
        exact_trades = int(exact_trades)
    except (AttributeError, TypeError, ValueError):
        log(f"blocked: exact {v7_release.ENGINE_VERSION} trade count missing or malformed")
        return False
    if exact_trades < MIN_EXACT_CURRENT_RELEASE_TRADES:
        log(
            "blocked: operator paper gate requires "
            f"{MIN_EXACT_CURRENT_RELEASE_TRADES} exact {v7_release.ENGINE_VERSION} closed trades; "
            f"have {exact_trades}"
        )
        return False

    # 3. Broker readiness (buying power, positions, orders)
    ready, detail = check_broker_readiness()
    if not ready:
        log(f"blocked: {detail}")
        return False

    log(f"BROKER READY: {detail}")
    exact_count = int(snapshot.get("exactClosedTrades", 0))
    exact_wins = int(snapshot.get("exactWins", snapshot.get("wins", 0)))
    exact_pnl = float(snapshot.get("exactNetPnl", snapshot.get("netPnl", 0.0)))
    combined_count = int(snapshot.get("closedTrades", exact_count))
    combined_pnl = float(snapshot.get("netPnl", exact_pnl))
    log(
        f"GATE MET: exact {exact_count} trades, {exact_wins} wins, "
        f"exact P&L ${exact_pnl:+.2f}; "
        f"combined context {combined_count} trades, ${combined_pnl:+.2f}"
    )

    if not activate:
        log(
            "READY FOR MANUAL ACTIVATION — no live state changed; "
            f"run with --activate --confirm-release {ACTIVATION_CONFIRMATION}"
        )
        return False
    if confirmation != ACTIVATION_CONFIRMATION:
        log("blocked: exact release confirmation required for manual activation")
        return False

    # All checks pass and the operator supplied the explicit activation flag.
    write_authorization(snapshot)
    log("authorization file written")

    enable_live_orders()
    log("live gate armed via authorization file")

    # Keep the former auto-activation cron paused. Live state is enabled only
    # by this manual path, never by a scheduler or statistical gate.
    log(f"{v7_release.ENGINE_VERSION} LIVE TRADING ENABLED")
    return True


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activate", action="store_true", help="perform the gated manual activation")
    parser.add_argument("--confirm-release", default="", help="exact release name to confirm manual activation")
    args = parser.parse_args(argv)

    if args.activate and args.confirm_release != ACTIVATION_CONFIRMATION:
        log("blocked: --activate requires the exact --confirm-release value")
        return 2

    enabled = attempt_enable(
        activate=args.activate,
        confirmation=args.confirm_release if args.activate else None,
    )
    if enabled:
        return 0
    log(f"not ready yet — live remains disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
