#!/usr/bin/env python3
"""Live order bridge for the active V7 release — MCP dispatch with allowlist + idempotency.

Extends the read-only quote bridge pattern to include order-capable Robinhood
MCP methods. Every call is logged to v7_live_order_audit.jsonl for audit trail.

Authorization is enforced at two levels:
1. mcp_call() checks live_authorized() for place_option_order only
2. submit_entry_order/submit_exit_order check live_authorized() before any dispatch

Kill switch (v7_kill_switch.json) blocks new entries; protective closes
remain available during authorization or kill-switch failures.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import io
import json
import math
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERMES_ROOT = Path.home() / ".hermes" / "hermes-agent"
MCP_PREFIX = "mcp__robinhood_trading__"
AGENTIC_ACCOUNT_NUMBER = "801336223"
MIN_BUYING_POWER = 75.0
OPEN_ORDER_STATES = frozenset({
    "queued", "confirmed", "pending", "pending_new", "unconfirmed",
    "submitted", "new", "created", "partially_filled", "pending_cancel",
})
TERMINAL_ORDER_STATES = frozenset({
    "filled", "cancelled", "canceled", "pending_cancelled", "failed",
    "voided", "rejected",
})

KILL_SWITCH_PATH = Path.home() / ".hermes" / "state" / "v7_kill_switch.json"
LIVE_STATE_PATH = Path.home() / ".hermes" / "state" / "v7_live_order_state.json"
AUDIT_PATH = Path.home() / ".hermes" / "state" / "v7_live_order_audit.jsonl"

# Normal placement requires authorization and an inactive kill switch.
# Protective closes are the deliberate exception: they must remain available
# when authorization lapses or the kill switch is active.
ORDER_PLACEMENT_METHODS = frozenset({"place_option_order"})

ALLOWED_METHODS = frozenset({
    "review_option_order",
    "place_option_order",
    "cancel_option_order",
    "get_option_orders",
    "get_option_positions",
    "get_option_quotes",
    "get_accounts",
    "get_portfolio",
    "get_option_chains",
    "get_option_instruments",
})

_MCP_DISCOVERED = False


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------

class OrderBridgeError(RuntimeError):
    """Raised when MCP order dispatch fails."""


class OrderRejected(OrderBridgeError):
    """Raised when an order is rejected by the broker."""


class OrderTimeout(OrderBridgeError):
    """Raised when an order is not filled within the timeout period."""


class OrderLifecycleHalted(OrderBridgeError):
    """Raised when partial, unknown, or unverifiable status needs reconciliation."""


class ReviewRequired(OrderBridgeError):
    """Raised when placement lacks a valid review proof."""


class _ReviewProof:
    __slots__ = ("intent_digest", "protective_exit", "used")

    def __init__(self, intent_digest: str, protective_exit: bool) -> None:
        self.intent_digest = intent_digest
        self.protective_exit = protective_exit
        self.used = False


class KillSwitchActive(OrderBridgeError):
    """Raised when the kill switch is active and blocks order submission."""


class LiveAuthorizationRequired(OrderBridgeError):
    """Raised when live authorization is not granted."""


# ---------------------------------------------------------------------------
# Authorization — delegates to v7_live_auth, patchable for tests
# ---------------------------------------------------------------------------

def live_authorized() -> bool:
    """Check live trading authorization via v7_live_auth."""
    try:
        import v7_live_auth
        return v7_live_auth.live_authorized()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------

def kill_switch_active() -> bool:
    """Return true when the kill switch is active, missing, or malformed.

    New entries fail closed. Protective exits explicitly bypass this check.
    """
    try:
        payload = json.loads(KILL_SWITCH_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return True
        return payload.get("active") is not False
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return True


def _is_protective_close(arguments: dict[str, Any]) -> bool:
    """Accept only sell-to-close legs for the protective auth bypass."""
    legs = arguments.get("legs")
    try:
        quantity = int(arguments.get("quantity", 0))
    except (TypeError, ValueError):
        quantity = 0
    return (
        arguments.get("account_number") == AGENTIC_ACCOUNT_NUMBER
        and quantity > 0
        and bool(legs)
        and all(
        isinstance(leg, dict)
        and leg.get("side") == "sell"
        and leg.get("position_effect") == "close"
        for leg in legs
        )
    )


# ---------------------------------------------------------------------------
# Live state persistence (idempotency tracking)
# ---------------------------------------------------------------------------

def load_live_state() -> dict[str, Any]:
    """Load v7_live_order_state.json. Returns default on missing, raises on corrupt."""
    try:
        return json.loads(LIVE_STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"orders": {}, "positions": {}}
    except json.JSONDecodeError as exc:
        raise OrderBridgeError(f"corrupt_live_state:{exc}") from exc


def _save_live_state(state: dict[str, Any]) -> None:
    """Atomically save live order state."""
    import tempfile
    LIVE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{LIVE_STATE_PATH.name}.", dir=LIVE_STATE_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, LIVE_STATE_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


# ---------------------------------------------------------------------------
# Audit logging (best-effort, never blocks order flow)
# ---------------------------------------------------------------------------

def _audit_log(method: str, arguments: dict[str, Any], result: dict[str, Any] | None, error: str | None) -> None:
    try:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "method": method,
            "arguments": arguments,
            "success": error is None,
        }
        if error:
            entry["error"] = error[:300]
        if result and isinstance(result, dict):
            entry["resultKeys"] = {
                k: result.get(k)
                for k in ("id", "order_id", "state", "status", "filled_quantity",
                          "average_price", "account_number")
                if k in result
            }
        with AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# MCP dispatch internals
# ---------------------------------------------------------------------------

def _run_discovery(discover):
    result = discover()
    return asyncio.run(result) if inspect.isawaitable(result) else result


def _release_dispatch_context() -> tuple[str, str]:
    """Return the current manifest-owned MCP task context.

    This keeps future order dispatch separate from every historical release's
    idempotency and audit context without rewriting prior broker records.
    """
    import v7_release

    identity = v7_release.identity()
    strategy_id = str(identity.get("strategyId") or "")
    engine_version = str(identity.get("engineVersion") or "")
    if not strategy_id or not engine_version:
        raise OrderBridgeError("release_identity_missing")
    return (
        f"{strategy_id}-live-order-execution",
        f"{engine_version} live option order execution",
    )


def _direct_dispatch(tool_name: str, arguments: dict[str, Any]) -> Any:
    """Dispatch through Hermes' authenticated MCP registry with no model call."""
    global _MCP_DISCOVERED
    root = str(HERMES_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from model_tools import handle_function_call
        from tools.mcp_tool_discovery import discover_mcp_tools
    except Exception as exc:
        raise OrderBridgeError(f"hermes_mcp_import_failed:{type(exc).__name__}") from exc

    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            if not _MCP_DISCOVERED:
                discovered = _run_discovery(discover_mcp_tools)
                if tool_name not in set(discovered or []):
                    raise OrderBridgeError(f"required_mcp_tool_not_discovered:{tool_name}")
                _MCP_DISCOVERED = True
            task_id, user_task = _release_dispatch_context()
            return handle_function_call(
                tool_name,
                arguments,
                task_id=task_id,
                user_task=user_task,
            )
    except OrderBridgeError:
        raise
    except Exception as exc:
        detail = captured.getvalue()[-500:].replace("\n", " ")
        raise OrderBridgeError(
            f"mcp_dispatch_failed:{type(exc).__name__}:{exc!s}:{detail}"
        ) from exc


def _normalize_result(raw: Any) -> dict[str, Any]:
    """Parse MCP dispatch result into a dict, raising on errors."""
    if raw is None:
        raise OrderBridgeError("mcp_dispatch_returned_none")
    try:
        outer = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise OrderBridgeError("mcp_dispatch_invalid_json") from exc
    if not isinstance(outer, dict):
        raise OrderBridgeError("mcp_dispatch_not_object")
    if outer.get("error"):
        raise OrderBridgeError(f"mcp_dispatch_error:{outer['error']}")
    structured = outer.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    result = outer.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError as exc:
            raise OrderBridgeError("mcp_result_invalid_json") from exc
    if isinstance(result, dict):
        if result.get("error"):
            raise OrderBridgeError(f"mcp_result_error:{result['error']}")
        return result
    if "data" in outer:
        return outer
    raise OrderBridgeError("mcp_dispatch_missing_structured_result")


# ---------------------------------------------------------------------------
# Public MCP call with authorization
# ---------------------------------------------------------------------------

def _order_intent_digest(arguments: dict[str, Any]) -> str:
    """Canonicalize fields that review and placement must share exactly."""
    intent = {
        "account_number": arguments.get("account_number"),
        "legs": arguments.get("legs"),
        "quantity": arguments.get("quantity"),
        "type": arguments.get("type"),
        "price": arguments.get("price"),
        "time_in_force": arguments.get("time_in_force"),
    }
    try:
        return json.dumps(intent, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ReviewRequired("order_intent_not_serializable") from exc


def _validated_review_proof(
    review_args: dict[str, Any],
    review_result: dict[str, Any],
    *,
    protective_exit: bool = False,
) -> _ReviewProof:
    """Validate review response shape and return a one-use placement proof."""
    data = review_result.get("data")
    if not isinstance(data, dict):
        raise ReviewRequired("review_response_missing_data")
    order_checks = data.get("order_checks")
    if not isinstance(order_checks, list):
        raise ReviewRequired("review_response_missing_order_checks")
    allowed_states = {"ok", "passed", "clear", "warning", "informational"}
    for check in order_checks:
        if not isinstance(check, dict):
            raise ReviewRequired("review_response_invalid_order_check")
        state = check.get("state")
        if not isinstance(state, str):
            raise ReviewRequired("review_response_order_check_missing_state")
        normalized = state.lower()
        if normalized in {"blocked", "rejected", "failed", "error"}:
            raise OrderRejected(f"review_blocked:{check.get('detail', '')}")
        if normalized not in allowed_states:
            raise ReviewRequired(f"review_response_unknown_state:{state}")
    return _ReviewProof(_order_intent_digest(review_args), protective_exit)


def mcp_call(
    method: str,
    arguments: dict[str, Any],
    dispatcher=None,
    *,
    protective_exit: bool = False,
    review_proof: _ReviewProof | None = None,
) -> dict[str, Any]:
    """Invoke one allowlisted Robinhood MCP method.

    Normal place_option_order calls require authorization and an inactive kill
    switch. A protective exit may bypass those two gates, but only when every
    leg is explicitly sell-to-close.
    """
    if method not in ALLOWED_METHODS:
        raise OrderBridgeError(f"method_not_allowlisted:{method}")
    if not isinstance(arguments, dict):
        raise OrderBridgeError("mcp_arguments_not_object")
    account_bound_methods = {
        "review_option_order",
        "cancel_option_order",
        "get_option_orders",
        "get_option_positions",
        "get_portfolio",
    }
    if method in account_bound_methods and arguments.get("account_number") != AGENTIC_ACCOUNT_NUMBER:
        raise OrderBridgeError("account_binding_mismatch")
    if method in ORDER_PLACEMENT_METHODS:
        if protective_exit:
            if arguments.get("account_number") != AGENTIC_ACCOUNT_NUMBER:
                raise OrderBridgeError("account_binding_mismatch")
            if not _is_protective_close(arguments):
                raise OrderBridgeError("protective_exit_requires_sell_to_close")
        else:
            if kill_switch_active():
                raise KillSwitchActive("kill_switch_active")
            if not live_authorized():
                raise LiveAuthorizationRequired("live_not_authorized")
            if arguments.get("account_number") != AGENTIC_ACCOUNT_NUMBER:
                raise OrderBridgeError("account_binding_mismatch")
        if not isinstance(review_proof, _ReviewProof):
            raise ReviewRequired("placement_requires_review_proof")
        if review_proof.used:
            raise ReviewRequired("review_proof_already_used")
        if review_proof.protective_exit != protective_exit:
            raise ReviewRequired("review_proof_protective_scope_mismatch")
        if review_proof.intent_digest != _order_intent_digest(arguments):
            raise ReviewRequired("review_proof_order_intent_mismatch")
        review_proof.used = True
    if dispatcher is None:
        dispatcher = _direct_dispatch
    try:
        raw = dispatcher(MCP_PREFIX + method, arguments)
        result = _normalize_result(raw)
        _audit_log(method, arguments, result, None)
        return result
    except OrderBridgeError:
        raise
    except Exception as exc:
        _audit_log(method, arguments, None, str(exc))
        raise OrderBridgeError(f"mcp_dispatch_failed:{type(exc).__name__}:{exc!s}") from exc


def get_option_quote_by_instrument(
    instrument_id: str,
    occ_symbol: str,
    *,
    dispatcher=None,
) -> dict[str, Any]:
    """Fetch and normalize one exact option quote for live monitoring.

    This is read-only. It deliberately uses the stored instrument ID rather
    than rebuilding a chain, then verifies the broker returned that same ID
    and a non-crossed bid/ask book for the stored OCC contract.
    """
    instrument_id = str(instrument_id).strip()
    occ_symbol = str(occ_symbol).strip().upper()
    if not instrument_id:
        raise OrderBridgeError("missing_option_instrument_id")
    parsed = parse_occ(occ_symbol)
    result = mcp_call(
        "get_option_quotes",
        {"instrument_ids": [instrument_id]},
        dispatcher=dispatcher,
    )
    results = result.get("data", {}).get("results", [])
    if len(results) != 1 or not isinstance(results[0], dict):
        raise OrderBridgeError("live_option_quote_result_count_mismatch")
    raw = results[0].get("quote", {})
    if str(raw.get("instrument_id", "")) != instrument_id:
        raise OrderBridgeError("live_option_quote_instrument_mismatch")
    try:
        bid = float(raw.get("bid_price"))
        ask = float(raw.get("ask_price"))
        mark = float(raw.get("mark_price"))
    except (TypeError, ValueError) as exc:
        raise OrderBridgeError("live_option_quote_invalid_price") from exc
    timestamp = raw.get("updated_at")
    if not isinstance(timestamp, str) or not timestamp:
        raise OrderBridgeError("live_option_quote_missing_timestamp")
    if not all(math.isfinite(value) for value in (bid, ask, mark)):
        raise OrderBridgeError("live_option_quote_non_finite_price")
    if bid < 0 or ask <= 0 or bid > ask:
        raise OrderBridgeError("live_option_quote_crossed_or_invalid")
    return {
        "kind": "option_quote",
        "source": "Robinhood MCP",
        "timestamp": timestamp,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "symbol": parsed["symbol"],
        "occ_symbol": occ_symbol,
        "instrument_id": instrument_id,
        "option_type": parsed["option_type"],
        "strike": float(parsed["strike"]),
        "expiry": parsed["expiry"],
        "bid": bid,
        "ask": ask,
        "mark": mark,
        "updated_at": timestamp,
        "quote_source": "Robinhood MCP",
    }


# ---------------------------------------------------------------------------
# OCC symbol parsing
# ---------------------------------------------------------------------------

def parse_occ(occ_symbol: str) -> dict[str, str]:
    """Parse an OCC option symbol into its components.

    Example: "SPY260904C00772000" ->
      {"symbol": "SPY", "expiry": "2026-09-04", "option_type": "call", "strike": "772.00"}
    """
    if not isinstance(occ_symbol, str) or len(occ_symbol) < 15:
        raise OrderBridgeError(f"invalid_occ_symbol:{occ_symbol}")
    for split_pos in range(1, len(occ_symbol) - 12):
        date_part = occ_symbol[split_pos:split_pos + 6]
        if date_part.isdigit() and date_part[0:2] in ("20", "21", "25", "26", "27", "28"):
            cp = occ_symbol[split_pos + 6]
            if cp in ("C", "P"):
                root = occ_symbol[:split_pos]
                expiry_raw = date_part
                option_type = "call" if cp == "C" else "put"
                strike_raw = occ_symbol[split_pos + 7:]
                if not strike_raw.isdigit():
                    continue
                strike = f"{int(strike_raw) / 1000:.2f}"
                year = int(expiry_raw[0:2]) + 2000
                month = int(expiry_raw[2:4])
                day = int(expiry_raw[4:6])
                expiry = f"{year:04d}-{month:02d}-{day:02d}"
                return {
                    "symbol": root,
                    "expiry": expiry,
                    "option_type": option_type,
                    "strike": strike,
                }
    raise OrderBridgeError(f"occ_parse_failed:{occ_symbol}")


# ---------------------------------------------------------------------------
# Position validation helpers
# ---------------------------------------------------------------------------

def _check_occ_match(position: dict[str, Any]) -> None:
    """Verify OCC symbol root matches position symbol.

    For full OCC symbols (15+ chars), parse and compare the root.
    For bare tickers (test fixtures, legacy positions), compare directly.
    """
    occ_symbol = position.get("occSymbol", "")
    symbol = position.get("symbol", "")
    if not occ_symbol or not symbol:
        return
    if len(occ_symbol) >= 15:
        try:
            root = parse_occ(occ_symbol)["symbol"]
        except OrderBridgeError:
            raise OrderBridgeError(f"occ_parse_failed:{occ_symbol}")
    else:
        root = occ_symbol
    if root != symbol:
        raise OrderBridgeError(f"occ_symbol_ticker_mismatch:{root}!={symbol}")


def _extract_placement_order_id(
    result: dict[str, Any],
    *,
    expected_ref_id: str | None = None,
) -> str:
    """Extract and verify a broker order ID from a placement response."""
    if not isinstance(result, dict):
        raise OrderLifecycleHalted("placement_receipt_not_object")
    data = result.get("data")
    order: dict[str, Any] | None = None
    if isinstance(data, dict):
        orders = data.get("orders")
        if isinstance(orders, list) and orders and isinstance(orders[0], dict):
            order = orders[0]
        elif isinstance(data.get("order"), dict):
            order = data["order"]
    if order is None:
        orders = result.get("orders")
        if isinstance(orders, list) and orders and isinstance(orders[0], dict):
            order = orders[0]
        elif isinstance(result.get("order"), dict):
            order = result["order"]
        elif any(key in result for key in ("id", "order_id", "orderId")):
            order = result
    if order is None:
        raise OrderLifecycleHalted("placement_receipt_missing_order")

    order_id = next(
        (order.get(key) for key in ("id", "order_id", "orderId") if order.get(key) is not None),
        None,
    )
    if not isinstance(order_id, str) or not order_id.strip():
        raise OrderLifecycleHalted("placement_receipt_missing_order_id")
    for key in ("ref_id", "refId", "client_order_id", "clientOrderId"):
        if key in order and order[key] not in (None, "") and expected_ref_id is not None:
            if str(order[key]) != str(expected_ref_id):
                raise OrderLifecycleHalted(
                    f"placement_ref_id_mismatch:expected={expected_ref_id}:returned={order[key]}"
                )
    if "account_number" in order and order["account_number"] != AGENTIC_ACCOUNT_NUMBER:
        raise OrderLifecycleHalted("placement_account_mismatch")
    return order_id.strip()


# ---------------------------------------------------------------------------
# Entry order submission: review -> place
# ---------------------------------------------------------------------------

def submit_entry_order(position: dict[str, Any], dispatcher=None, ref_id: str | None = None) -> dict[str, Any]:
    """Review then place a buy-to-open entry order.

    If ref_id is provided, it is used as the idempotency key (for daemon
    restart-safe deduplication). If omitted, a random UUID4 is generated.

    Returns {"orderId", "side", "positionEffect", "limitPrice", "refId"}.
    Raises KillSwitchActive, LiveAuthorizationRequired, OrderRejected, OrderBridgeError.
    """
    if kill_switch_active():
        raise KillSwitchActive("kill_switch_active")
    if not live_authorized():
        raise LiveAuthorizationRequired("live_not_authorized")

    instrument_id = position.get("instrumentId")
    if not instrument_id:
        raise OrderBridgeError("missing_instrument_id")
    entry_ask = float(position.get("entryAsk", 0))
    if entry_ask <= 0:
        raise OrderBridgeError("invalid_entry_ask")
    _check_occ_match(position)

    symbol = position.get("symbol", "")

    # Review (mandatory before place, fail-closed)
    review_args: dict[str, Any] = {
        "account_number": AGENTIC_ACCOUNT_NUMBER,
        "legs": [{"option_id": instrument_id, "side": "buy", "position_effect": "open"}],
        "quantity": "1",
        "type": "limit",
        "price": str(entry_ask),
        "time_in_force": "gfd",
        "chain_symbol": symbol,
        "underlying_type": "index" if symbol == "XSP" else "equity",
    }
    review_result = mcp_call("review_option_order", review_args, dispatcher=dispatcher)
    review_proof = _validated_review_proof(review_args, review_result)

    # Place with idempotency key (no chain_symbol/underlying_type in place)
    # Use caller-provided ref_id for restart-safe deduplication; fall back to UUID4
    ref_id = ref_id or str(uuid.uuid4())
    place_args: dict[str, Any] = {
        "account_number": AGENTIC_ACCOUNT_NUMBER,
        "legs": [{"option_id": instrument_id, "side": "buy", "position_effect": "open"}],
        "quantity": "1",
        "type": "limit",
        "price": str(entry_ask),
        "time_in_force": "gfd",
        "ref_id": ref_id,
    }
    place_result = mcp_call(
        "place_option_order",
        place_args,
        dispatcher=dispatcher,
        review_proof=review_proof,
    )

    order_id = _extract_placement_order_id(
        place_result,
        expected_ref_id=ref_id,
    )

    return {
        "orderId": order_id,
        "side": "buy",
        "positionEffect": "open",
        "limitPrice": entry_ask,
        "refId": ref_id,
    }


# ---------------------------------------------------------------------------
# Exit order submission: review -> place
# ---------------------------------------------------------------------------

def submit_exit_order(
    position: dict[str, Any],
    exit_bid: float,
    dispatcher=None,
    ref_id: str | None = None,
    *,
    protective: bool = False,
) -> dict[str, Any]:
    """Review then place a sell-to-close exit order.

    ``protective=True`` keeps an existing broker position closable when the
    kill switch is active or authorization has lapsed. It still requires a
    sell-to-close leg and the normal broker review.
    """
    if not protective:
        if kill_switch_active():
            raise KillSwitchActive("kill_switch_active")
        if not live_authorized():
            raise LiveAuthorizationRequired("live_not_authorized")

    instrument_id = position.get("instrumentId")
    if not instrument_id:
        raise OrderBridgeError("missing_instrument_id")
    if exit_bid <= 0:
        raise OrderBridgeError("invalid_exit_bid")
    _check_occ_match(position)

    symbol = position.get("symbol", "")

    # Review (mandatory before place, fail-closed)
    review_args: dict[str, Any] = {
        "account_number": AGENTIC_ACCOUNT_NUMBER,
        "legs": [{"option_id": instrument_id, "side": "sell", "position_effect": "close"}],
        "quantity": "1",
        "type": "limit",
        "price": str(exit_bid),
        "time_in_force": "gfd",
        "chain_symbol": symbol,
        "underlying_type": "index" if symbol == "XSP" else "equity",
    }
    review_result = mcp_call("review_option_order", review_args, dispatcher=dispatcher)
    review_proof = _validated_review_proof(
        review_args,
        review_result,
        protective_exit=protective,
    )

    # Place with idempotency key
    ref_id = ref_id or str(uuid.uuid4())
    place_args: dict[str, Any] = {
        "account_number": AGENTIC_ACCOUNT_NUMBER,
        "legs": [{"option_id": instrument_id, "side": "sell", "position_effect": "close"}],
        "quantity": "1",
        "type": "limit",
        "price": str(exit_bid),
        "time_in_force": "gfd",
        "ref_id": ref_id,
    }
    place_result = mcp_call(
        "place_option_order",
        place_args,
        dispatcher=dispatcher,
        protective_exit=protective,
        review_proof=review_proof,
    )

    order_id = _extract_placement_order_id(
        place_result,
        expected_ref_id=ref_id,
    )

    return {
        "orderId": order_id,
        "side": "sell",
        "positionEffect": "close",
        "limitPrice": exit_bid,
        "refId": ref_id,
    }


# ---------------------------------------------------------------------------
# Order status and fill verification
# ---------------------------------------------------------------------------

def verify_order_status(order_id: str, dispatcher=None) -> dict[str, Any]:
    """Check order status via get_option_orders.

    Returns {"state", "fillPrice"?, "filledQuantity"?, "rejectReason"?}.
    """
    result = mcp_call("get_option_orders", {
        "account_number": AGENTIC_ACCOUNT_NUMBER,
        "order_id": order_id,
    }, dispatcher=dispatcher)

    orders = result.get("data", {}).get("orders", [])
    if not orders:
        raise OrderBridgeError(f"order_not_found:{order_id}")

    order = orders[0]
    returned_order_id = order.get("id", order.get("order_id"))
    if str(returned_order_id or "") != str(order_id):
        raise OrderLifecycleHalted(
            f"order_identity_mismatch:requested={order_id}:returned={returned_order_id}"
        )
    state = order.get("state", "unknown")
    status: dict[str, Any] = {"state": state}

    legs = order.get("legs", [])
    if not isinstance(legs, list):
        legs = []

    # Robinhood returns both the older fill_price/fill_quantity fields and the
    # current processed_quantity + executions shape. Normalize both before a
    # caller can treat a filled state as ownership.
    price_candidates = [order.get("average_price"), order.get("average_fill_price")]
    quantity_candidates = [order.get("processed_quantity"), order.get("filled_quantity")]
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        price_candidates.extend([leg.get("fill_price"), leg.get("average_price")])
        quantity_candidates.extend([leg.get("fill_quantity"), leg.get("filled_quantity")])
        executions = leg.get("executions", [])
        if isinstance(executions, list):
            for execution in executions:
                if not isinstance(execution, dict):
                    continue
                price_candidates.append(execution.get("price"))
                quantity_candidates.append(execution.get("quantity"))

    for value in quantity_candidates:
        try:
            quantity = int(float(value))
        except (TypeError, ValueError, OverflowError):
            continue
        if quantity >= 0:
            status["filledQuantity"] = quantity
            break

    for value in price_candidates:
        try:
            price = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(price) and price > 0:
            status["fillPrice"] = price
            break

    reject_reason = order.get("reject_reason")
    if reject_reason:
        status["rejectReason"] = reject_reason

    return status


def _cancel_and_verify_zero_fill(order_id: str, dispatcher=None) -> dict[str, Any]:
    """Cancel an order and prove the exact broker order has zero fills."""
    try:
        cancel_result = mcp_call("cancel_option_order", {
            "account_number": AGENTIC_ACCOUNT_NUMBER,
            "order_id": order_id,
        }, dispatcher=dispatcher)
    except Exception as exc:
        raise OrderLifecycleHalted(f"cancel_dispatch_failed:{order_id}") from exc

    data = cancel_result.get("data") if isinstance(cancel_result, dict) else None
    acknowledged = False
    if isinstance(data, dict):
        acknowledged = data.get("cancelled") is True or data.get("canceled") is True
        candidates = data.get("orders")
        if not isinstance(candidates, list):
            candidates = [data.get("order")] if isinstance(data.get("order"), dict) else []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            returned_id = candidate.get("id", candidate.get("order_id", candidate.get("orderId")))
            if returned_id is not None and str(returned_id) != str(order_id):
                raise OrderLifecycleHalted(
                    f"cancel_identity_mismatch:requested={order_id}:returned={returned_id}"
                )
            if str(candidate.get("state", candidate.get("status", ""))).lower() in {
                "cancelled", "canceled", "voided", "failed", "pending_cancelled",
            }:
                acknowledged = True
    if not acknowledged:
        raise OrderLifecycleHalted(f"cancel_not_acknowledged:{order_id}")

    try:
        status = verify_order_status(order_id, dispatcher=dispatcher)
    except Exception as exc:
        raise OrderLifecycleHalted(f"cancel_status_unverifiable:{order_id}") from exc
    if status.get("state") not in {"cancelled", "canceled", "voided", "failed", "pending_cancelled"}:
        raise OrderLifecycleHalted(
            f"cancel_race_unresolved:{order_id}:{status.get('state', 'unknown')}"
        )
    if status.get("filledQuantity") != 0:
        raise OrderLifecycleHalted(
            f"cancel_has_fills:{order_id}:{status.get('filledQuantity', 'unknown')}"
        )
    return status


def wait_for_fill(
    order_id: str,
    timeout_seconds: float = 30.0,
    poll_interval: float = 1.0,
    dispatcher=None,
    expected_quantity: int = 1,
) -> dict[str, Any]:
    """Poll an order until a verified full fill or a terminal failure.

    Partial fills and provider states outside the known lifecycle halt the
    caller immediately. They must be reconciled before any new action.
    Pending orders alone may be polled until timeout, after which cancellation
    is attempted before raising OrderTimeout.
    """
    if expected_quantity <= 0:
        raise OrderLifecycleHalted("invalid_expected_quantity")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = verify_order_status(order_id, dispatcher=dispatcher)
        state = status.get("state", "")
        if state == "filled":
            try:
                filled_quantity = int(status["filledQuantity"])
                fill_price = float(status["fillPrice"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise OrderLifecycleHalted(
                    f"filled_order_unverifiable:{order_id}"
                ) from exc
            if (
                filled_quantity < expected_quantity
                or not math.isfinite(fill_price)
                or fill_price <= 0
            ):
                raise OrderLifecycleHalted(
                    f"filled_order_not_full:{order_id}:"
                    f"quantity={filled_quantity}:expected={expected_quantity}"
                )
            return status
        if state == "rejected":
            raise OrderRejected(f"order_rejected:{status.get('rejectReason', 'unknown')}")
        if state == "partially_filled":
            raise OrderLifecycleHalted(
                f"partial_fill_requires_reconciliation:{order_id}:"
                f"quantity={status.get('filledQuantity', 'unknown')}"
            )
        if state in OPEN_ORDER_STATES:
            time.sleep(poll_interval)
            continue
        if state in {"cancelled", "pending_cancelled", "failed", "voided"}:
            raise OrderRejected(f"order_not_filled:{state}:{order_id}")
        raise OrderLifecycleHalted(f"unknown_order_state:{state}:{order_id}")

    # Timeout: cancellation must be acknowledged and verified before retrying.
    _cancel_and_verify_zero_fill(order_id, dispatcher=dispatcher)
    raise OrderTimeout(f"order_timeout:{order_id}")


# ---------------------------------------------------------------------------
# Position reconciliation
# ---------------------------------------------------------------------------

def reconcile_positions(
    local: list[dict[str, Any]],
    dispatcher=None,
    *,
    account_number: str | None = None,
) -> dict[str, Any]:
    """Compare local state against broker positions.

    Returns {"matched", "mismatched", "orphanedLocal", "orphanedBroker",
             "brokerPositionCount", "localPositionCount"}.
    """
    account_number = account_number or AGENTIC_ACCOUNT_NUMBER
    if account_number != AGENTIC_ACCOUNT_NUMBER:
        raise OrderBridgeError("account_binding_mismatch")

    result = mcp_call("get_option_positions", {
        "account_number": account_number,
        "nonzero": True,
    }, dispatcher=dispatcher)

    position_data = result.get("data")
    if not isinstance(position_data, dict):
        raise OrderBridgeError("option_positions_response_malformed")
    broker_positions = position_data.get("results", position_data.get("positions"))
    if not isinstance(broker_positions, list):
        raise OrderBridgeError("option_positions_response_missing_results")

    matched: list[str] = []
    mismatched: list[str] = []
    orphaned_local: list[str] = []
    orphaned_broker: list[str] = []

    broker_by_id: dict[str, dict] = {}
    duplicate_broker_ids: set[str] = set()
    for bp in broker_positions:
        option_id = bp.get("option_id", "")
        if not option_id:
            continue
        if option_id in broker_by_id:
            # Duplicate broker rows make the position set unverifiable: fail closed.
            duplicate_broker_ids.add(option_id)
            continue
        broker_by_id[option_id] = bp

    for lp in local:
        inst_id = lp.get("instrumentId", "")
        if not inst_id:
            orphaned_local.append(inst_id)
            continue

        bp = broker_by_id.get(inst_id)
        if bp is None:
            orphaned_local.append(inst_id)
            continue

        if inst_id in duplicate_broker_ids:
            mismatched.append(inst_id)
            continue

        local_qty = int(lp.get("quantity", 0))
        broker_qty = int(bp.get("quantity", 0))
        local_side = lp.get("side", "")
        broker_type = bp.get("type", "")
        local_expiry = lp.get("expiry", "")
        broker_expiry = bp.get("expiration_date", "")

        # A local identity field requires a corresponding broker field that
        # matches; a missing broker field is unverifiable, not a match.
        side_matches = not local_side or (bool(broker_type) and local_side == broker_type)
        expiry_matches = not local_expiry or (bool(broker_expiry) and local_expiry == broker_expiry)
        if local_qty == broker_qty and side_matches and expiry_matches:
            matched.append(inst_id)
        else:
            mismatched.append(inst_id)

    local_ids = {lp.get("instrumentId", "") for lp in local}
    for bp in broker_positions:
        option_id = bp.get("option_id", "")
        if option_id and option_id not in local_ids:
            orphaned_broker.append(option_id)

    healthy = not (mismatched or orphaned_local or orphaned_broker)
    return {
        "healthy": healthy,
        "matched": matched,
        "mismatched": mismatched,
        "orphanedLocal": orphaned_local,
        "orphanedBroker": orphaned_broker,
        "brokerPositionCount": len(broker_positions),
        "localPositionCount": len(local),
    }


# ---------------------------------------------------------------------------
# Pre-live readiness check
# ---------------------------------------------------------------------------

def _mask_account(account_number: str) -> str:
    """Mask all but the last 4 characters of an account number."""
    if len(account_number) <= 4:
        return account_number
    return "\u2022\u2022\u2022\u2022" + account_number[-4:]


_OPTION_ID_CACHE: dict[str, str] = {}


def resolve_option_id(occ_symbol: str, dispatcher=None) -> str:
    """Resolve an OCC symbol to a Robinhood option instrument UUID.

    Calls get_option_chains then get_option_instruments, caches the result.
    """
    if occ_symbol in _OPTION_ID_CACHE:
        return _OPTION_ID_CACHE[occ_symbol]

    parsed = parse_occ(occ_symbol)
    chain_result = mcp_call("get_option_chains", {
        "chain_symbol": parsed["symbol"],
    }, dispatcher=dispatcher)

    chains = chain_result.get("data", {}).get("chains", [])
    if not chains:
        chains = chain_result.get("results", chain_result.get("data", []))
    if not isinstance(chains, list) or not chains:
        raise OrderBridgeError(f"no_option_chain:{parsed['symbol']}")
    chain_id = chains[0].get("id") if isinstance(chains[0], dict) else None
    if not chain_id:
        raise OrderBridgeError(f"chain_missing_id:{parsed['symbol']}")

    instruments_result = mcp_call("get_option_instruments", {
        "chain_id": chain_id,
        "expiration_date": parsed["expiry"],
        "type": parsed["option_type"],
        "strike_price": parsed["strike"],
    }, dispatcher=dispatcher)

    instruments = instruments_result.get("data", {}).get("results", [])
    if not instruments:
        instruments = instruments_result.get("results", [])
    if not isinstance(instruments, list) or not instruments:
        raise OrderBridgeError(
            f"no_instrument:{occ_symbol}:{parsed['expiry']}:{parsed['strike']}:{parsed['option_type']}"
        )
    option_id = instruments[0].get("id") if isinstance(instruments[0], dict) else None
    if not option_id:
        raise OrderBridgeError(f"instrument_missing_id:{occ_symbol}")
    _OPTION_ID_CACHE[occ_symbol] = option_id
    return option_id


def force_close_position(position: dict[str, Any], dispatcher=None) -> dict[str, Any]:
    """Emergency market close that remains available for protective exits."""
    instrument_id = position.get("instrumentId")
    if not instrument_id:
        raise OrderBridgeError("missing_instrument_id_for_emergency_exit")

    ref_id = str(uuid.uuid4())
    review_args: dict[str, Any] = {
        "account_number": AGENTIC_ACCOUNT_NUMBER,
        "legs": [{"option_id": instrument_id, "side": "sell", "position_effect": "close"}],
        "quantity": "1",
        "type": "market",
        "time_in_force": "gfd",
        "chain_symbol": position.get("symbol", ""),
        "underlying_type": "index" if position.get("symbol") == "XSP" else "equity",
    }
    review_result = mcp_call("review_option_order", review_args, dispatcher=dispatcher)
    review_proof = _validated_review_proof(
        review_args,
        review_result,
        protective_exit=True,
    )
    place_args: dict[str, Any] = {
        "account_number": AGENTIC_ACCOUNT_NUMBER,
        "legs": [{"option_id": instrument_id, "side": "sell", "position_effect": "close"}],
        "quantity": "1",
        "type": "market",
        "time_in_force": "gfd",
        "ref_id": ref_id,
    }
    place_result = mcp_call(
        "place_option_order",
        place_args,
        dispatcher=dispatcher,
        protective_exit=True,
        review_proof=review_proof,
    )

    order_id = _extract_placement_order_id(
        place_result,
        expected_ref_id=ref_id,
    )

    return {
        "orderId": order_id,
        "side": "sell",
        "positionEffect": "close",
        "orderType": "market",
        "refId": ref_id,
    }


def pre_live_readiness(dispatcher=None) -> dict[str, Any]:
    """Run pre-live readiness checks.

    Checks: kill switch, bound account, option level, buying power, open
    positions, and unresolved open orders.
    Returns {"ready", "checks", "accountNumber"}.
    """
    checks: list[dict[str, Any]] = []

    # 1. Kill switch
    ks_active = kill_switch_active()
    checks.append({
        "name": "kill_switch",
        "passed": not ks_active,
        "detail": "kill_switch_active" if ks_active else "ok",
    })

    # 2 & 3. Account and option level
    accounts_result = mcp_call("get_accounts", {}, dispatcher=dispatcher)
    accounts = accounts_result.get("data", {}).get("accounts", [])

    agentic_account = None
    for acct in accounts:
        if (
            acct.get("account_number") == AGENTIC_ACCOUNT_NUMBER
            and acct.get("agentic_allowed") is True
        ):
            option_level = str(acct.get("option_level", ""))
            level_num = 0
            if option_level.startswith("option_level_"):
                try:
                    level_num = int(option_level.split("_")[-1])
                except ValueError:
                    pass
            if level_num >= 2:
                agentic_account = acct
                break

    checks.append({
        "name": "agentic_account",
        "passed": agentic_account is not None,
        "detail": "agentic_account_found" if agentic_account else "no_agentic_account",
    })

    if agentic_account:
        option_level = str(agentic_account.get("option_level", ""))
        level_num = 0
        if option_level.startswith("option_level_"):
            try:
                level_num = int(option_level.split("_")[-1])
            except ValueError:
                pass
        checks.append({
            "name": "option_level",
            "passed": level_num >= 2,
            "detail": f"option_level_{level_num}",
        })
    else:
        checks.append({
            "name": "option_level",
            "passed": False,
            "detail": "no_agentic_account",
        })

    account_number = agentic_account.get("account_number", "") if agentic_account else ""

    # 4. Buying power
    buying_power = 0.0
    if account_number:
        portfolio_result = mcp_call("get_portfolio", {
            "account_number": account_number,
        }, dispatcher=dispatcher)
        bp_data = portfolio_result.get("data", {}).get("buying_power", {})
        if isinstance(bp_data, dict):
            bp_value = bp_data.get("buying_power", "0")
        else:
            bp_value = str(bp_data)
        try:
            buying_power = float(bp_value)
            if not math.isfinite(buying_power):
                buying_power = 0.0
        except (TypeError, ValueError):
            buying_power = 0.0

    checks.append({
        "name": "buying_power",
        "passed": buying_power >= MIN_BUYING_POWER,
        "detail": f"${buying_power:.2f}",
    })

    # 5. No open broker positions
    open_positions: list = []
    if account_number:
        positions_result = mcp_call("get_option_positions", {
            "account_number": account_number,
            "nonzero": True,
        }, dispatcher=dispatcher)
        position_data = positions_result.get("data")
        if not isinstance(position_data, dict):
            raise OrderBridgeError("option_positions_response_malformed")
        open_positions = position_data.get("results", position_data.get("positions"))
        if not isinstance(open_positions, list):
            raise OrderBridgeError("option_positions_response_missing_results")

    checks.append({
        "name": "no_open_positions",
        "passed": len(open_positions) == 0,
        "detail": f"{len(open_positions)}_open_positions",
    })

    # 6. No unresolved broker orders. Historical filled/canceled orders are
    # allowed; any live/pending order blocks unattended activation.
    open_orders: list = []
    if account_number:
        orders_result = mcp_call("get_option_orders", {
            "account_number": account_number,
        }, dispatcher=dispatcher)
        order_data = orders_result.get("data")
        if not isinstance(order_data, dict):
            raise OrderBridgeError("option_orders_response_malformed")
        open_orders = order_data.get("orders", order_data.get("results"))
        if not isinstance(open_orders, list):
            raise OrderBridgeError("option_orders_response_missing_orders")
    unresolved_orders = []
    for order in open_orders:
        if not isinstance(order, dict):
            unresolved_orders.append(order)
            continue
        raw_state = order.get("state", order.get("status"))
        state = raw_state.strip().lower() if isinstance(raw_state, str) else ""
        if state not in TERMINAL_ORDER_STATES:
            unresolved_orders.append(order)
    checks.append({
        "name": "no_open_orders",
        "passed": bool(account_number) and len(unresolved_orders) == 0,
        "detail": f"{len(unresolved_orders)}_open_orders",
    })

    ready = all(check["passed"] for check in checks)

    return {
        "ready": ready,
        "checks": checks,
        "accountNumber": _mask_account(account_number) if account_number else None,
    }
