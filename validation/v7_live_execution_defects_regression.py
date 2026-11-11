#!/usr/bin/env python3
"""Regression tests for V7 live-execution defects found in the Astra audit."""
from __future__ import annotations

import importlib
import json
import math
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def order_response(order_id: str = "order-1"):
    return {
        "data": {
            "orders": [{
                "id": order_id,
                "state": "filled",
                "legs": [{
                    "option_id": "inst-1",
                    "side": "sell",
                    "position_effect": "close",
                    "fill_price": "1.20",
                    "fill_quantity": "1",
                }],
            }]
        }
    }


def dispatcher_for_exit(calls):
    def dispatch(tool_name, arguments):
        method = tool_name.rsplit("__", 1)[-1]
        calls.append((method, arguments))
        if method == "review_option_order":
            return {"data": {"order_checks": []}}
        if method == "place_option_order":
            return order_response()
        raise AssertionError(f"unexpected method: {method}")
    return dispatch


class ProtectiveExitTests(unittest.TestCase):
    def test_protective_exit_works_when_auth_lapses_and_kill_switch_is_active(self):
        bridge = importlib.import_module("v7_live_order_bridge")
        position = {
            "positionId": "live-1",
            "instrumentId": "inst-1",
            "occSymbol": "SPY260904C00560000",
            "symbol": "SPY",
        }
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            kill_path = Path(tmp) / "v7_kill_switch.json"
            kill_path.write_text(json.dumps({"active": True}))
            with patch.object(bridge, "KILL_SWITCH_PATH", kill_path), \
                 patch.object(bridge, "live_authorized", return_value=False):
                result = bridge.submit_exit_order(
                    position,
                    1.20,
                    dispatcher=dispatcher_for_exit(calls),
                    ref_id="exit-ref-1",
                    protective=True,
                )
        self.assertEqual("order-1", result["orderId"])
        self.assertEqual(["review_option_order", "place_option_order"], [m for m, _ in calls])

    def test_raw_nonprotective_placement_still_requires_auth_and_kill_switch(self):
        bridge = importlib.import_module("v7_live_order_bridge")
        with tempfile.TemporaryDirectory() as tmp:
            kill_path = Path(tmp) / "missing-kill-switch.json"
            with patch.object(bridge, "KILL_SWITCH_PATH", kill_path), \
                 patch.object(bridge, "live_authorized", return_value=True):
                with self.assertRaises(bridge.KillSwitchActive):
                    bridge.mcp_call(
                        "place_option_order",
                        {"legs": [{"side": "buy", "position_effect": "open"}]},
                        dispatcher=lambda *_: self.fail("dispatcher called"),
                    )

    def test_non_object_kill_switch_state_fails_closed(self):
        bridge = importlib.import_module("v7_live_order_bridge")
        with tempfile.TemporaryDirectory() as tmp:
            kill_path = Path(tmp) / "v7_kill_switch.json"
            kill_path.write_text("[]")
            with patch.object(bridge, "KILL_SWITCH_PATH", kill_path):
                self.assertTrue(bridge.kill_switch_active())


class PaperKillSwitchTests(unittest.TestCase):
    def _modules_and_path(self):
        paper = importlib.import_module("v7_scalp_paper")
        preflight = importlib.import_module("v7_preflight_check")
        tmpdir = tempfile.TemporaryDirectory()
        path = Path(tmpdir.name) / "v7_kill_switch.json"
        return paper, preflight, tmpdir, path

    def test_explicit_inactive_switch_allows_paper_and_preflight(self):
        paper, preflight, tmpdir, path = self._modules_and_path()
        self.addCleanup(tmpdir.cleanup)
        path.write_text(json.dumps({"active": False}))
        with patch.object(paper, "KILL_SWITCH", path), \
             patch.object(preflight, "STATE_DIR", path.parent):
            self.assertFalse(paper.kill_switch_active())
            self.assertFalse(preflight.kill_switch_active())

    def test_active_or_malformed_switch_blocks_paper_and_preflight(self):
        paper, preflight, tmpdir, path = self._modules_and_path()
        self.addCleanup(tmpdir.cleanup)
        with patch.object(paper, "KILL_SWITCH", path), \
             patch.object(preflight, "STATE_DIR", path.parent):
            for payload in ({"active": True}, {"active": "false"}, []):
                path.write_text(json.dumps(payload))
                self.assertTrue(paper.kill_switch_active())
                self.assertTrue(preflight.kill_switch_active())
            path.write_text("{malformed")
            self.assertTrue(paper.kill_switch_active())
            self.assertTrue(preflight.kill_switch_active())


