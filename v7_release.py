#!/usr/bin/env python3
"""Single source of truth for the active V7 release identity.

User-facing V7 components import this module instead of embedding a release
number. The JSON manifest is intentionally small, reviewable, and hashed into
the strategy configuration fingerprint so a release change cannot be silently
reported as the previous configuration.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).with_name("v7_release.json")
_ENGINE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+-[a-z0-9][a-z0-9-]*$")
_STRATEGY_ID_RE = re.compile(r"^v\d{4,}-[a-z0-9][a-z0-9-]*$")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def load_release(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    """Load and validate the release manifest, failing closed on drift."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"release_manifest_missing:{path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"release_manifest_invalid_json:{path}:{exc}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("release_manifest_not_object")
    required = {
        "family_name",
        "engine_version",
        "strategy_id",
        "release_date",
        "previous_engine_version",
        "previous_strategy_id",
        "change_type",
        "change_summary",
        "changes",
        "evidence_gate",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise RuntimeError("release_manifest_missing:" + ",".join(missing))

    engine_version = str(payload["engine_version"])
    strategy_id = str(payload["strategy_id"])
    if not _ENGINE_VERSION_RE.fullmatch(engine_version):
        raise RuntimeError(f"release_engine_version_invalid:{engine_version}")
    if not _STRATEGY_ID_RE.fullmatch(strategy_id):
        raise RuntimeError(f"release_strategy_id_invalid:{strategy_id}")
    try:
        date.fromisoformat(str(payload["release_date"]))
    except ValueError as exc:
        raise RuntimeError(f"release_date_invalid:{payload['release_date']}") from exc

    changes = payload["changes"]
    if not isinstance(changes, list) or not changes:
        raise RuntimeError("release_changes_missing")
    orders = []
    for change in changes:
        if not isinstance(change, dict):
            raise RuntimeError("release_change_not_object")
        for key in ("change_order", "change_type", "summary", "details", "files", "verification"):
            if key not in change:
                raise RuntimeError(f"release_change_missing:{key}")
        if not isinstance(change["files"], list) or not isinstance(change["verification"], list):
            raise RuntimeError("release_change_lists_invalid")
        orders.append(int(change["change_order"]))
    if sorted(orders) != list(range(1, len(orders) + 1)):
        raise RuntimeError(f"release_change_order_invalid:{orders}")

    evidence_gate = payload["evidence_gate"]
    if not isinstance(evidence_gate, dict):
        raise RuntimeError("release_evidence_gate_not_object")
    for key in ("min_closed_trades", "min_win_rate_pct", "require_positive_pnl"):
        if key not in evidence_gate:
            raise RuntimeError(f"release_evidence_gate_missing:{key}")
    try:
        min_closed_trades = int(evidence_gate["min_closed_trades"])
        min_win_rate_pct = float(evidence_gate["min_win_rate_pct"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("release_evidence_gate_values_invalid") from exc
    if min_closed_trades <= 0 or not 0 <= min_win_rate_pct <= 100:
        raise RuntimeError("release_evidence_gate_values_out_of_range")
    if not isinstance(evidence_gate["require_positive_pnl"], bool):
        raise RuntimeError("release_evidence_gate_positive_pnl_invalid")

    carry_forward = payload.get("evidence_carry_forward")
    if carry_forward is not None:
        if not isinstance(carry_forward, dict):
            raise RuntimeError("release_evidence_carry_forward_not_object")
        required_carry = {
            "closed_trades",
            "wins",
            "losses_or_breakeven",
            "net_pnl",
            "source_engine_version",
            "source_strategy_id",
            "source_release_manifest_sha256",
        }
        missing_carry = sorted(required_carry - carry_forward.keys())
        if missing_carry:
            raise RuntimeError(
                "release_evidence_carry_forward_missing:" + ",".join(missing_carry)
            )
        try:
            carry_closed = int(carry_forward["closed_trades"])
            carry_wins = int(carry_forward["wins"])
            carry_losses = int(carry_forward["losses_or_breakeven"])
            carry_pnl = float(carry_forward["net_pnl"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("release_evidence_carry_forward_values_invalid") from exc
        if (
            carry_closed < 0
            or carry_wins < 0
            or carry_losses < 0
            or carry_wins + carry_losses != carry_closed
            or not math.isfinite(carry_pnl)
        ):
            raise RuntimeError("release_evidence_carry_forward_values_invalid")
        for key in (
            "source_engine_version",
            "source_strategy_id",
            "source_release_manifest_sha256",
        ):
            if not isinstance(carry_forward[key], str) or not carry_forward[key]:
                raise RuntimeError(f"release_evidence_carry_forward_{key}_invalid")

    return payload


def manifest_sha256(manifest: dict[str, Any] | None = None) -> str:
    """Return the canonical SHA256 of the release manifest."""
    return hashlib.sha256(_canonical_json(RELEASE if manifest is None else manifest)).hexdigest()


RELEASE = load_release()
PROMOTION_GATE_PATH = Path(__file__).with_name("v7_promotion_gate.json")


def load_promotion_gate() -> dict[str, object]:
    """Load the operator promotion policy separately from release identity."""
    try:
        payload = json.loads(PROMOTION_GATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"promotion_gate_unavailable:{exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("promotion_gate_not_object")
    required = {"min_closed_trades", "min_win_rate_pct", "require_positive_pnl"}
    if not required.issubset(payload):
        raise RuntimeError("promotion_gate_missing_fields")
    try:
        min_closed_trades = int(payload["min_closed_trades"])
        min_win_rate_pct = float(payload["min_win_rate_pct"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("promotion_gate_values_invalid") from exc
    if min_closed_trades <= 0 or not 0 <= min_win_rate_pct <= 100:
        raise RuntimeError("promotion_gate_values_out_of_range")
    if not isinstance(payload["require_positive_pnl"], bool):
        raise RuntimeError("promotion_gate_positive_pnl_invalid")
    return {
        "min_closed_trades": min_closed_trades,
        "min_win_rate_pct": min_win_rate_pct,
        "require_positive_pnl": payload["require_positive_pnl"],
    }


PROMOTION_GATE = load_promotion_gate()
ENGINE_VERSION = str(RELEASE["engine_version"])
STRATEGY_ID = str(RELEASE["strategy_id"])
RELEASE_DATE = str(RELEASE["release_date"])
PREVIOUS_ENGINE_VERSION = str(RELEASE["previous_engine_version"])
PREVIOUS_STRATEGY_ID = str(RELEASE.get("previous_strategy_id", ""))
CHANGE_TYPE = str(RELEASE["change_type"])
CHANGE_SUMMARY = str(RELEASE["change_summary"])
# The operator promotion policy is intentionally separate from the release
# manifest metadata. Existing engine consumers use this compatibility name.
EVIDENCE_GATE = PROMOTION_GATE
EVIDENCE_CARRY_FORWARD = dict(RELEASE.get("evidence_carry_forward") or {})
MANIFEST_SHA256 = manifest_sha256(RELEASE)


def display_version() -> str:
    return f"V{ENGINE_VERSION}"


def identity() -> dict[str, str]:
    return {
        "engineVersion": ENGINE_VERSION,
        "strategyId": STRATEGY_ID,
        "releaseDate": RELEASE_DATE,
        "previousEngineVersion": PREVIOUS_ENGINE_VERSION,
        "previousStrategyId": PREVIOUS_STRATEGY_ID,
        "manifestSha256": MANIFEST_SHA256,
    }


def main() -> int:
    print(json.dumps({**identity(), "changeType": CHANGE_TYPE, "changeSummary": CHANGE_SUMMARY, "evidenceGate": EVIDENCE_GATE}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
