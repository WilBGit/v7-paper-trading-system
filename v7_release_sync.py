#!/usr/bin/env python3
"""Validate and record the active V7 release.

Release workflow after editing v7_release.json:
    python3 v7_release_sync.py static-check
    python3 v7_release_sync.py check
    bash v73_restart_daemon.sh
    python3 v7_release_sync.py record

IMPORTANT deploy order: always write v7_release.json BEFORE v7_release.py.
If the validator requires a new field that the on-disk manifest doesn't
have yet, every script that imports v7_release will crash (including this
one, the daemon, and the watchdog).  Write manifest first, then code.

The record command is fail-closed when the daemon is stale or any paper
position is open. It seals the release and imports the append-only ledgers into
the local MySQL archive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".hermes" / "state"
DB_DIR = Path.home() / ".hermes" / "trading-db"
DB_MANAGER = DB_DIR / "db_manager.py"
sys.path.insert(0, str(SCRIPTS_DIR))

import v7_release  # noqa: E402


DYNAMIC_CONSUMERS = {
    "v7_scalp_paper.py": "v7_release.ENGINE_VERSION",
    "v7_scalp_live_daemon.py": "engine.ENGINE_VERSION",
    "v7_preflight_check.py": "engine.ENGINE_VERSION",
    "v7_trade_signal_monitor.py": "engine.ENGINE_VERSION",
    "v7_scalp_photon_alert.py": "v7_scalp_paper.ENGINE_VERSION",
    "v73_morning_premarket_brief.py": "engine.ENGINE_VERSION",
    "v7_scalp_weekly_report.py": "engine.ENGINE_VERSION",
    "v7_live_execution_daemon.py": "v7_release.STRATEGY_ID",
    "v7_live_order_bridge.py": "v7_release.identity()",
}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def current_runtime() -> dict[str, Any]:
    path = STATE_DIR / "v7_scalp_live_runtime.json"
    if not path.exists():
        return {}
    return read_json(path)


def disk_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def open_positions() -> list[str]:
    open_ids: list[str] = []
    for path in sorted(STATE_DIR.glob("v7_scalp_state_*.json")):
        try:
            state = read_json(path)
        except Exception:
            continue
        for pos in state.get("activePositions", []):
            if isinstance(pos, dict):
                open_ids.append(str(pos.get("positionId") or path.stem))
    return open_ids


def static_check() -> tuple[bool, dict[str, Any]]:
    """Validate the on-disk release without requiring a restarted daemon."""
    import v7_scalp_paper as engine

    errors: list[str] = []
    manifest = v7_release.identity()
    if engine.ENGINE_VERSION != v7_release.ENGINE_VERSION:
        errors.append("engine_version_not_manifest")
    if engine.STRATEGY_ID != v7_release.STRATEGY_ID:
        errors.append("strategy_id_not_manifest")
    if engine.RELEASE_MANIFEST_SHA256 != v7_release.MANIFEST_SHA256:
        errors.append("engine_manifest_hash_not_manifest")

    consumer_checks: dict[str, bool] = {}
    for filename, token in DYNAMIC_CONSUMERS.items():
        path = SCRIPTS_DIR / filename
        present = path.exists() and token in path.read_text(encoding="utf-8")
        consumer_checks[filename] = present
        if not present:
            errors.append(f"consumer_not_dynamic:{filename}")

    return not errors, {
        "manifest": manifest,
        "manifestSha256": v7_release.MANIFEST_SHA256,
        "consumerChecks": consumer_checks,
        "errors": errors,
    }


def check() -> tuple[bool, dict[str, Any]]:
    _static_ok, static_details = static_check()
    errors = list(static_details["errors"])
    import v7_scalp_paper as engine

    runtime = current_runtime()
    if not runtime:
        errors.append("runtime_missing")
    else:
        if runtime.get("engineVersion") != v7_release.ENGINE_VERSION:
            errors.append("runtime_engine_version_mismatch")
        if runtime.get("strategyId") != v7_release.STRATEGY_ID:
            errors.append("runtime_strategy_id_mismatch")
        if runtime.get("releaseManifestSha256") != v7_release.MANIFEST_SHA256:
            errors.append("runtime_manifest_hash_mismatch")
        if runtime.get("engineSourceSha256") != disk_hash(SCRIPTS_DIR / "v7_scalp_paper.py"):
            errors.append("runtime_engine_source_hash_mismatch")
        if runtime.get("daemonSourceSha256") != disk_hash(SCRIPTS_DIR / "v7_scalp_live_daemon.py"):
            errors.append("runtime_daemon_source_hash_mismatch")
        if runtime.get("configSha256") != engine.config_sha256():
            errors.append("runtime_config_hash_mismatch")
        if runtime.get("consecutiveFailures", 0) != 0:
            errors.append("runtime_consecutive_failures")
        if runtime.get("liveOrdersEnabled") is not False:
            errors.append("live_orders_not_false")

    positions = open_positions()
    return not errors, {
        **static_details,
        "runtime": {
            key: runtime.get(key)
            for key in (
                "engineVersion", "strategyId", "releaseManifestSha256",
                "engineSourceSha256", "daemonSourceSha256", "configSha256",
                "updatedAt", "mode", "consecutiveFailures", "liveOrdersEnabled",
            )
        },
        "openPositions": positions,
        "errors": errors,
    }


def run_command(command: list[str]) -> None:
    print(">>>", " ".join(command))
    result = subprocess.run(command, cwd=DB_DIR if command[1:] and command[1].endswith("db_manager.py") else None)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate or record the active V7 release")
    parser.add_argument("command", choices=("check", "static-check", "record"))
    args = parser.parse_args()

    if args.command == "static-check":
        ok, details = static_check()
        print(json.dumps(details, indent=2, sort_keys=True))
        if not ok:
            print("STATIC RELEASE CHECK FAILED", file=sys.stderr)
            return 2
        print("STATIC RELEASE CHECK PASSED")
        return 0

    ok, details = check()
    print(json.dumps(details, indent=2, sort_keys=True))
    if not ok:
        print("RELEASE CHECK FAILED", file=sys.stderr)
        return 2
    if args.command == "check":
        print("RELEASE CHECK PASSED")
        return 0

    if details["openPositions"]:
        print("RECORD BLOCKED: open paper positions exist", file=sys.stderr)
        return 2
    if not DB_MANAGER.exists():
        print(f"RECORD BLOCKED: missing {DB_MANAGER}", file=sys.stderr)
        return 2

    run_command([sys.executable, str(DB_MANAGER), "setup"])
    run_command([sys.executable, str(DB_MANAGER), "seal"])
    run_command([sys.executable, str(DB_MANAGER), "import"])
    print(f"RELEASE RECORDED: {v7_release.ENGINE_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