class RecoveryTests(unittest.TestCase):
    def test_corrupt_execution_state_raises_instead_of_resetting_ownership(self):
        daemon = importlib.import_module("v7_live_execution_daemon")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not-json")
            with patch.object(daemon, "EXECUTION_STATE_PATH", path):
                with self.assertRaises(daemon.ExecutionStateCorrupt):
                    daemon._load_state()

    def test_null_reconciliation_flag_normalizes_to_false(self):
        daemon = importlib.import_module("v7_live_execution_daemon")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({
                "processedEntries": [],
                "processedExits": [],
                "livePositions": {},
                "pendingEntries": {},
                "retryBackoff": {},
                "reconciliationHealthy": None,
            }))
            with patch.object(daemon, "EXECUTION_STATE_PATH", path):
                state = daemon._load_state()
        self.assertIs(state["reconciliationHealthy"], False)

    def test_order_fill_price_accepts_robinhood_execution_shape(self):
        daemon = importlib.import_module("v7_live_execution_daemon")
        order = {
            "state": "filled",
            "processed_quantity": "1.00000",
            "legs": [{
                "executions": [{"price": "1.16", "quantity": "1.00000"}],
            }],
        }
        self.assertEqual(1.16, daemon._order_fill_price(order))

    def test_restart_does_not_mark_missed_exit_for_known_live_position_processed(self):
        daemon = importlib.import_module("v7_live_execution_daemon")
        position_id = "live-1"
        state = {
            "processedEntries": [],
            "processedExits": [],
            "livePositions": {position_id: {"instrumentId": "inst-1"}},
            "pendingEntries": {},
            "retryBackoff": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "v7_scalp_trades_SPY.jsonl").write_text(json.dumps({
                "event": "PAPER_EXIT_REAL_BID",
                "positionId": position_id,
            }) + "\n")
            with patch.object(daemon, "STATE_DIR", state_dir):
                daemon._mark_existing_as_processed(state)
        self.assertNotIn(position_id, state["processedExits"])

    def test_restart_does_not_mark_exit_for_pending_entry_processed(self):
        daemon = importlib.import_module("v7_live_execution_daemon")
        position_id = "pending-1"
        state = {
            "processedEntries": [],
            "processedExits": [],
            "livePositions": {},
            "pendingEntries": {position_id: {"instrumentId": "inst-1"}},
            "retryBackoff": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "v7_scalp_trades_SPY.jsonl").write_text(json.dumps({
                "event": "PAPER_EXIT_REAL_BID",
                "positionId": position_id,
            }) + "\n")
            with patch.object(daemon, "STATE_DIR", state_dir):
                daemon._mark_existing_as_processed(state)
        self.assertNotIn(position_id, state["processedExits"])

    def test_unknown_exit_is_not_fenced_during_unhealthy_authorized_startup(self):
        daemon = importlib.import_module("v7_live_execution_daemon")
        state = {
            "processedEntries": [],
            "processedExits": [],
            "livePositions": {},
            "pendingEntries": {},
            "retryBackoff": {},
            "authorized": True,
            "reconciliationHealthy": False,
        }
        daemon._process_exit(
            {"event": "PAPER_EXIT_REAL_BID", "positionId": "orphan-1", "exitBid": 1.2},
            state,
            {},
        )
        self.assertNotIn("orphan-1", state["processedExits"])

    def test_recovered_partial_entry_halts_executor(self):
        daemon = importlib.import_module("v7_live_execution_daemon")
        identity = {"engineVersion": "current-engine"}
        position_id = "recovered-partial-entry"
        state = {
            "processedEntries": [],
            "processedExits": [],
            "livePositions": {},
            "pendingEntries": {},
            "retryBackoff": {},
        }
        event = {
            "event": "PAPER_ENTRY_REAL_ASK",
            "positionId": position_id,
            "symbol": "SPY",
            "occSymbol": "SPY260904C00560000",
            "instrumentId": "inst-1",
            "entryAsk": 1.50,
            **identity,
        }
        with patch.object(daemon, "_check_existing_order", return_value={
            "id": "partial-order",
            "state": "partially_filled",
        }), patch.object(daemon, "_save_state"), patch.object(daemon, "_deliver_alert"), \
                patch.object(daemon, "_kill_switch_active", return_value=False):
            daemon._process_entry(event, state, identity)
        self.assertTrue(state.get("executionHalted"))
        self.assertFalse(state.get("reconciliationHealthy"))
        self.assertEqual("partial-order", state["pendingEntries"][position_id]["orderId"])

    def test_recovered_partial_exit_halts_executor_and_keeps_position(self):
        daemon = importlib.import_module("v7_live_execution_daemon")
        position_id = "recovered-partial-exit"
        identity = {"engineVersion": "current-engine"}
        state = {
            "processedEntries": [position_id],
            "processedExits": [],
            "livePositions": {
                position_id: {
                    "positionId": position_id,
                    "instrumentId": "inst-1",
                    "occSymbol": "SPY260904C00560000",
                    "symbol": "SPY",
                },
            },
            "pendingEntries": {},
            "retryBackoff": {},
        }
        event = {
            "event": "PAPER_EXIT_REAL_BID",
            "positionId": position_id,
            "symbol": "SPY",
            "occSymbol": "SPY260904C00560000",
            "exitBid": 1.20,
            **identity,
        }
        with patch.object(daemon, "_check_existing_order", return_value={
            "id": "partial-exit-order",
            "state": "partially_filled",
        }), patch.object(daemon, "_save_state"), patch.object(daemon, "_deliver_alert"):
            daemon._process_exit(event, state, identity)
        self.assertTrue(state.get("executionHalted"))
        self.assertFalse(state.get("reconciliationHealthy"))
        self.assertIn(position_id, state["livePositions"])
        self.assertEqual(
            "partial-exit-order",
            state["livePositions"][position_id]["pendingExitOrderId"],
        )

    def test_zero_bid_exit_remains_retryable(self):
        daemon = importlib.import_module("v7_live_execution_daemon")
        state = {
            "processedEntries": [],
            "processedExits": [],
            "livePositions": {"live-1": {"instrumentId": "inst-1", "occSymbol": "SPY260904C00560000", "symbol": "SPY"}},
            "pendingEntries": {},
            "retryBackoff": {},
            "authorized": False,
            "reconciliationHealthy": False,
        }
        event = {
            "event": "PAPER_EXIT_REAL_BID",
            "positionId": "live-1",
            "exitBid": 0,
            "engineVersion": "v",
        }
        daemon._process_exit(event, state, {"engineVersion": "v"})
        self.assertNotIn("live-1", state["processedExits"])

    def test_filled_close_keeps_local_ownership_on_reconciliation_mismatch(self):
        daemon = importlib.import_module("v7_live_execution_daemon")
        bridge = importlib.import_module("v7_live_order_bridge")
        state = {
            "processedEntries": [],
            "processedExits": [],
            "livePositions": {"live-1": {
                "instrumentId": "inst-1",
                "occSymbol": "SPY260904C00560000",
                "symbol": "SPY",
            }},
            "pendingEntries": {},
            "retryBackoff": {},
            "authorized": False,
            "reconciliationHealthy": False,
        }
        event = {
            "event": "PAPER_EXIT_REAL_BID",
            "positionId": "live-1",
            "exitBid": 1.2,
            "engineVersion": "v",
        }
        with patch.object(daemon, "_check_existing_order", return_value={"state": "filled", "id": "order-1"}), \
             patch.object(bridge, "reconcile_positions", return_value={
                 "healthy": False,
                 "orphanedBroker": ["inst-1"],
                 "mismatched": [],
             }), \
             patch.object(daemon, "_save_state"), \
             patch.object(daemon, "_deliver_alert"):
            daemon._process_exit(event, state, {"engineVersion": "v"})
        self.assertIn("live-1", state["livePositions"])
        self.assertNotIn("live-1", state["processedExits"])
        self.assertTrue(state.get("executionHalted"))
        self.assertFalse(state.get("reconciliationHealthy"))


