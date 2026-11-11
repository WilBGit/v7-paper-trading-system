#!/usr/bin/env python3
"""Persistent isolated read-only Robinhood quote worker.

The parent daemon owns strategy state. This child owns the heavier Hermes MCP
runtime and can be terminated during idle modes to return memory to the Mac.
Only quote-read operations are reachable through v7_rh_quote_bridge's allowlist.
"""
from __future__ import annotations

import json
import sys
from typing import Any

import v7_rh_quote_bridge as bridge


def dispatch(request: dict[str, Any]) -> dict[str, Any]:
    operation = request.get("op")
    if operation == "equity":
        return bridge.quote_equity(str(request.get("symbol", "SPY")))
    if operation == "index":
        if request.get("reference_underlying") is None:
            raise bridge.BridgeError("index_operation_requires_reference_underlying")
        return bridge.quote_index(
            str(request.get("symbol", "")),
            reference_underlying=float(request["reference_underlying"]),
            dte=int(request.get("dte", 1)),
        )
    if operation == "scalp_candidate":
        return bridge.quote_scalp_candidate(
            str(request.get("symbol", "SPY")),
            str(request["option_type"]),
            float(request["underlying"]),
            int(request["dte"]),
            float(request["min_debit"]),
            float(request["max_debit"]),
            float(request["max_otm_pct"]),
        )
    if operation == "spread_candidates":
        return bridge.quote_spread_candidates(
            str(request.get("symbol", "SPY")),
            dte=int(request.get("dte", 1)),
            max_strike_distance=float(request.get("max_strike_distance", 5.0)),
        )
    if operation == "spread_position":
        legs = request.get("legs")
        if not isinstance(legs, dict):
            raise bridge.BridgeError("spread_position_requires_legs")
        return bridge.build_spread_position_quotes(legs)
    if operation == "option":
        return bridge.quote_option(
            str(request["occ_symbol"]),
            str(request["instrument_id"]) if request.get("instrument_id") else None,
        )
    raise bridge.BridgeError(f"unsupported_worker_operation:{operation}")


def main() -> int:
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            request = json.loads(raw_line)
            if not isinstance(request, dict):
                raise bridge.BridgeError("worker_request_must_be_object")
            payload = dispatch(request)
            response = {"ok": True, "data": payload}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        print(json.dumps(response, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
