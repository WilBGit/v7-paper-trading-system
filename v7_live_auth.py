#!/usr/bin/env python3
"""Shared fail-closed live-trading authorization for V7.4.23.

All order-capable callers use this module. Authorization is not a strategy
switch; it is a separate, explicit operator attestation that must also match
the live release, current source/config fingerprints, exact-release evidence,
and the Agentic account.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

STATE_DIR = Path.home() / ".hermes" / "state"
SCRIPT_DIR = Path(__file__).resolve().parent
AUTHORIZATION_PATH = STATE_DIR / "v7_live_authorization.json"
LIVE_DAEMON_PATH = SCRIPT_DIR / "v7_live_execution_daemon.py"
LIVE_BRIDGE_PATH = SCRIPT_DIR / "v7_live_order_bridge.py"
AGENTIC_ACCOUNT_NUMBER = "801336223"
REQUIRED_FINGERPRINT_FIELDS = (
    "engineVersion",
    "strategyId",
    "releaseManifestSha256",
    "sourceCodeSha256",
    "configSha256",
    "liveDaemonSourceSha256",
    "liveBridgeSourceSha256",
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _current_identity() -> dict[str, str]:
    """Return the active release identity and current code fingerprints."""
    import v7_release
    import v7_scalp_paper as engine

    return {
        "engineVersion": str(v7_release.identity()["engineVersion"]),
        "strategyId": str(v7_release.identity()["strategyId"]),
        "releaseManifestSha256": str(v7_release.identity()["manifestSha256"]),
        "sourceCodeSha256": str(engine.source_code_sha256()),
        "configSha256": str(engine.config_sha256()),
        "liveDaemonSourceSha256": _file_sha256(LIVE_DAEMON_PATH),
        "liveBridgeSourceSha256": _file_sha256(LIVE_BRIDGE_PATH),
    }


LIVE_ACTIVATION_MIN_EXACT_TRADES = 5


def _evidence_gate_met() -> bool:
    """Live activation requires Project Contributor's 25 exact-release bar, not the 5-trade review."""
    try:
        import v7_live_gate_progress as progress
        snapshot = progress.build_snapshot()
        if snapshot.get("gateMet") is not True:
            return False
        if snapshot.get("enableLiveAutomatically") is not False:
            return False
        if snapshot.get("liveOrdersEnabled") is not False:
            return False
        exact_trades = snapshot.get("exactClosedTrades")
        if isinstance(exact_trades, bool) or exact_trades is None:
            return False
        if int(exact_trades) < LIVE_ACTIVATION_MIN_EXACT_TRADES:
            return False
        exact_pnl = snapshot.get("exactNetPnl")
        if isinstance(exact_pnl, bool) or exact_pnl is None:
            return False
        return float(exact_pnl) > 0.0
    except Exception:
        return False


def _matches_current_release(payload: dict[str, Any]) -> bool:
    try:
        identity = _current_identity()
    except Exception:
        return False
    return all(payload.get(field) == value for field, value in identity.items())


def live_authorized() -> bool:
    """Return true only for a fully attested, exact-release live authorization."""
    try:
        payload = json.loads(AUTHORIZATION_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("authorized") is not True:
        return False
    if payload.get("readinessAttested") is not True:
        return False
    if payload.get("accountNumber") != AGENTIC_ACCOUNT_NUMBER:
        return False
    if not all(isinstance(payload.get(field), str) and payload.get(field) for field in REQUIRED_FINGERPRINT_FIELDS):
        return False
    if not _matches_current_release(payload):
        return False
    return _evidence_gate_met()
