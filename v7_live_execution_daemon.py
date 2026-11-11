#!/usr/bin/env python3
"""Live execution sidecar for the active V7 release.

Watches paper ledger files for entry/exit events. When live_authorized()
is true, mirrors those decisions to the broker via v7_live_order_bridge.

Does NOT modify any hashed source. Runs as a separate LaunchAgent.

Safety:
  - Full ledger read each tick (no cursor — failed events stay readable)
  - Dedup via processedEntries/processedExits sets
  - Pending entries kept separate from livePositions (no naked short risk)
  - Exits only processed for confirmed-filled livePositions
  - Kill switch blocks new entries; exits always attempted
  - Duplicate prevention via bridge's _logical_ref_id
  - Exponential backoff on retries
  - Reconciliation after exit fill
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")

STATE_DIR = Path.home() / ".hermes" / "state"
EXECUTION_STATE_PATH = STATE_DIR / "v7_live_execution_state.json"
LOCK_PATH = STATE_DIR / "v7_live_execution_daemon.lock"
PID_PATH = STATE_DIR / "v7_live_execution_daemon.pid"
HERMES_ROOT = Path.home() / ".hermes" / "hermes-agent"
HERMES_PYTHON = Path(sys.executable)

SYMBOLS = ("SPY", "QQQ")
POLL_INTERVAL_SECONDS = 1.0
UNAUTHORIZED_SLEEP_SECONDS = 5.0
ENTRY_FILL_TIMEOUT_SECONDS = 10.0
EXIT_FILL_TIMEOUT_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 30.0
RECONCILIATION_INTERVAL_SECONDS = 30.0

_STOP = False


class ExecutionStateCorrupt(RuntimeError):
    """Broker-critical local state is unreadable or structurally invalid."""


def _handle_signal(signum, frame):
    global _STOP
    _STOP = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load_state() -> dict:
    try:
        state = json.loads(EXECUTION_STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {
            "processedEntries": [],
            "processedExits": [],
            "livePositions": {},
            "pendingEntries": {},
            "retryBackoff": {},
            "lastCheckedAt": None,
            "lastReconciliationAt": None,
            "authorizedAt": None,
            "executionHalted": False,
        }
    except (json.JSONDecodeError, OSError) as exc:
        raise ExecutionStateCorrupt(f"cannot_read_execution_state:{exc}") from exc

    if not isinstance(state, dict):
        raise ExecutionStateCorrupt("execution_state_not_object")
    required_shapes = {
        "processedEntries": list,
        "processedExits": list,
        "livePositions": dict,
        "pendingEntries": dict,
        "retryBackoff": dict,
    }
    for key, expected_type in required_shapes.items():
        if key in state and not isinstance(state[key], expected_type):
            raise ExecutionStateCorrupt(f"execution_state_invalid_{key}")
        state.setdefault(key, expected_type())
    if not isinstance(state.get("reconciliationHealthy"), bool):
        state["reconciliationHealthy"] = False
    if state.get("lastReconciliationAt") is not None and not isinstance(
        state.get("lastReconciliationAt"), str
    ):
        raise ExecutionStateCorrupt("execution_state_invalid_lastReconciliationAt")
    state.setdefault("lastReconciliationAt", None)
    if not isinstance(state.get("authorized"), bool):
        state["authorized"] = False
    if "executionHalted" in state and not isinstance(state["executionHalted"], bool):
        raise ExecutionStateCorrupt("execution_state_invalid_executionHalted")
    state.setdefault("executionHalted", False)
    return state


def _save_state(state: dict) -> None:
    _atomic_write(EXECUTION_STATE_PATH, state)


def _deliver_alert(message: str) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(HERMES_ROOT) + (
        os.pathsep + env.get("PYTHONPATH", "")
    )
    for target in ("telegram", "photon"):
        try:
            subprocess.run(
                [str(HERMES_PYTHON), "-m", "hermes_cli.main", "send",
                 "--to", target, "--quiet", message],
                capture_output=True, text=True, timeout=30, check=False,
                env=env,
            )
        except Exception:
            pass


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _current_identity() -> dict:
    try:
        import v7_release
        import v7_scalp_paper as engine
        return {
            "engineVersion": str(v7_release.identity()["engineVersion"]),
            "strategyId": str(v7_release.identity()["strategyId"]),
            "releaseManifestSha256": str(v7_release.identity()["manifestSha256"]),
            "sourceCodeSha256": str(engine.source_code_sha256()),
            "configSha256": str(engine.config_sha256()),
            "liveDaemonSourceSha256": _file_sha256(SCRIPT_DIR / "v7_live_execution_daemon.py"),
            "liveBridgeSourceSha256": _file_sha256(SCRIPT_DIR / "v7_live_order_bridge.py"),
        }
    except Exception:
        return {}


PAPER_IDENTITY_FIELDS = (
    "engineVersion",
    "strategyId",
    "releaseManifestSha256",
    "sourceCodeSha256",
    "configSha256",
)


def _matches_identity(event: dict, identity: dict) -> bool:
    """Match only engine-side identity fields paper events can carry.

    liveDaemonSourceSha256/liveBridgeSourceSha256 are authorization-file
    fingerprints only — the paper engine cannot know them. Ignore extra
    identity fields; require every present paper field to match.
    """
    if not identity:
        return False
    if not any(k in PAPER_IDENTITY_FIELDS for k in identity):
        return False
    return all(
        event.get(k) == v
        for k, v in identity.items()
        if k in PAPER_IDENTITY_FIELDS
    )


def _kill_switch_active() -> bool:
    try:
        data = json.loads((STATE_DIR / "v7_kill_switch.json").read_text())
        if not isinstance(data, dict):
            return True
        return data.get("active") is not False
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return True  # fail-closed: missing/corrupt kill switch = halt


def _bridge_ref_id(position_id: str, action: str) -> str:
    import v7_release

    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{v7_release.STRATEGY_ID}:{action}:{position_id}",
        )
    )


RECOVERED_PENDING_STATES = frozenset({
    "queued", "confirmed", "pending", "pending_new", "unconfirmed",
    "submitted", "new", "created", "pending_cancel",
})

_ORDER_CHECK_ERROR = object()  # sentinel for lookup failure


def _check_existing_order(ref_id: str, order_id: str | None = None) -> dict | None | object:
    """Find a current-account order by exact ID, then by ref ID as fallback."""
    try:
        import v7_live_order_bridge as bridge
        today = datetime.now(ET).strftime("%Y-%m-%d")
        arguments = {
            "account_number": bridge.AGENTIC_ACCOUNT_NUMBER,
        }
        if order_id:
            arguments["order_id"] = order_id
        else:
            arguments["created_at_gte"] = today
        result = bridge.mcp_call("get_option_orders", arguments)
        data = result.get("data", {}) if isinstance(result, dict) else {}
        orders = data.get("orders", []) if isinstance(data, dict) else []
        if not orders and isinstance(result, dict):
            orders = result.get("orders", [])
        if not isinstance(orders, list):
            return _ORDER_CHECK_ERROR
        if order_id:
            for order in orders:
                if not isinstance(order, dict):
                    continue
                returned_id = order.get("id", order.get("order_id", order.get("orderId")))
                if str(returned_id or "") == str(order_id):
                    return order
            return _ORDER_CHECK_ERROR
        for order in orders:
            if isinstance(order, dict) and order.get("ref_id") == ref_id:
                return order
        return None
    except Exception:
        return _ORDER_CHECK_ERROR


def _order_filled_quantity(order: dict) -> int | None:
    candidates = [order.get("processed_quantity"), order.get("filled_quantity")]
    legs = order.get("legs", [])
    if isinstance(legs, list):
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            candidates.extend([leg.get("fill_quantity"), leg.get("filled_quantity")])
            executions = leg.get("executions", [])
            if isinstance(executions, list):
                candidates.extend(
                    execution.get("quantity")
                    for execution in executions
                    if isinstance(execution, dict)
                )
    for value in candidates:
        try:
            quantity = int(float(value))
        except (TypeError, ValueError, OverflowError):
            continue
        if quantity >= 0:
            return quantity
    return None


def _validated_existing_fill(order: dict, expected_quantity: int = 1) -> tuple[float, int]:
    """Require a complete, finite fill before creating local ownership."""
    state = str(order.get("state", order.get("status", ""))).lower()
    quantity = _order_filled_quantity(order)
    price = _order_fill_price(order)
    if state != "filled" or quantity != expected_quantity or price is None:
        raise RuntimeError(
            f"recovered_fill_unverifiable:state={state}:quantity={quantity}:"
            f"expected={expected_quantity}:price={price}"
        )
    return price, quantity


def _order_fill_price(order: dict) -> float | None:
    """Extract a finite per-share fill price from broker order shapes."""
    candidates = [
        order.get("average_price"),
        order.get("average_fill_price"),
        order.get("fill_price"),
    ]
    for leg in order.get("legs", []) if isinstance(order.get("legs"), list) else []:
        if isinstance(leg, dict):
            candidates.extend([
                leg.get("fill_price"),
                leg.get("average_price"),
            ])
            executions = leg.get("executions", [])
            if isinstance(executions, list):
                candidates.extend(
                    execution.get("price")
                    for execution in executions
                    if isinstance(execution, dict)
                )
    for value in candidates:
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0:
            return price
    return None


def _get_backoff(state: dict, key: str) -> float:
    current = state.get("retryBackoff", {}).get(key, 0)
    if current == 0:
        return 1.0
    return min(current * 2, MAX_BACKOFF_SECONDS)


def _set_backoff(state: dict, key: str, value: float) -> None:
    state.setdefault("retryBackoff", {})[key] = value


def _clear_backoff(state: dict, key: str) -> None:
    state.setdefault("retryBackoff", {}).pop(key, None)
    state.setdefault("retryBackoff", {}).pop(f"{key}:next", None)


def _persist_recovered_order_and_halt(
    state: dict,
    action: str,
    position_id: str,
    position: dict,
    existing: dict,
    ref_id: str,
    reason: str,
) -> None:
    """Keep a recovered broker receipt and fence all new actions."""
    order_id = existing.get("id", existing.get("order_id", existing.get("orderId", "")))
    if action == "entry":
        state.setdefault("pendingEntries", {})[position_id] = {
            **position,
            "orderId": order_id,
            "refId": ref_id,
        }
    elif position_id in state.get("livePositions", {}):
        state["livePositions"][position_id]["pendingExitOrderId"] = order_id
        state["livePositions"][position_id]["pendingExitRefId"] = ref_id
    halt = RuntimeError(f"{action}_recovery:{position_id}:{reason}")
    _halt_for_reconciliation(state, f"{action}_recovery", position_id, halt)


def _is_lifecycle_halt(exc: Exception) -> bool:
    """Identify broker lifecycle failures without coupling module import at startup."""
    try:
        import v7_live_order_bridge as bridge
        return isinstance(exc, bridge.OrderLifecycleHalted)
    except Exception:
        return False


def _halt_for_reconciliation(
    state: dict,
    action: str,
    position_id: str,
    exc: Exception,
) -> None:
    """Persist a hard lifecycle fence and keep broker ownership recoverable."""
    state["executionHalted"] = True
    state["reconciliationHealthy"] = False
    state["executionHaltedAt"] = datetime.now(ET).isoformat()
    state["executionHaltedReason"] = (
        f"{action}:{position_id}:{type(exc).__name__}:{exc}"
    )
    _save_state(state)
    message = (
        f"⛔ LIVE {action.upper()} HALTED: {position_id} — {exc}; "
        "manual broker reconciliation required before new actions"
    )
    print(message, flush=True)
    _deliver_alert(message)


def _local_positions_for_reconciliation(state: dict, exclude_position_id: str | None = None) -> list[dict]:
    """Build the minimal broker-position identity from durable live state."""
    local = []
    for position_id, position in state.get("livePositions", {}).items():
        if position_id == exclude_position_id:
            continue
        local.append({
            "positionId": position_id,
            "instrumentId": position.get("instrumentId", ""),
            "quantity": int(position.get("filledQuantity", position.get("quantity", 1)) or 1),
            "side": position.get("side", ""),
            "expiry": position.get("expiry", ""),
        })
    return local


def _reconciliation_due(
    *,
    authorized: bool,
    was_authorized: bool,
    healthy: bool,
    last_reconciliation_monotonic: float | None,
    now_monotonic: float,
) -> bool:
    """Require startup/recovery reconciliation and enforce periodic checks."""
    if not authorized:
        return False
    if not was_authorized or not healthy or last_reconciliation_monotonic is None:
        return True
    return (
        now_monotonic - last_reconciliation_monotonic
        >= RECONCILIATION_INTERVAL_SECONDS
    )


def _reconcile_live_state(state: dict) -> dict:
    """Reconcile durable local positions against the bound broker account."""
    import v7_live_order_bridge as bridge
    result = bridge.reconcile_positions(
        _local_positions_for_reconciliation(state),
        account_number=bridge.AGENTIC_ACCOUNT_NUMBER,
    )
    if not result.get("healthy"):
        raise RuntimeError(
            "reconciliation_mismatch:"
            f"local={result.get('orphanedLocal', [])},"
            f"broker={result.get('orphanedBroker', [])},"
            f"mismatched={result.get('mismatched', [])}"
        )
    state["lastReconciliationAt"] = datetime.now(PT).isoformat()
    return result


def _reconcile_after_exit(state: dict, position_id: str, bridge) -> bool:
    """Confirm the closed position is absent before deleting local ownership."""
    result = bridge.reconcile_positions(
        _local_positions_for_reconciliation(state, exclude_position_id=position_id),
        account_number=bridge.AGENTIC_ACCOUNT_NUMBER,
    )
    if result.get("healthy"):
        state["lastReconciliationAt"] = datetime.now(PT).isoformat()
        return True
    msg = (
        f"⚠️ EXIT FILLED but reconciliation mismatch: {position_id} — "
        f"orphaned broker={result.get('orphanedBroker', [])}, "
        f"mismatched={result.get('mismatched', [])}"
    )
    halt = RuntimeError(
        f"post_exit_reconciliation_mismatch:{position_id}:"
        f"broker={result.get('orphanedBroker', [])}:"
        f"mismatched={result.get('mismatched', [])}"
    )
    _halt_for_reconciliation(state, "exit_reconciliation", position_id, halt)
    print(msg, flush=True)
    _deliver_alert(msg)
    return False


def _read_all_ledger_events() -> list[dict]:
    """Read ALL ledger events. Dedup via processedEntries/processedExits.

    No file cursor — failed events must remain readable for retry.
    Files are small (hundreds of lines per symbol per day).
    """
    events = []
    for symbol in SYMBOLS:
        path = STATE_DIR / f"v7_scalp_trades_{symbol}.jsonl"
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        events.append(event)
                except json.JSONDecodeError:
                    continue
        except OSError:
            continue
    return events


def _mark_existing_as_processed(state: dict) -> None:
    live_ids = set(state.get("livePositions", {}))
    pending_ids = set(state.get("pendingEntries", {}))
    for symbol in SYMBOLS:
        path = STATE_DIR / f"v7_scalp_trades_{symbol}.jsonl"
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pid = event.get("positionId", "")
                if not pid:
                    continue
                if event.get("event") == "PAPER_ENTRY_REAL_ASK":
                    if pid not in live_ids and pid not in pending_ids and pid not in state["processedEntries"]:
                        state["processedEntries"].append(pid)
                elif event.get("event") == "PAPER_EXIT_REAL_BID":
                    # A missed exit for a known live position must remain
                    # readable so the protective close can be retried.
                    if pid not in live_ids and pid not in pending_ids and pid not in state["processedExits"]:
                        state["processedExits"].append(pid)
        except OSError:
            continue


def _process_entry(event: dict, state: dict, identity: dict) -> None:
    position_id = event.get("positionId", "")
    if not position_id or position_id in state["processedEntries"]:
        return
    if not _matches_identity(event, identity):
        return
    if state.get("executionHalted"):
        return
    if _kill_switch_active():
        return

    backoff_key = f"entry:{position_id}"
    next_retry = state.get("retryBackoff", {}).get(f"{backoff_key}:next", 0)
    if next_retry and time.monotonic() < next_retry:
        return

    instrument_id = event.get("instrumentId", "")
    if not instrument_id:
        # Fallback: resolve OCC symbol to instrument UUID via broker
        occ_symbol = event.get("occSymbol", "")
        if occ_symbol:
            try:
                import v7_live_order_bridge as _bridge
                instrument_id = _bridge.resolve_option_id(occ_symbol)
                print(f"ENTRY {position_id}: resolved OCC {occ_symbol} -> {instrument_id}", flush=True)
            except Exception as exc:
                print(f"ENTRY {position_id}: OCC resolution failed: {exc}", flush=True)
                # Retryable: set backoff, don't mark as processed
                backoff = _get_backoff(state, backoff_key)
                _set_backoff(state, backoff_key, backoff)
                state.setdefault("retryBackoff", {})[f"{backoff_key}:next"] = time.monotonic() + backoff
                return
        else:
            state["processedEntries"].append(position_id)
            return

    entry_ask = float(event.get("entryAsk", 0))
    if entry_ask <= 0:
        state["processedEntries"].append(position_id)
        return

    symbol = event.get("symbol", "")
    position = {
        "positionId": position_id,
        "symbol": symbol,
        "occSymbol": event.get("occSymbol", ""),
        "instrumentId": instrument_id,
        "entryAsk": entry_ask,
        "entryTime": event.get("entryTime", ""),
        "expiry": event.get("expiry", ""),
        "strike": event.get("strike"),
        "direction": event.get("direction"),
        "optionType": event.get("optionType"),
        "signal": event.get("signal", {}),
        "turtleN": event.get("turtleN", 0.0),
        "engineVersion": event.get("engineVersion", ""),
        "strategyId": event.get("strategyId", ""),
        "releaseManifestSha256": event.get("releaseManifestSha256", ""),
        "sourceCodeSha256": event.get("sourceCodeSha256", ""),
        "configSha256": event.get("configSha256", ""),
        "chainSymbol": event.get("chainSymbol", symbol),
        "underlyingType": event.get("underlyingType", "index" if symbol == "XSP" else "equity"),
    }

    # Duplicate prevention. After a restart, poll the durable exact broker ID
    # first; ref_id search is only the pre-submission fallback.
    ref_id = _bridge_ref_id(position_id, "entry")
    pending = state.get("pendingEntries", {}).get(position_id)
    persisted_order_id = pending.get("orderId") if isinstance(pending, dict) else None
    existing = _check_existing_order(ref_id, persisted_order_id)
    if existing is _ORDER_CHECK_ERROR:
        if persisted_order_id:
            _halt_for_reconciliation(
                state,
                "entry_recovery",
                position_id,
                RuntimeError(f"exact_order_lookup_failed:{persisted_order_id}"),
            )
        return
    if existing:
        order_state = str(existing.get("state", existing.get("status", "unknown"))).lower()
        order_id = existing.get("id", existing.get("order_id", existing.get("orderId", "")))
        if order_state == "filled":
            try:
                fill_price, filled_quantity = _validated_existing_fill(existing)
            except Exception as exc:
                _persist_recovered_order_and_halt(
                    state, "entry", position_id, position, existing, ref_id, str(exc)
                )
                return
            state["pendingEntries"].pop(position_id, None)
            state["livePositions"][position_id] = {
                **position,
                "orderId": order_id,
                "fillPrice": fill_price,
                "filledQuantity": filled_quantity,
                "quantity": filled_quantity,
                "side": "long",
                "filledAt": datetime.now(ET).isoformat(),
            }
            state["processedEntries"].append(position_id)
            _clear_backoff(state, backoff_key)
            print(f"ENTRY {position_id}: already filled (dup check)", flush=True)
            return
        if order_state == "partially_filled" or order_state not in RECOVERED_PENDING_STATES:
            _persist_recovered_order_and_halt(
                state, "entry", position_id, position, existing, ref_id,
                f"unresolved_order_state:{order_state}",
            )
            return
        # Known pending state: preserve the exact receipt and wait for a later
        # exact-ID poll. It must not become live ownership yet.
        state["pendingEntries"][position_id] = {
            **position,
            "orderId": order_id,
            "refId": ref_id,
        }
        print(f"ENTRY {position_id}: order pending {order_state}", flush=True)
        return

    try:
        import v7_live_order_bridge as bridge
        print(f"LIVE ENTRY: submitting {symbol} {position_id} @ ${entry_ask}", flush=True)
        result = bridge.submit_entry_order(position, ref_id=ref_id)
        order_id = result.get("orderId") if isinstance(result, dict) else None
        if not isinstance(order_id, str) or not order_id:
            raise RuntimeError("entry_order_missing_order_id")
        # Persist the broker receipt before any status lookup. If the process
        # stops while the order is pending, startup can reconcile this entry
        # instead of submitting a second order.
        state["pendingEntries"][position_id] = {
            **position,
            "orderId": order_id,
            "refId": ref_id,
            "submittedAt": datetime.now(ET).isoformat(),
        }
        _save_state(state)
        fill = bridge.wait_for_fill(
            order_id,
            timeout_seconds=ENTRY_FILL_TIMEOUT_SECONDS,
        )
        state["pendingEntries"].pop(position_id, None)
        # Only add to livePositions AFTER fill confirmation
        state["livePositions"][position_id] = {
            **position,
            "orderId": result["orderId"],
            "fillPrice": fill.get("fillPrice"),
            "filledQuantity": fill.get("filledQuantity"),
            "quantity": fill.get("filledQuantity", 1),
            "side": "long",
            "filledAt": datetime.now(ET).isoformat(),
        }
        state["processedEntries"].append(position_id)
        _clear_backoff(state, backoff_key)
        msg = (f"🟢 LIVE ENTRY FILLED: {symbol} {position_id} — "
               f"fill ${fill.get('fillPrice')}, order {result['orderId'][:8]}")
        print(msg, flush=True)
        _deliver_alert(msg)
    except Exception as exc:
        if _is_lifecycle_halt(exc):
            _halt_for_reconciliation(state, "entry", position_id, exc)
            return
        backoff = _get_backoff(state, backoff_key)
        _set_backoff(state, backoff_key, backoff)
        state.setdefault("retryBackoff", {})[f"{backoff_key}:next"] = time.monotonic() + backoff
        msg = f"🔴 LIVE ENTRY FAILED: {position_id} — {type(exc).__name__}: {exc} (retry in {backoff:.0f}s)"
        print(msg, flush=True)
        _deliver_alert(msg)


def _recover_pending_entry(state: dict, position_id: str) -> None:
    """Recover one durable pending entry using its exact broker order ID."""
    pending = state.get("pendingEntries", {}).get(position_id)
    if not isinstance(pending, dict):
        return
    order_id = pending.get("orderId")
    if not isinstance(order_id, str) or not order_id:
        _halt_for_reconciliation(
            state,
            "entry_recovery",
            position_id,
            RuntimeError("pending_entry_missing_order_id"),
        )
        return
    ref_id = str(pending.get("refId") or _bridge_ref_id(position_id, "entry"))
    existing = _check_existing_order(ref_id, order_id)
    if existing is _ORDER_CHECK_ERROR or not isinstance(existing, dict):
        _halt_for_reconciliation(
            state,
            "entry_recovery",
            position_id,
            RuntimeError(f"exact_order_lookup_failed:{order_id}"),
        )
        return
    order_state = str(existing.get("state", existing.get("status", "unknown"))).lower()
    if order_state == "filled":
        try:
            fill_price, filled_quantity = _validated_existing_fill(existing)
        except Exception as exc:
            _persist_recovered_order_and_halt(
                state, "entry", position_id, pending, existing, ref_id, str(exc)
            )
            return
        state["pendingEntries"].pop(position_id, None)
        state.setdefault("livePositions", {})[position_id] = {
            **pending,
            "orderId": order_id,
            "fillPrice": fill_price,
            "filledQuantity": filled_quantity,
            "quantity": filled_quantity,
            "side": "long",
            "filledAt": datetime.now(ET).isoformat(),
        }
        state.setdefault("processedEntries", []).append(position_id)
        _clear_backoff(state, f"entry:{position_id}")
        print(f"ENTRY {position_id}: pending → filled (exact ID)", flush=True)
        return
    if order_state == "partially_filled" or order_state not in RECOVERED_PENDING_STATES:
        _persist_recovered_order_and_halt(
            state, "entry", position_id, pending, existing, ref_id,
            f"unresolved_order_state:{order_state}",
        )
        return
    print(f"ENTRY {position_id}: exact order pending {order_state}", flush=True)


def _process_exit(event: dict, state: dict, identity: dict) -> None:
    position_id = event.get("positionId", "")
    if not position_id or position_id in state["processedExits"]:
        return

    # Only process exit if entry fill was CONFIRMED in livePositions
    live_pos = state["livePositions"].get(position_id)
    if not live_pos:
        # Check if it's still pending — skip exit until entry fills.
        if position_id in state.get("pendingEntries", {}):
            return
        # During an authorized but unreconciled startup, an unknown exit may
        # correspond to an orphan broker position. Never fence it as history.
        if state.get("authorized") and not state.get("reconciliationHealthy", False):
            return
        # Paper-only position (entry happened before authorization) — skip.
        if position_id not in state["processedExits"]:
            state["processedExits"].append(position_id)
        return

    if not _matches_identity(event, identity):
        return

    # NO kill switch check — exits must always proceed

    backoff_key = f"exit:{position_id}"
    next_retry = state.get("retryBackoff", {}).get(f"{backoff_key}:next", 0)
    if next_retry and time.monotonic() < next_retry:
        return

    exit_bid = float(event.get("exitBid", 0))
    if exit_bid <= 0:
        # A malformed/absent bid is retryable evidence, not a completed exit.
        return

    symbol = live_pos.get("symbol", event.get("symbol", ""))
    position = {
        "positionId": position_id,
        "symbol": symbol,
        "occSymbol": live_pos.get("occSymbol", event.get("occSymbol", "")),
        "instrumentId": live_pos["instrumentId"],
        "chainSymbol": symbol,
        "underlyingType": "index" if symbol == "XSP" else "equity",
    }

    # Duplicate prevention for exits. A durable pending close is recovered by
    # exact order ID before falling back to the deterministic ref ID.
    ref_id = _bridge_ref_id(position_id, "exit")
    persisted_order_id = live_pos.get("pendingExitOrderId")
    existing = _check_existing_order(ref_id, persisted_order_id)
    if existing is _ORDER_CHECK_ERROR:
        if persisted_order_id:
            _halt_for_reconciliation(
                state,
                "exit_recovery",
                position_id,
                RuntimeError(f"exact_order_lookup_failed:{persisted_order_id}"),
            )
        return
    if existing:
        order_state = str(existing.get("state", existing.get("status", "unknown"))).lower()
        order_id = existing.get("id", existing.get("order_id", existing.get("orderId", "")))
        expected_quantity = int(live_pos.get("filledQuantity", live_pos.get("quantity", 1)) or 1)
        if order_state == "filled":
            try:
                _validated_existing_fill(existing, expected_quantity)
            except Exception as exc:
                _persist_recovered_order_and_halt(
                    state, "exit", position_id, position, existing, ref_id, str(exc)
                )
                return
            import v7_live_order_bridge as bridge
            if not _reconcile_after_exit(state, position_id, bridge):
                return
            del state["livePositions"][position_id]
            state["processedExits"].append(position_id)
            _clear_backoff(state, backoff_key)
            print(f"EXIT {position_id}: already filled (dup check)", flush=True)
            return
        if order_state == "partially_filled" or order_state not in RECOVERED_PENDING_STATES:
            _persist_recovered_order_and_halt(
                state, "exit", position_id, position, existing, ref_id,
                f"unresolved_order_state:{order_state}",
            )
            return
        live_pos["pendingExitOrderId"] = order_id
        live_pos["pendingExitRefId"] = ref_id
        print(f"EXIT {position_id}: order pending {order_state}", flush=True)
        return

    try:
        import v7_live_order_bridge as bridge
        print(f"LIVE EXIT: submitting {symbol} {position_id} @ ${exit_bid}", flush=True)
        result = bridge.submit_exit_order(
            position,
            exit_bid,
            ref_id=ref_id,
            protective=True,
        )
        order_id = result.get("orderId") if isinstance(result, dict) else None
        if not isinstance(order_id, str) or not order_id:
            raise RuntimeError("exit_order_missing_order_id")
        # Keep broker ownership durable until the fill and reconciliation both
        # succeed. A restart can then recover this exact order instead of
        # submitting a duplicate close.
        live_pos["pendingExitOrderId"] = order_id
        live_pos["pendingExitRefId"] = ref_id
        live_pos["pendingExitSubmittedAt"] = datetime.now(ET).isoformat()
        _save_state(state)
        fill = bridge.wait_for_fill(
            order_id,
            timeout_seconds=EXIT_FILL_TIMEOUT_SECONDS,
        )

        # Reconcile before deleting local ownership. A local PAPER/LIVE exit
        # record is not evidence that the broker position is actually closed.
        if not _reconcile_after_exit(state, position_id, bridge):
            raise RuntimeError("post_exit_reconciliation_failed")

        del state["livePositions"][position_id]
        state["processedExits"].append(position_id)
        _clear_backoff(state, backoff_key)
        pnl = float(event.get("pnl", 0))
        msg = (f"🟢 LIVE EXIT FILLED: {symbol} {position_id} — "
               f"fill ${fill.get('fillPrice')}, P&L ${pnl:+.2f}")
        print(msg, flush=True)
        _deliver_alert(msg)
    except Exception as exc:
        if _is_lifecycle_halt(exc):
            _halt_for_reconciliation(state, "exit", position_id, exc)
            return
        backoff = _get_backoff(state, backoff_key)
        _set_backoff(state, backoff_key, backoff)
        state.setdefault("retryBackoff", {})[f"{backoff_key}:next"] = time.monotonic() + backoff
        msg = f"🔴 LIVE EXIT FAILED: {position_id} — {type(exc).__name__}: {exc} (retry in {backoff:.0f}s)"
        print(msg, flush=True)
        _deliver_alert(msg)


def monitor_live_position(
    state: dict,
    live_pos: dict,
    identity: dict,
    *,
    now: datetime | None = None,
) -> dict | None:
    """Monitor one filled broker position using the exact live instrument.

    The paper ledger remains the source of structural completed-bar exits, but
    quote-driven exits are evaluated here against the actual live fill price.
    A triggered close is routed through the existing idempotent protective
    sell-to-close path. This function never opens a position.
    """
    if not identity:
        return None
    close_only = not all(live_pos.get(key) == value for key, value in identity.items())
    try:
        fill_price = float(live_pos.get("fillPrice"))
        quantity = int(live_pos.get("filledQuantity", live_pos.get("quantity", 1)) or 1)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(fill_price) or fill_price <= 0 or quantity <= 0:
        return None
    entry_time = live_pos.get("filledAt") or live_pos.get("entryTime")
    if not isinstance(entry_time, str) or not entry_time:
        return None
    current_time = now or datetime.now(ET)
    policy_position = dict(live_pos)
    policy_position["entryAsk"] = fill_price
    policy_position["entryTime"] = entry_time
    try:
        import v7_live_order_bridge as bridge
        import v7_scalp_paper as engine

        quote = bridge.get_option_quote_by_instrument(
            live_pos.get("instrumentId", ""),
            live_pos.get("occSymbol", ""),
        )
        bid, quote_reason = engine.validate_exit_quote(
            quote,
            expected_occ_symbol=str(live_pos.get("occSymbol", "")),
            now=current_time,
        )
        if bid is None:
            live_pos["lastMonitorError"] = f"invalid_live_exit_quote:{quote_reason}"
            return None
        ask = float(quote["ask"])
        venue_timestamp = quote.get("timestamp")
        pnl = (bid - fill_price) * 100.0 * quantity
        pnl_pct = (bid / fill_price - 1.0) * 100.0
        held_minutes = (
            current_time.astimezone(ET)
            - datetime.fromisoformat(entry_time).astimezone(ET)
        ).total_seconds() / 60.0
        live_pos["lastQuote"] = {
            "bid": bid,
            "ask": ask,
            "pnl": round(pnl, 2),
            "pnlPct": round(pnl_pct, 2),
            "at": current_time.astimezone(ET).isoformat(),
            "quoteTimestamp": venue_timestamp,
            "quoteAgeSeconds": engine.quote_freshness_seconds(quote, now=current_time),
        }
        duplicate_timestamp = (
            isinstance(venue_timestamp, str)
            and venue_timestamp
            and live_pos.get("lastQuoteTimestamp") == venue_timestamp
        )
        live_pos["lastQuoteTimestamp"] = venue_timestamp
        live_pos["peakBid"] = max(float(live_pos.get("peakBid", fill_price)), bid)
        reason = engine.price_exit_reason(
            policy_position,
            bid,
            now=current_time,
            peak_bid=live_pos["peakBid"],
            entry_price=fill_price,
            symbol=str(live_pos.get("symbol", "")),
        )
        if duplicate_timestamp and reason in engine.SOFT_EXIT_REASONS:
            return None
        if reason is None:
            live_pos.pop("pendingExitReason", None)
            live_pos.pop("exitConfirmCount", None)
            return None

        if reason in engine.SOFT_EXIT_REASONS:
            pending = live_pos.get("pendingExitReason")
            confirm_count = int(live_pos.get("exitConfirmCount", 0)) + 1
            required_ticks = engine.MIN_CONFIRMATION_TICKS
            if pending != reason or confirm_count < required_ticks:
                live_pos["pendingExitReason"] = reason
                live_pos["exitConfirmCount"] = confirm_count
                return None

        live_pos.pop("pendingExitReason", None)
        live_pos.pop("exitConfirmCount", None)
        exit_event = {
            "event": "PAPER_EXIT_REAL_BID",
            "positionId": live_pos["positionId"],
            "symbol": live_pos.get("symbol", ""),
            "occSymbol": live_pos["occSymbol"],
            "reason": reason,
            "entryAsk": fill_price,
            "exitBid": bid,
            "exitAskAtCheck": ask,
            "pnl": round(pnl, 2),
            "pnlPct": round(pnl_pct, 2),
            "heldMinutes": round(held_minutes, 1),
            "quoteSource": "Robinhood MCP",
            "quoteTimestamp": venue_timestamp,
            "quoteAgeSeconds": engine.quote_freshness_seconds(quote, now=current_time),
            "closeOnly": close_only,
            **identity,
        }
        _process_exit(exit_event, state, identity)
        if live_pos["positionId"] not in state.get("livePositions", {}):
            return {"action": "LIVE_EXIT_TRIGGERED", **exit_event}
        return None
    except Exception as exc:
        live_pos["lastMonitorError"] = f"live_monitor_error:{type(exc).__name__}:{exc}"
        return None


def main() -> int:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"action": "ERROR", "reason": "execution_daemon_already_running"}))
            return 2

        _atomic_write(PID_PATH, {"pid": os.getpid(), "startedAt": datetime.now(ET).isoformat()})

        try:
            from v7_live_auth import live_authorized
        except Exception:
            def live_authorized() -> bool:
                return False

        identity = _current_identity()
        state = _load_state()
        was_authorized = False
        last_reconciliation_monotonic = None

        print(f"V7 live execution sidecar started — authorized={live_authorized()}", flush=True)

        while not _STOP:
            try:
                authorized = live_authorized()
                state["authorized"] = authorized
                state["lastCheckedAt"] = datetime.now(PT).isoformat()

                if (
                    not state.get("executionHalted")
                    and _reconciliation_due(
                        authorized=authorized,
                        was_authorized=was_authorized,
                        healthy=state.get("reconciliationHealthy", False),
                        last_reconciliation_monotonic=last_reconciliation_monotonic,
                        now_monotonic=time.monotonic(),
                    )
                ):
                    if state.get("executionHalted"):
                        state["reconciliationHealthy"] = False
                        print(
                            "Live execution remains fenced: manual reconciliation "
                            "required before resuming"
                        , flush=True)
                    else:
                        try:
                            _reconcile_live_state(state)
                            last_reconciliation_monotonic = time.monotonic()
                            if not state.get("reconciliationHealthy", False):
                                # Only fence historical paper events after the
                                # broker/local ownership snapshot is consistent.
                                _mark_existing_as_processed(state)
                                state["authorizedAt"] = datetime.now(PT).isoformat()
                                msg = "V7 LIVE EXECUTION ACTIVATED — mirroring new trades only"
                                print(msg, flush=True)
                                _deliver_alert(msg)
                            state["reconciliationHealthy"] = True
                        except Exception as recon_exc:
                            state["reconciliationHealthy"] = False
                            print(f"Live startup reconciliation blocked: {recon_exc}", flush=True)
                elif not authorized and was_authorized:
                    state["authorizedAt"] = None
                    state["reconciliationHealthy"] = False
                    print("V7 live execution deactivated", flush=True)

                was_authorized = authorized

                events = _read_all_ledger_events()

                # ALWAYS process exits for existing livePositions first,
                # regardless of authorization state. If authorization drops
                # mid-trade, we must still close open broker positions.
                for event in events:
                    if event.get("event") == "PAPER_EXIT_REAL_BID":
                        _process_exit(event, state, identity)

                if authorized and state.get("reconciliationHealthy", False):
                    # Recover pending entries only through their durable exact
                    # broker order IDs; the helper fences ambiguous states.
                    for pid in list(state.get("pendingEntries", {}).keys()):
                        _recover_pending_entry(state, pid)

                    for event in events:
                        if event.get("event") == "PAPER_ENTRY_REAL_ASK":
                            _process_entry(event, state, identity)

                # Quote-driven exits run for every confirmed live position,
                # including after authorization drops. Protective closes remain
                # available even when new live entries are fenced.
                for pid in list(state.get("livePositions", {}).keys()):
                    live_pos = state["livePositions"].get(pid)
                    if not isinstance(live_pos, dict):
                        continue
                    monitor_result = monitor_live_position(state, live_pos, identity)
                    if monitor_result and monitor_result.get("action") == "LIVE_EXIT_TRIGGERED":
                        print(json.dumps(monitor_result, separators=(",", ":")), flush=True)

                if not authorized or not state.get("reconciliationHealthy", False):
                    _save_state(state)
                    time.sleep(UNAUTHORIZED_SLEEP_SECONDS if not authorized else POLL_INTERVAL_SECONDS)
                    continue

                _save_state(state)
                time.sleep(POLL_INTERVAL_SECONDS)

            except Exception as exc:
                print(f"Execution daemon error: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(POLL_INTERVAL_SECONDS)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