class OrderReceiptDurabilityTests(unittest.TestCase):
    def test_entry_receipt_is_persisted_before_fill_status_lookup(self):
        daemon = importlib.import_module("v7_live_execution_daemon")
        bridge = importlib.import_module("v7_live_order_bridge")
        identity = {
            "engineVersion": "current-engine",
            "strategyId": "current-strategy",
            "releaseManifestSha256": "current-manifest",
            "sourceCodeSha256": "current-source",
            "configSha256": "current-config",
        }
        position_id = "live-entry-receipt-1"
        state = {
            "processedEntries": [],
            "processedExits": [],
            "livePositions": {},
            "pendingEntries": {},
            "retryBackoff": {},
        }
        event = {
            "event": "PAPER_ENTRY_REAL_ASK",
            "positionId": position_id,
            "symbol": "SPY",
            "occSymbol": "SPY260904C00560000",
            "instrumentId": "inst-1",
            "entryAsk": 1.50,
            "entryTime": "2026-09-17T09:40:00-04:00",
            **identity,
        }

        def wait_for_fill(order_id, **_kwargs):
            self.assertEqual("order-1", order_id)
            self.assertEqual("order-1", state["pendingEntries"][position_id]["orderId"])
            return {"fillPrice": 1.50, "filledQuantity": 1}

        with patch.object(daemon, "_check_existing_order", return_value=None), \
             patch.object(daemon, "_kill_switch_active", return_value=False), \
             patch.object(bridge, "submit_entry_order", return_value={"orderId": "order-1"}), \
             patch.object(bridge, "wait_for_fill", side_effect=wait_for_fill), \
             patch.object(daemon, "_deliver_alert"):
            daemon._process_entry(event, state, identity)

        self.assertIn(position_id, state["processedEntries"])
        self.assertNotIn(position_id, state["pendingEntries"])

    def test_partial_entry_fill_halts_executor_and_keeps_receipt(self):
        daemon = importlib.import_module("v7_live_execution_daemon")
        bridge = importlib.import_module("v7_live_order_bridge")
        identity = {
            "engineVersion": "current-engine",
            "strategyId": "current-strategy",
            "releaseManifestSha256": "current-manifest",
            "sourceCodeSha256": "current-source",
            "configSha256": "current-config",
        }
        position_id = "live-entry-partial-1"
        state = {
            "processedEntries": [],
            "processedExits": [],
            "livePositions": {},
            "pendingEntries": {},
            "retryBackoff": {},
        }
        event = {
            "event": "PAPER_ENTRY_REAL_ASK",
            "positionId": position_id,
            "symbol": "SPY",
            "occSymbol": "SPY260904C00560000",
            "instrumentId": "inst-1",
            "entryAsk": 1.50,
            **identity,
        }
        halt = bridge.OrderLifecycleHalted("partial_fill_requires_reconciliation")
        with patch.object(daemon, "_check_existing_order", return_value=None), \
             patch.object(daemon, "_kill_switch_active", return_value=False), \
             patch.object(bridge, "submit_entry_order", return_value={"orderId": "partial-order"}), \
             patch.object(bridge, "wait_for_fill", side_effect=halt), \
             patch.object(daemon, "_save_state"), \
             patch.object(daemon, "_deliver_alert"):
            daemon._process_entry(event, state, identity)

        self.assertTrue(state["executionHalted"])
        self.assertFalse(state["reconciliationHealthy"])
        self.assertEqual("partial-order", state["pendingEntries"][position_id]["orderId"])
        self.assertNotIn(position_id, state["livePositions"])

    def test_old_release_position_remains_monitorable_for_close_only_recovery(self):
        daemon = importlib.import_module("v7_live_execution_daemon")
        bridge = importlib.import_module("v7_live_order_bridge")
        engine = importlib.import_module("v7_scalp_paper")
        position = {
            "positionId": "old-release-live-position",
            "instrumentId": "inst-1",
            "occSymbol": "SPY260904C00560000",
            "symbol": "SPY",
            "fillPrice": 1.00,
            "filledQuantity": 1,
            "filledAt": "2026-09-17T09:40:00-04:00",
            "engineVersion": "old-release",
            "strategyId": "old-strategy",
        }
        quote = {
            "bid": 1.20,
            "ask": 1.25,
            "timestamp": "2026-09-17T13:45:00Z",
        }
        with patch.object(bridge, "get_option_quote_by_instrument", return_value=quote) as get_quote, \
             patch.object(engine, "validate_exit_quote", return_value=(1.20, "ok")), \
             patch.object(engine, "quote_freshness_seconds", return_value=1.0), \
             patch.object(engine, "price_exit_reason", return_value=None):
            daemon.monitor_live_position(
                {"livePositions": {position["positionId"]: position}},
                position,
                {"engineVersion": "current-release"},
                now=datetime.now(daemon.ET),
            )
        get_quote.assert_called_once_with("inst-1", "SPY260904C00560000")

    def test_pending_entry_recovery_uses_exact_id_and_validates_fill(self):
        daemon = importlib.import_module("v7_live_execution_daemon")
        position_id = "pending-exact-entry"
        state = {
            "processedEntries": [],
            "processedExits": [],
            "livePositions": {},
            "pendingEntries": {
                position_id: {
                    "positionId": position_id,
                    "instrumentId": "inst-1",
                    "symbol": "SPY",
                    "occSymbol": "SPY260904C00560000",
                    "orderId": "exact-order-1",
                },
            },
            "retryBackoff": {},
        }
        existing = {
            "id": "exact-order-1",
            "state": "filled",
            "processed_quantity": "1",
            "average_price": "1.55",
        }
        with patch.object(daemon, "_check_existing_order", return_value=existing) as lookup, \
             patch.object(daemon, "_save_state"):
            daemon._recover_pending_entry(state, position_id)
        ref_id = daemon._bridge_ref_id(position_id, "entry")
        lookup.assert_called_once_with(ref_id, "exact-order-1")
        self.assertEqual(1, state["livePositions"][position_id]["filledQuantity"])
        self.assertEqual(1.55, state["livePositions"][position_id]["fillPrice"])
        self.assertNotIn(position_id, state["pendingEntries"])

    def test_exit_receipt_is_persisted_before_fill_status_lookup(self):
        daemon = importlib.import_module("v7_live_execution_daemon")
        bridge = importlib.import_module("v7_live_order_bridge")
        identity = {
            "engineVersion": "current-engine",
            "strategyId": "current-strategy",
            "releaseManifestSha256": "current-manifest",
            "sourceCodeSha256": "current-source",
            "configSha256": "current-config",
        }
        position_id = "live-exit-receipt-1"
        state = {
            "processedEntries": [],
            "processedExits": [],
            "livePositions": {
                position_id: {
                    "positionId": position_id,
                    "instrumentId": "inst-1",
                    "occSymbol": "SPY260904C00560000",
                    "symbol": "SPY",
                },
            },
            "pendingEntries": {},
            "retryBackoff": {},
        }
        event = {
            "event": "PAPER_EXIT_REAL_BID",
            "positionId": position_id,
            "symbol": "SPY",
            "occSymbol": "SPY260904C00560000",
            "entryAsk": 1.50,
            "exitBid": 1.20,
            **identity,
        }

        def wait_for_fill(order_id, **_kwargs):
            self.assertEqual("exit-order-1", order_id)
            self.assertEqual(
                "exit-order-1",
                state["livePositions"][position_id]["pendingExitOrderId"],
            )
            return {"fillPrice": 1.20, "filledQuantity": 1}

        with patch.object(daemon, "_check_existing_order", return_value=None), \
             patch.object(bridge, "submit_exit_order", return_value={"orderId": "exit-order-1"}), \
             patch.object(bridge, "wait_for_fill", side_effect=wait_for_fill), \
             patch.object(daemon, "_reconcile_after_exit", return_value=True), \
             patch.object(daemon, "_deliver_alert"):
            daemon._process_exit(event, state, identity)

        self.assertIn(position_id, state["processedExits"])
        self.assertNotIn(position_id, state["livePositions"])


