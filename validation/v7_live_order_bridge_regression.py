#!/usr/bin/env python3
"""Regression tests for the V7.4.23 live order bridge.

Uses mock MCP dispatchers — never calls real Robinhood. Tests cover:
- Kill switch blocks all submission
- Review failure blocks submission (fail-closed)
- Successful entry/exit order submission with correct leg structure
- Order rejection raises OrderRejected
- Timeout auto-cancels the pending order before raising
- Position reconciliation matches instrument ID + quantity + side
- Pre-live readiness all-pass and various fail cases
- Idempotency: ref_id is unique per order
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _make_mock_dispatcher(responses: dict[str, list[dict]]):
    """Create a mock dispatcher that returns queued responses per method name.

    responses: {"method_name": [{"data": {...}}, ...]}
    Each call pops the first response for that method.
    """
    queue = {k: list(v) for k, v in responses.items()}

    def dispatcher(tool_name: str, arguments: dict):
        # Strip the MCP prefix to get the method name
        method = tool_name.replace("mcp__robinhood_trading__", "")
        if method not in queue or not queue[method]:
            if method == "get_option_orders":
                return {"data": {"orders": []}}
            raise RuntimeError(f"unexpected_dispatch:{method}")
        return queue[method].pop(0)

    return dispatcher


def _order_response(order_id: str, state: str = "filled", fill_price: str = "1.50"):
    return {
        "data": {
            "orders": [{
                "id": order_id,
                "state": state,
                "legs": [{
                    "option_id": "test-instrument-uuid",
                    "side": "buy",
                    "position_effect": "open",
                    "fill_price": fill_price,
                    "fill_quantity": "1",
                }],
            }]
        }
    }


class KillSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ks_path = Path(self.tmpdir) / "v7_kill_switch.json"
        self.live_state_path = Path(self.tmpdir) / "v7_live_order_state.json"
        self.ks_path.write_text(json.dumps({"active": False, "reason": "test"}))

    def test_kill_switch_blocks_entry_order(self):
        import v7_live_order_bridge as bridge
        self.ks_path.write_text(json.dumps({"active": True, "reason": "test"}))
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            with self.assertRaises(bridge.KillSwitchActive):
                bridge.submit_entry_order(
                    {"instrumentId": "test-uuid", "occSymbol": "SPY123", "entryAsk": 1.50, "symbol": "SPY"},
                    dispatcher=lambda *a: None,
                )

    def test_kill_switch_blocks_exit_order(self):
        import v7_live_order_bridge as bridge
        self.ks_path.write_text(json.dumps({"active": True, "reason": "test"}))
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            with self.assertRaises(bridge.KillSwitchActive):
                bridge.submit_exit_order(
                    {"instrumentId": "test-uuid", "occSymbol": "SPY123", "symbol": "SPY"},
                    exit_bid=1.20,
                    dispatcher=lambda *a: None,
                )

    def test_no_kill_switch_allows_state_check(self):
        import v7_live_order_bridge as bridge
        # An explicit inactive file is required; missing state fails closed.
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            self.assertFalse(bridge.kill_switch_active())

    def test_malformed_kill_switch_blocks_state_check(self):
        import v7_live_order_bridge as bridge
        self.ks_path.write_text(json.dumps({"reason": "missing_active_field"}))
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            self.assertTrue(bridge.kill_switch_active())

    def test_non_boolean_kill_switch_does_not_clear_state(self):
        import v7_live_order_bridge as bridge
        self.ks_path.write_text(json.dumps({"active": 0}))
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            self.assertTrue(bridge.kill_switch_active())

    def test_shared_authorization_blocks_entry(self):
        import v7_live_order_bridge as bridge
        position = {
            "positionId": "paper-auth-blocked",
            "instrumentId": "inst-auth-blocked",
            "occSymbol": "SPY260904C00560000",
            "symbol": "SPY",
            "entryAsk": 1.50,
        }
        calls = []

        def dispatcher(*args):
            calls.append(args)
            raise AssertionError("dispatcher_must_not_be_called")

        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            with patch.object(bridge, "live_authorized", return_value=False):
                with self.assertRaises(bridge.LiveAuthorizationRequired):
                    bridge.submit_entry_order(position, dispatcher=dispatcher)
        self.assertEqual([], calls)
    def test_raw_placement_dispatch_also_requires_authorization(self):
        import v7_live_order_bridge as bridge
        calls = []

        def dispatcher(*args):
            calls.append(args)
            raise AssertionError("dispatcher_must_not_be_called")

        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            with patch.object(bridge, "live_authorized", return_value=False):
                with self.assertRaises(bridge.LiveAuthorizationRequired):
                    bridge.mcp_call("place_option_order", {}, dispatcher=dispatcher)
        self.assertEqual([], calls)

    def test_shared_authorization_blocks_exit(self):
        import v7_live_order_bridge as bridge
        position = {
            "positionId": "paper-auth-blocked-exit",
            "instrumentId": "inst-auth-blocked-exit",
            "occSymbol": "SPY260904C00560000",
            "symbol": "SPY",
        }
        calls = []

        def dispatcher(*args):
            calls.append(args)
            raise AssertionError("dispatcher_must_not_be_called")

        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            with patch.object(bridge, "live_authorized", return_value=False):
                with self.assertRaises(bridge.LiveAuthorizationRequired):
                    bridge.submit_exit_order(position, 1.20, dispatcher=dispatcher)
        self.assertEqual([], calls)


class EntryOrderTests(unittest.TestCase):
    def setUp(self):
        import v7_live_order_bridge as bridge
        self.auth_patch = patch.object(bridge, "live_authorized", return_value=True)
        self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.tmpdir = tempfile.mkdtemp()
        self.ks_path = Path(self.tmpdir) / "v7_kill_switch.json"
        self.ks_path.write_text(json.dumps({"active": False, "reason": "test"}))
        self.live_state_path = Path(self.tmpdir) / "v7_live_order_state.json"

    def test_authorized_raw_placement_requires_review_proof(self):
        import v7_live_order_bridge as bridge
        from unittest.mock import Mock

        dispatcher = Mock()
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            with self.assertRaises(bridge.ReviewRequired):
                bridge.mcp_call(
                    "place_option_order",
                    {"account_number": bridge.AGENTIC_ACCOUNT_NUMBER},
                    dispatcher=dispatcher,
                )
        dispatcher.assert_not_called()

    def test_malformed_review_response_blocks_placement(self):
        import v7_live_order_bridge as bridge
        calls = []

        def dispatcher(tool_name, arguments):
            method = tool_name.replace("mcp__robinhood_trading__", "")
            calls.append(method)
            if method == "review_option_order":
                return {"data": {}}
            raise AssertionError("placement_must_not_follow_malformed_review")

        position = {
            "instrumentId": "inst-uuid-123",
            "occSymbol": "SPY260904C00560000",
            "entryAsk": 1.50,
            "symbol": "SPY",
        }
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            with self.assertRaises(bridge.ReviewRequired):
                bridge.submit_entry_order(position, dispatcher=dispatcher)
        self.assertEqual(["review_option_order"], calls)

    def test_missing_placement_receipt_halts_before_return(self):
        import v7_live_order_bridge as bridge

        position = {
            "instrumentId": "inst-uuid-123",
            "occSymbol": "SPY260904C00560000",
            "entryAsk": 1.50,
            "symbol": "SPY",
        }
        dispatcher = _make_mock_dispatcher({
            "review_option_order": [{"data": {"order_checks": []}}],
            "place_option_order": [{"data": {"orders": [{"state": "queued"}]}}],
        })
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            with self.assertRaises(bridge.OrderLifecycleHalted):
                bridge.submit_entry_order(position, dispatcher=dispatcher)

    def test_successful_entry_submission(self):
        import v7_live_order_bridge as bridge
        position = {
            "instrumentId": "inst-uuid-123",
            "occSymbol": "SPY260904C00560000",
            "entryAsk": 1.50,
            "symbol": "SPY",
        }
        review_resp = {"data": {"order_checks": []}}
        place_resp = _order_response("order-abc", state="filled", fill_price="1.50")

        dispatcher = _make_mock_dispatcher({
            "review_option_order": [review_resp],
            "place_option_order": [place_resp],
        })

        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            result = bridge.submit_entry_order(position, dispatcher=dispatcher)

        self.assertEqual("order-abc", result["orderId"])
        self.assertEqual("buy", result["side"])
        self.assertEqual("open", result["positionEffect"])
        self.assertEqual(1.50, result["limitPrice"])
        self.assertTrue(result["refId"])

    def test_review_failure_blocks_submission(self):
        """Review failure must be fail-closed — do NOT proceed to place."""
        import v7_live_order_bridge as bridge
        position = {
            "instrumentId": "inst-uuid-123",
            "occSymbol": "SPY260904C00560000",
            "entryAsk": 1.50,
            "symbol": "SPY",
        }

        def failing_dispatcher(tool_name, arguments):
            if "review_option_order" in tool_name:
                raise bridge.OrderBridgeError("mcp_dispatch_failed:review_error")
            raise RuntimeError(f"unexpected_dispatch_after_review_failure:{tool_name}")

        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            with self.assertRaises(bridge.OrderBridgeError) as ctx:
                bridge.submit_entry_order(position, dispatcher=failing_dispatcher)
        self.assertIn("review", str(ctx.exception).lower())

    def test_review_with_blocking_alerts_blocks_submission(self):
        """If review surfaces blocking alerts, do NOT place the order."""
        import v7_live_order_bridge as bridge
        position = {
            "instrumentId": "inst-uuid-123",
            "occSymbol": "SPY260904C00560000",
            "entryAsk": 1.50,
            "symbol": "SPY",
        }
        review_resp = {"data": {"order_checks": [{"state": "blocked", "detail": "Insufficient buying power"}]}}

        def dispatcher(tool_name, arguments):
            if "review_option_order" in tool_name:
                return review_resp
            raise RuntimeError("place_should_not_be_called_after_blocking_review")

        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            with self.assertRaises(bridge.OrderRejected):
                bridge.submit_entry_order(position, dispatcher=dispatcher)

    def test_qqq_uses_broker_chain_symbol_qqq(self):
        import v7_live_order_bridge as bridge
        position = {
            "instrumentId": "qqq-inst-1",
            "occSymbol": "QQQ260904C00500000",
            "entryAsk": 1.50,
            "symbol": "QQQ",
            "positionId": "paper-qqq-1",
        }
        review_resp = {"data": {"order_checks": []}}
        place_resp = _order_response("order-qqq", state="filled")
        calls = []

        def dispatcher(tool_name, arguments):
            calls.append((tool_name, arguments.copy()))
            if "review_option_order" in tool_name:
                return review_resp
            if "place_option_order" in tool_name:
                return place_resp
            raise RuntimeError(f"unexpected_dispatch:{tool_name}")

        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            bridge.submit_entry_order(position, dispatcher=dispatcher)

        review_calls = [args for name, args in calls if "review_option_order" in name]
        place_calls = [args for name, args in calls if "place_option_order" in name]
        self.assertEqual(1, len(review_calls))
        self.assertEqual("QQQ", review_calls[0]["chain_symbol"])
        self.assertEqual("equity", review_calls[0]["underlying_type"])
        self.assertEqual(1, len(place_calls))
        self.assertNotIn("chain_symbol", place_calls[0])
        self.assertNotIn("underlying_type", place_calls[0])


    def test_missing_instrument_id_raises(self):
        import v7_live_order_bridge as bridge
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            with self.assertRaises(bridge.OrderBridgeError):
                bridge.submit_entry_order(
                    {"occSymbol": "SPY123", "entryAsk": 1.50, "symbol": "SPY"},
                    dispatcher=lambda *a: None,
                )

    def test_occ_symbol_ticker_mismatch_blocks_entry(self):
        import v7_live_order_bridge as bridge
        position = {
            "positionId": "paper-occ-mismatch",
            "instrumentId": "inst-occ-mismatch",
            "occSymbol": "QQQ260904C00500000",
            "symbol": "SPY",
            "entryAsk": 1.50,
        }
        dispatcher = _make_mock_dispatcher({
            "review_option_order": [{"data": {"order_checks": []}}],
            "place_option_order": [_order_response("must-not-place")],
        })
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            with self.assertRaises(bridge.OrderBridgeError):
                bridge.submit_entry_order(position, dispatcher=dispatcher)

    def test_invalid_ask_raises(self):
        import v7_live_order_bridge as bridge
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            with self.assertRaises(bridge.OrderBridgeError):
                bridge.submit_entry_order(
                    {"instrumentId": "x", "occSymbol": "SPY", "entryAsk": 0, "symbol": "SPY"},
                    dispatcher=lambda *a: None,
                )


class ExitOrderTests(unittest.TestCase):
    def setUp(self):
        import v7_live_order_bridge as bridge
        self.auth_patch = patch.object(bridge, "live_authorized", return_value=True)
        self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.tmpdir = tempfile.mkdtemp()
        self.ks_path = Path(self.tmpdir) / "v7_kill_switch.json"
        self.ks_path.write_text(json.dumps({"active": False, "reason": "test"}))

    def test_force_close_reviews_before_protective_place(self):
        import v7_live_order_bridge as bridge
        calls = []

        def dispatcher(tool_name, arguments):
            method = tool_name.replace("mcp__robinhood_trading__", "")
            calls.append(method)
            if method == "review_option_order":
                return {"data": {"order_checks": []}}
            if method == "place_option_order":
                return _order_response("emergency-order", state="queued")
            raise AssertionError(f"unexpected_method:{method}")

        position = {"instrumentId": "instrument-1", "symbol": "SPY"}
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            result = bridge.force_close_position(position, dispatcher=dispatcher)
        self.assertEqual("emergency-order", result["orderId"])
        self.assertEqual(["review_option_order", "place_option_order"], calls)

    def test_successful_exit_submission(self):
        import v7_live_order_bridge as bridge
        position = {
            "instrumentId": "inst-uuid-456",
            "occSymbol": "QQQ260904C00500000",
            "symbol": "QQQ",
            "positionId": "paper-20260903T100000",
        }
        review_resp = {"data": {"order_checks": []}}
        place_resp = _order_response("order-xyz", state="filled", fill_price="1.20")

        dispatcher = _make_mock_dispatcher({
            "review_option_order": [review_resp],
            "place_option_order": [place_resp],
        })

        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            result = bridge.submit_exit_order(position, exit_bid=1.20, dispatcher=dispatcher)

        self.assertEqual("order-xyz", result["orderId"])
        self.assertEqual("sell", result["side"])
        self.assertEqual("close", result["positionEffect"])
        self.assertEqual(1.20, result["limitPrice"])

    def test_exit_review_failure_blocks_submission(self):
        """Exit review failure must be fail-closed — do NOT proceed to place."""
        import v7_live_order_bridge as bridge
        position = {
            "instrumentId": "inst-uuid-456",
            "occSymbol": "QQQ260904C00500000",
            "symbol": "QQQ",
            "positionId": "paper-20260903T100000",
        }

        def failing_dispatcher(tool_name, arguments):
            if "review_option_order" in tool_name:
                raise bridge.OrderBridgeError("mcp_dispatch_failed:review_error")
            raise RuntimeError(f"unexpected_dispatch_after_review_failure:{tool_name}")

        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            with self.assertRaises(bridge.OrderBridgeError) as ctx:
                bridge.submit_exit_order(position, exit_bid=1.20, dispatcher=failing_dispatcher)
        self.assertIn("review", str(ctx.exception).lower())

    def test_exit_review_blocking_alerts_blocks_submission(self):
        """If exit review surfaces blocking alerts, do NOT place the order."""
        import v7_live_order_bridge as bridge
        position = {
            "instrumentId": "inst-uuid-456",
            "occSymbol": "QQQ260904C00500000",
            "symbol": "QQQ",
            "positionId": "paper-20260903T100000",
        }
        review_resp = {"data": {"order_checks": [{"state": "blocked", "detail": "Position already closed"}]}}

        def dispatcher(tool_name, arguments):
            if "review_option_order" in tool_name:
                return review_resp
            raise RuntimeError("place_should_not_be_called_after_blocking_review")

        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            with self.assertRaises(bridge.OrderRejected):
                bridge.submit_exit_order(position, exit_bid=1.20, dispatcher=dispatcher)

    def test_exit_missing_instrument_id_raises(self):
        import v7_live_order_bridge as bridge
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            with self.assertRaises(bridge.OrderBridgeError):
                bridge.submit_exit_order({"occSymbol": "QQQ", "symbol": "QQQ"}, exit_bid=1.20, dispatcher=lambda *a: None)

    def test_exit_invalid_bid_raises(self):
        import v7_live_order_bridge as bridge
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            with self.assertRaises(bridge.OrderBridgeError):
                bridge.submit_exit_order({"instrumentId": "x", "occSymbol": "QQQ", "symbol": "QQQ"}, exit_bid=0, dispatcher=lambda *a: None)


class OrderStatusTests(unittest.TestCase):
    def test_verify_filled_order(self):
        import v7_live_order_bridge as bridge
        resp = _order_response("order-123", state="filled", fill_price="1.45")
        dispatcher = _make_mock_dispatcher({"get_option_orders": [resp]})
        status = bridge.verify_order_status("order-123", dispatcher=dispatcher)
        self.assertEqual("filled", status["state"])
        self.assertEqual(1.45, status["fillPrice"])
        self.assertEqual(1, status["filledQuantity"])

    def test_verify_rejected_order(self):
        import v7_live_order_bridge as bridge
        resp = {"data": {"orders": [{"id": "order-rej", "state": "rejected", "reject_reason": "insufficient_bp"}]}}
        dispatcher = _make_mock_dispatcher({"get_option_orders": [resp]})
        status = bridge.verify_order_status("order-rej", dispatcher=dispatcher)
        self.assertEqual("rejected", status["state"])
        self.assertEqual("insufficient_bp", status["rejectReason"])

    def test_verify_rejects_returned_order_id_mismatch(self):
        import v7_live_order_bridge as bridge
        resp = _order_response("different-order", state="filled")
        dispatcher = _make_mock_dispatcher({"get_option_orders": [resp]})
        with self.assertRaises(bridge.OrderLifecycleHalted):
            bridge.verify_order_status("requested-order", dispatcher=dispatcher)

    def test_verify_robinhood_execution_shape(self):
        import v7_live_order_bridge as bridge
        resp = {"data": {"orders": [{
            "id": "order-rh",
            "state": "filled",
            "quantity": "4.00000",
            "processed_quantity": "4.00000",
            "legs": [{
                "option_id": "test-instrument-uuid",
                "executions": [{"price": "1.16", "quantity": "4.00000"}],
            }],
        }]}}
        dispatcher = _make_mock_dispatcher({"get_option_orders": [resp]})
        status = bridge.verify_order_status("order-rh", dispatcher=dispatcher)
        self.assertEqual("filled", status["state"])
        self.assertEqual(1.16, status["fillPrice"])
        self.assertEqual(4, status["filledQuantity"])

    def test_wait_for_fill_succeeds(self):
        import v7_live_order_bridge as bridge
        queued = _order_response("order-1", state="queued")
        filled = _order_response("order-1", state="filled", fill_price="1.50")
        dispatcher = _make_mock_dispatcher({"get_option_orders": [queued, filled]})
        status = bridge.wait_for_fill("order-1", timeout_seconds=5, poll_interval=0.01, dispatcher=dispatcher)
        self.assertEqual("filled", status["state"])

    def test_wait_for_fill_partial_fill_halts_without_auto_cancel(self):
        import v7_live_order_bridge as bridge
        partial = _order_response("order-partial", state="partially_filled")
        dispatcher = _make_mock_dispatcher({"get_option_orders": [partial]})
        with self.assertRaises(bridge.OrderLifecycleHalted):
            bridge.wait_for_fill("order-partial", timeout_seconds=0.05, poll_interval=0.01, dispatcher=dispatcher)

    def test_wait_for_fill_unknown_state_halts_without_auto_cancel(self):
        import v7_live_order_bridge as bridge
        unknown = _order_response("order-unknown", state="broker_added_state")
        dispatcher = _make_mock_dispatcher({"get_option_orders": [unknown]})
        with self.assertRaises(bridge.OrderLifecycleHalted):
            bridge.wait_for_fill("order-unknown", timeout_seconds=0.05, poll_interval=0.01, dispatcher=dispatcher)

    def test_wait_for_fill_rejection_raises(self):
        import v7_live_order_bridge as bridge
        rejected = {"data": {"orders": [{"id": "order-2", "state": "rejected", "reject_reason": "no_bp"}]}}
        dispatcher = _make_mock_dispatcher({"get_option_orders": [rejected]})
        with self.assertRaises(bridge.OrderRejected):
            bridge.wait_for_fill("order-2", timeout_seconds=5, poll_interval=0.01, dispatcher=dispatcher)

    def test_timeout_with_failed_cancel_halts_lifecycle(self):
        import v7_live_order_bridge as bridge

        cancelled = {"value": False}

        def dispatcher(tool_name, arguments):
            method = tool_name.replace("mcp__robinhood_trading__", "")
            if method == "get_option_orders":
                if cancelled["value"]:
                    return _order_response("order-timeout", state="queued")
                return _order_response("order-timeout", state="queued")
            if method == "cancel_option_order":
                cancelled["value"] = True
                return {"data": {"cancelled": False}}
            raise AssertionError(f"unexpected_method:{method}")

        with self.assertRaises(bridge.OrderLifecycleHalted):
            bridge.wait_for_fill(
                "order-timeout",
                timeout_seconds=0.01,
                poll_interval=0.001,
                dispatcher=dispatcher,
            )

    def test_timeout_auto_cancels_pending_order(self):
        """Timeout must cancel the pending order before raising OrderTimeout."""
        import v7_live_order_bridge as bridge
        from unittest.mock import MagicMock

        queued = _order_response("order-3", state="queued")
        cancel_tracker = MagicMock(return_value={"data": {"cancelled": True}})
        cancel_state = {"requested": False}

        def dispatcher(tool_name, arguments):
            if "get_option_orders" in tool_name:
                if cancel_state["requested"]:
                    return {
                        "data": {"orders": [{
                            "id": "order-3",
                            "state": "cancelled",
                            "legs": [{"fill_quantity": "0"}],
                        }]}
                    }
                return queued
            if "cancel_option_order" in tool_name:
                cancel_state["requested"] = True
                return cancel_tracker(tool_name, arguments)
            raise RuntimeError(f"unexpected_dispatch:{tool_name}")

        with self.assertRaises(bridge.OrderTimeout):
            bridge.wait_for_fill("order-3", timeout_seconds=0.1, poll_interval=0.01, dispatcher=dispatcher)

        # Verify cancel was actually called
        cancel_tracker.assert_called_once()


class ReconciliationTests(unittest.TestCase):
    def test_reconciliation_rejects_missing_identity_fields(self):
        import v7_live_order_bridge as bridge
        broker_resp = {"data": {"results": [{
            "option_id": "inst-A", "quantity": "1",
        }]}}
        dispatcher = _make_mock_dispatcher({"get_option_positions": [broker_resp]})
        local = [{"instrumentId": "inst-A", "quantity": 1, "side": "long", "expiry": "2026-09-04"}]
        result = bridge.reconcile_positions(local, dispatcher=dispatcher)
        self.assertFalse(result["healthy"])
        self.assertIn("inst-A", result["mismatched"])

    def test_reconciliation_rejects_duplicate_broker_rows(self):
        import v7_live_order_bridge as bridge
        broker_resp = {"data": {"results": [
            {"option_id": "inst-A", "quantity": "1", "type": "long", "expiration_date": "2026-09-04"},
            {"option_id": "inst-A", "quantity": "1", "type": "long", "expiration_date": "2026-09-04"},
        ]}}
        dispatcher = _make_mock_dispatcher({"get_option_positions": [broker_resp]})
        local = [{"instrumentId": "inst-A", "quantity": 1, "side": "long", "expiry": "2026-09-04"}]
        result = bridge.reconcile_positions(local, dispatcher=dispatcher)
        self.assertFalse(result["healthy"])
        self.assertIn("inst-A", result["mismatched"])

    def test_reconciliation_matches_instrument_and_quantity(self):
        import v7_live_order_bridge as bridge
        broker_resp = {
            "data": {
                "results": [
                    {"option_id": "inst-A", "quantity": "1", "average_price": "1.50", "chain_symbol": "SPY",
                     "type": "long", "expiration_date": "2026-09-04"},
                    {"option_id": "inst-B", "quantity": "2", "average_price": "0.80", "chain_symbol": "QQQ",
                     "type": "long", "expiration_date": "2026-09-04"},
                ]
            }
        }
        dispatcher = _make_mock_dispatcher({"get_option_positions": [broker_resp]})

        local = [{"instrumentId": "inst-A", "quantity": 1, "side": "long", "expiry": "2026-09-04"}]
        result = bridge.reconcile_positions(local, dispatcher=dispatcher)

        self.assertIn("inst-A", result["matched"])
        self.assertIn("inst-B", result["orphanedBroker"])
        self.assertEqual(2, result["brokerPositionCount"])
        self.assertEqual(1, result["localPositionCount"])

    def test_reconciliation_no_broker_positions(self):
        import v7_live_order_bridge as bridge
        dispatcher = _make_mock_dispatcher({"get_option_positions": [{"data": {"results": []}}]})
        local = [{"instrumentId": "inst-A", "quantity": 1, "side": "long", "expiry": "2026-09-04"}]
        result = bridge.reconcile_positions(local, dispatcher=dispatcher)
        self.assertIn("inst-A", result["orphanedLocal"])
        self.assertEqual(0, result["brokerPositionCount"])

    def test_reconciliation_does_not_match_wrong_quantity_side_or_expiry(self):
        import v7_live_order_bridge as bridge
        broker_resp = {"data": {"results": [{
            "option_id": "inst-A", "quantity": "2", "average_price": "1.50",
            "chain_symbol": "SPY", "type": "short", "expiration_date": "2026-09-05",
        }]}}
        dispatcher = _make_mock_dispatcher({"get_option_positions": [broker_resp]})
        local = [{"instrumentId": "inst-A", "quantity": 1, "side": "long", "expiry": "2026-09-04"}]
        result = bridge.reconcile_positions(local, dispatcher=dispatcher)
        self.assertEqual([], result["matched"])
        self.assertIn("inst-A", result["mismatched"])


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ks_path = Path(self.tmpdir) / "v7_kill_switch.json"
        self.ks_path.write_text(json.dumps({"active": False, "reason": "test"}))

    def test_all_pass(self):
        import v7_live_order_bridge as bridge
        accounts_resp = {
            "data": {"accounts": [
                {"account_number": "801336223", "agentic_allowed": True, "option_level": "option_level_2"},
            ]}
        }
        portfolio_resp = {
            "data": {"buying_power": {"buying_power": "504.80"}}
        }
        positions_resp = {"data": {"results": []}}
        dispatcher = _make_mock_dispatcher({
            "get_accounts": [accounts_resp],
            "get_portfolio": [portfolio_resp],
            "get_option_positions": [positions_resp],
        })
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            result = bridge.pre_live_readiness(dispatcher=dispatcher)
        self.assertTrue(result["ready"])
        self.assertEqual(6, len(result["checks"]))

    def test_readiness_uses_account_returned_by_broker(self):
        import v7_live_order_bridge as bridge
        accounts_resp = {"data": {"accounts": [
            {"account_number": "different-account", "agentic_allowed": True, "option_level": "option_level_2"},
        ]}}
        dispatcher = _make_mock_dispatcher({"get_accounts": [accounts_resp]})

        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            result = bridge.pre_live_readiness(dispatcher=dispatcher)
        self.assertFalse(result["ready"])
        self.assertIsNone(result["accountNumber"])

    def test_kill_switch_fails_readiness(self):
        import v7_live_order_bridge as bridge
        self.ks_path.write_text(json.dumps({"active": True}))
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            result = bridge.pre_live_readiness(dispatcher=lambda *a: {"data": {}})
        self.assertFalse(result["ready"])

    def test_insufficient_buying_power_fails(self):
        import v7_live_order_bridge as bridge
        accounts_resp = {
            "data": {"accounts": [
                {"account_number": "801336223", "agentic_allowed": True, "option_level": "option_level_2"},
            ]}
        }
        portfolio_resp = {"data": {"buying_power": {"buying_power": "50.00"}}}
        positions_resp = {"data": {"results": []}}
        dispatcher = _make_mock_dispatcher({
            "get_accounts": [accounts_resp],
            "get_portfolio": [portfolio_resp],
            "get_option_positions": [positions_resp],
        })
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            result = bridge.pre_live_readiness(dispatcher=dispatcher)
        self.assertFalse(result["ready"])

    def test_open_broker_positions_fail(self):
        import v7_live_order_bridge as bridge
        accounts_resp = {
            "data": {"accounts": [
                {"account_number": "801336223", "agentic_allowed": True, "option_level": "option_level_2"},
            ]}
        }
        portfolio_resp = {"data": {"buying_power": {"buying_power": "504.80"}}}
        positions_resp = {"data": {"results": [{"option_id": "inst-X", "quantity": "1"}]}}
        dispatcher = _make_mock_dispatcher({
            "get_accounts": [accounts_resp],
            "get_portfolio": [portfolio_resp],
            "get_option_positions": [positions_resp],
        })
        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            result = bridge.pre_live_readiness(dispatcher=dispatcher)
        self.assertFalse(result["ready"])


class StateSafetyTests(unittest.TestCase):
    def test_corrupt_live_state_fails_closed(self):
        import v7_live_order_bridge as bridge
        state_path = Path(tempfile.mkdtemp()) / "v7_live_order_state.json"
        state_path.write_text("{broken", encoding="utf-8")
        with patch.object(bridge, "LIVE_STATE_PATH", state_path):
            with self.assertRaises(bridge.OrderBridgeError):
                bridge.load_live_state()


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        import v7_live_order_bridge as bridge
        self.auth_patch = patch.object(bridge, "live_authorized", return_value=True)
        self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.tmpdir = tempfile.mkdtemp()
        self.ks_path = Path(self.tmpdir) / "v7_kill_switch.json"
        self.ks_path.write_text(json.dumps({"active": False, "reason": "test"}))

    def test_uuid_ref_id_is_distinct_per_logical_position(self):
        import uuid
        import v7_live_order_bridge as bridge
        position_one = {
            "instrumentId": "inst-1", "occSymbol": "SPY", "entryAsk": 1.50,
            "symbol": "SPY", "positionId": "paper-one",
        }
        position_two = {
            "instrumentId": "inst-2", "occSymbol": "SPY", "entryAsk": 1.50,
            "symbol": "SPY", "positionId": "paper-two",
        }
        review_resp = {"data": {"order_checks": []}}
        place_resp = _order_response("order-a", state="filled")

        dispatcher = _make_mock_dispatcher({
            "review_option_order": [review_resp, review_resp],
            "place_option_order": [place_resp, {**place_resp, "data": {"orders": [{"id": "order-b", "state": "filled"}]} }],
        })

        with patch.object(bridge, "KILL_SWITCH_PATH", self.ks_path):
            r1 = bridge.submit_entry_order(position_one, dispatcher=dispatcher)
            r2 = bridge.submit_entry_order(position_two, dispatcher=dispatcher)

        uuid.UUID(r1["refId"])
        uuid.UUID(r2["refId"])
        self.assertNotEqual(r1["refId"], r2["refId"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