class ReconciliationTests(unittest.TestCase):
    def test_reconciliation_accepts_account_number_and_reports_health(self):
        bridge = importlib.import_module("v7_live_order_bridge")
        result = bridge.reconcile_positions(
            [],
            account_number=bridge.AGENTIC_ACCOUNT_NUMBER,
            dispatcher=lambda *_: {"data": {"results": []}},
        )
        self.assertTrue(result["healthy"])


class ReadinessTests(unittest.TestCase):
    def _dispatcher(self, accounts, orders):
        bridge = importlib.import_module("v7_live_order_bridge")
        def dispatch(tool_name, arguments):
            method = tool_name.rsplit("__", 1)[-1]
            if method == "get_accounts":
                return {"data": {"accounts": accounts}}
            if method == "get_portfolio":
                return {"data": {"buying_power": {"buying_power": "504.80"}}}
            if method == "get_option_positions":
                return {"data": {"results": []}}
            if method == "get_option_orders":
                return {"data": {"orders": orders}}
            raise AssertionError(f"unexpected method: {method}")
        return dispatch

    def test_readiness_rejects_different_agentic_account(self):
        bridge = importlib.import_module("v7_live_order_bridge")
        accounts = [{
            "account_number": "999999999",
            "agentic_allowed": True,
            "option_level": "option_level_2",
        }]
        result = bridge.pre_live_readiness(dispatcher=self._dispatcher(accounts, []))
        self.assertFalse(result["ready"])
        self.assertFalse(next(c for c in result["checks"] if c["name"] == "agentic_account")["passed"])

    def test_readiness_rejects_unknown_order_state(self):
        bridge = importlib.import_module("v7_live_order_bridge")
        accounts = [{
            "account_number": bridge.AGENTIC_ACCOUNT_NUMBER,
            "agentic_allowed": True,
            "option_level": "option_level_2",
        }]
        result = bridge.pre_live_readiness(
            dispatcher=self._dispatcher(accounts, [{"state": "provider_new_state", "id": "o1"}])
        )
        self.assertFalse(result["ready"])
        self.assertFalse(next(c for c in result["checks"] if c["name"] == "no_open_orders")["passed"])

    def test_readiness_rejects_open_orders(self):
        bridge = importlib.import_module("v7_live_order_bridge")
        accounts = [{
            "account_number": bridge.AGENTIC_ACCOUNT_NUMBER,
            "agentic_allowed": True,
            "option_level": "option_level_2",
        }]
        result = bridge.pre_live_readiness(dispatcher=self._dispatcher(accounts, [{"state": "queued", "id": "o1"}]))
        self.assertFalse(result["ready"])
        self.assertFalse(next(c for c in result["checks"] if c["name"] == "no_open_orders")["passed"])


class EvidenceValidationTests(unittest.TestCase):
    @staticmethod
    def release():
        return {
            "engineVersion": "7.4.21-test",
            "strategyId": "v7421-test",
            "releaseManifestSha256": "manifest-current",
            "sourceCodeSha256": "source-current",
            "configSha256": "config-current",
        }

    def entry(self, pid, release):
        return {
            "event": "PAPER_ENTRY_REAL_ASK",
            "positionId": pid,
            "symbol": "SPY",
            "entryAsk": 1.00,
            **release,
        }

    def exit(self, pid, release, **kwargs):
        return {
            "event": "PAPER_EXIT_REAL_BID",
            "positionId": pid,
            "symbol": "SPY",
            "entryAsk": 1.00,
            "exitBid": 1.20,
            "pnl": 20.0,
            "quoteSource": "Robinhood MCP",
            **release,
            **kwargs,
        }

    def test_nan_pnl_is_excluded(self):
        gate = importlib.import_module("v7_live_gate_progress")
        release = self.release()
        result = gate.summarize_events(
            [self.entry("nan", release), self.exit("nan", release, pnl=math.nan)],
            release,
            {"min_closed_trades": 1, "min_win_rate_pct": 0.0, "require_positive_pnl": True},
        )
        self.assertEqual(0, result["closedTrades"])

    def test_fallback_priced_exit_is_excluded(self):
        gate = importlib.import_module("v7_live_gate_progress")
        release = self.release()
        result = gate.summarize_events(
            [self.entry("fallback", release), self.exit(
                "fallback", release,
                reason="QUOTE_FAILURE_EXIT",
                quoteSource="fallback_last_known_bid",
            )],
            release,
            {"min_closed_trades": 1, "min_win_rate_pct": 0.0, "require_positive_pnl": True},
        )
        self.assertEqual(0, result["closedTrades"])

    def test_pnl_mismatch_is_excluded(self):
        gate = importlib.import_module("v7_live_gate_progress")
        release = self.release()
        result = gate.summarize_events(
            [self.entry("mismatch", release), self.exit("mismatch", release, pnl=999.0)],
            release,
            {"min_closed_trades": 1, "min_win_rate_pct": 0.0, "require_positive_pnl": True},
        )
        self.assertEqual(0, result["closedTrades"])

    def test_exit_copy_of_entry_price_cannot_override_paired_entry(self):
        gate = importlib.import_module("v7_live_gate_progress")
        release = self.release()
        result = gate.summarize_events(
            [self.entry("copied", release), self.exit(
                "copied",
                release,
                entryAsk=2.00,
                exitBid=2.20,
                pnl=20.0,
            )],
            release,
            {"min_closed_trades": 1, "min_win_rate_pct": 0.0, "require_positive_pnl": True},
        )
        self.assertEqual(0, result["closedTrades"])


class AutoActivateSafetyTests(unittest.TestCase):
    def test_auto_activate_entrypoint_is_report_only(self):
        auto = importlib.import_module("v7_live_auto_activate")
        identity = {
            "engineVersion": "7.4.23-evidence-gate-early-reversal",
            "strategyId": "v7423-evidence-gate-early-reversal",
            "manifestSha256": "manifest",
        }
        snapshot = {
            "gateMet": True,
            "enableLiveAutomatically": False,
            "liveOrdersEnabled": False,
            "netPnl": 49.0,
            "closedTrades": 25,
            "exactClosedTrades": 5,
            "wins": 11,
            "winRatePct": 44.0,
            "engineVersion": identity["engineVersion"],
        }
        fake_progress = type("Progress", (), {"build_snapshot": staticmethod(lambda: snapshot)})
        auth_path = Path.home() / ".hermes" / "state" / "v7_live_authorization.json"
        before = auth_path.read_bytes() if auth_path.exists() else None
        with patch.dict(sys.modules, {
            "v7_live_gate_progress": fake_progress,
        }):
            self.assertEqual(0, auto.main())
        after = auth_path.read_bytes() if auth_path.exists() else None
        self.assertEqual(before, after)


class LiveEnableMonitorActivationTests(unittest.TestCase):
    def test_default_monitor_run_never_enables_live(self):
        monitor = importlib.import_module("v7_live_enable_monitor")
        snapshot = {
            "gateMet": True,
            "enableLiveAutomatically": False,
            "liveOrdersEnabled": False,
            "netPnl": 49.0,
            "closedTrades": 25,
            "exactClosedTrades": 5,
            "wins": 11,
        }
        with patch.object(monitor, "check_kill_switch", return_value=False), \
             patch.object(monitor, "check_gate", return_value=snapshot), \
             patch.object(monitor, "check_broker_readiness", return_value=(True, "ok")), \
             patch.object(monitor, "write_authorization") as write_auth, \
             patch.object(monitor, "enable_live_orders") as enable_orders, \
             patch.object(monitor, "resume_auto_activate_cron") as resume_cron:
            self.assertFalse(monitor.attempt_enable())
        write_auth.assert_not_called()
        enable_orders.assert_not_called()
        resume_cron.assert_not_called()


    def test_explicit_confirmation_is_the_only_state_changing_path(self):
        monitor = importlib.import_module("v7_live_enable_monitor")
        snapshot = {
            "gateMet": True,
            "enableLiveAutomatically": False,
            "liveOrdersEnabled": False,
            "netPnl": 49.0,
            "closedTrades": 25,
            "exactClosedTrades": 5,
            "wins": 11,
        }
        with patch.object(monitor, "check_kill_switch", return_value=False), \
             patch.object(monitor, "check_gate", return_value=snapshot), \
             patch.object(monitor, "check_broker_readiness", return_value=(True, "ok")), \
             patch.object(monitor, "write_authorization") as write_auth, \
             patch.object(monitor, "enable_live_orders") as enable_orders, \
             patch.object(monitor, "resume_auto_activate_cron") as resume_cron:
            self.assertTrue(
                monitor.attempt_enable(
                    activate=True,
                    confirmation=monitor.ACTIVATION_CONFIRMATION,
                )
            )
        write_auth.assert_called_once_with(snapshot)
        enable_orders.assert_called_once_with()
        resume_cron.assert_not_called()


    def test_activation_requires_five_exact_current_release_trades(self):
        monitor = importlib.import_module("v7_live_enable_monitor")
        snapshot = {
            "gateMet": True,
            "enableLiveAutomatically": False,
            "liveOrdersEnabled": False,
            "netPnl": 49.0,
            "closedTrades": 28,
            "exactClosedTrades": 4,
            "wins": 13,
        }
        with (
            patch.object(monitor, "check_kill_switch", return_value=False),
            patch.object(monitor, "check_gate", return_value=snapshot),
            patch.object(monitor, "check_broker_readiness", return_value=(True, "ok")),
            patch.object(monitor, "write_authorization") as write_auth,
            patch.object(monitor, "enable_live_orders") as enable_orders,
        ):
            self.assertFalse(
                monitor.attempt_enable(
                    activate=True,
                    confirmation=monitor.ACTIVATION_CONFIRMATION,
                )
            )
        write_auth.assert_not_called()
        enable_orders.assert_not_called()


class LiveEnableMonitorSafetyTests(unittest.TestCase):
    def test_missing_kill_switch_blocks_monitor(self):
        monitor = importlib.import_module("v7_live_enable_monitor")
        with tempfile.TemporaryDirectory() as tmp:
            kill_path = Path(tmp) / "v7_kill_switch.json"
            with patch.object(monitor, "KILL_SWITCH_PATH", kill_path):
                self.assertTrue(monitor.check_kill_switch())


if __name__ == "__main__":
    unittest.main(verbosity=2)
