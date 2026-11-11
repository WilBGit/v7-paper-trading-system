#!/usr/bin/env python3
"""TDD regression suite for the quarantined V7 live executor.

This suite uses a deterministic simulated broker only. It must never import
Robinhood/MCP code or call a network endpoint.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

QUARANTINE = Path.home() / ".hermes" / "quarantine"
SCRIPTS = Path(__file__).resolve().parents[1]
for path in (str(QUARANTINE), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

from v7_isolated_live_executor import (  # noqa: E402
    BrokerLifecycleHalted,
    DuplicateOrder,
    Executor,
    ExecutorError,
    LiveModeRejected,
    ReconciliationError,
    StateCorruption,
    deterministic_client_order_id,
)


IDENTITY = {
    "engineVersion": "7.4.21-early-reversal-detection",
    "strategyId": "v7421-early-reversal-detection",
    "releaseManifestSha256": "manifest-current",
    "sourceCodeSha256": "engine-current",
    "configSha256": "config-current",
}


def make_intent(action="entry", *, position_id="paper-1", symbol="QQQ",
                chain_symbol=None, instrument_id="inst-1", expiry="2026-09-04",
                quantity=1, limit_price=1.25):
    if chain_symbol is None:
        chain_symbol = symbol
    is_entry = action == "entry"
    return {
        "action": action,
        "positionId": position_id,
        "clientOrderId": deterministic_client_order_id(position_id, action),
        "symbol": symbol,
        "chainSymbol": chain_symbol,
        "underlyingType": "equity",
        "occSymbol": f"{symbol}260904C00500000",
        "instrumentId": instrument_id,
        "expiry": expiry,
        "side": "buy" if is_entry else "sell",
        "positionEffect": "open" if is_entry else "close",
        "quantity": quantity,
        "limitPrice": limit_price,
        **IDENTITY,
    }


class FakeBroker:
    """Deterministic simulated broker port used by every test."""

    def __init__(self, statuses=None):
        self.calls = []
        self.statuses = list(statuses or [{"state": "filled", "filledQuantity": 1, "averageFillPrice": 1.25}])
        self.positions = []
        self.open_orders = []
        self.next_order = 0
        self.state_path = None
        self.place_ids = []

    def review_order(self, intent):
        self.calls.append(("review_order", intent.copy()))
        return {"approved": True, "alerts": []}

    def place_order(self, intent):
        self.calls.append(("place_order", intent.copy()))
        self.next_order += 1
        order_id = f"sim-order-{self.next_order}"
        self.place_ids.append(order_id)
        return {"orderId": order_id, "clientOrderId": intent["clientOrderId"]}

    def get_order(self, order_id):
        self.calls.append(("get_order", order_id))
        if self.state_path is not None:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if order_id not in state.get("pendingOrders", {}):
                raise AssertionError("order_id_not_persisted_before_status_lookup")
        if not self.statuses:
            return {"state": "unknown", "filledQuantity": 0}
        return self.statuses.pop(0)

    def get_positions(self):
        self.calls.append(("get_positions", None))
        return [p.copy() for p in self.positions]

    def get_open_orders(self):
        self.calls.append(("get_open_orders", None))
        return [o.copy() for o in self.open_orders]

    def cancel_order(self, order_id):
        self.calls.append(("cancel_order", order_id))
        return {"cancelled": True, "orderId": order_id}


class ExecutorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="v7421-executor-test-"))
        self.state_path = self.tmpdir / "state.json"
        self.journal_path = self.tmpdir / "journal.jsonl"
        self.broker = FakeBroker()
        self.broker.state_path = self.state_path
        self.executor = Executor(
            self.broker,
            environment="sandbox",
            live_enabled=False,
            state_path=self.state_path,
            journal_path=self.journal_path,
            expected_identity=IDENTITY,
        )

    def read_state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))


class ConstructionSafetyTests(ExecutorTestCase):
    def test_live_environment_is_rejected_at_construction(self):
        with self.assertRaises(LiveModeRejected):
            Executor(self.broker, environment="live", state_path=self.state_path,
                     journal_path=self.journal_path, expected_identity=IDENTITY)

    def test_live_enabled_flag_is_rejected_at_construction(self):
        with self.assertRaises(LiveModeRejected):
            Executor(self.broker, environment="sandbox", live_enabled=True,
                     state_path=self.state_path, journal_path=self.journal_path,
                     expected_identity=IDENTITY)

    def test_default_mode_is_sandbox_and_disabled(self):
        executor = Executor(self.broker, state_path=self.state_path,
                            journal_path=self.journal_path, expected_identity=IDENTITY)
        self.assertEqual("sandbox", executor.environment)
        self.assertFalse(executor.live_enabled)


class SubmissionLifecycleTests(ExecutorTestCase):
    def test_review_occurs_before_placement(self):
        result = self.executor.submit(make_intent())
        self.assertEqual("filled", result["state"])
        names = [name for name, _ in self.broker.calls]
        self.assertLess(names.index("review_order"), names.index("place_order"))

    def test_broker_order_id_is_persisted_before_status_lookup(self):
        result = self.executor.submit(make_intent())
        self.assertEqual("sim-order-1", result["orderId"])
        self.assertIn("sim-order-1", self.read_state()["orders"])
        names = [name for name, _ in self.broker.calls]
        self.assertLess(names.index("place_order"), names.index("get_order"))

    def test_filled_entry_creates_local_position(self):
        result = self.executor.submit(make_intent())
        self.assertEqual("filled", result["state"])
        state = self.read_state()
        self.assertEqual("inst-1", state["livePositions"]["paper-1"]["instrumentId"])
        self.assertEqual(1, state["livePositions"]["paper-1"]["quantity"])

    def test_duplicate_client_order_id_is_rejected_before_review(self):
        self.executor.submit(make_intent())
        calls_before = len(self.broker.calls)
        with self.assertRaises(DuplicateOrder):
            self.executor.submit(make_intent())
        self.assertEqual(calls_before, len(self.broker.calls))

    def test_existing_broker_position_blocks_duplicate_entry(self):
        self.broker.positions = [{
            "instrumentId": "inst-1", "quantity": 1, "side": "long", "expiry": "2026-09-04",
        }]
        with self.assertRaises(DuplicateOrder):
            self.executor.submit(make_intent())
        self.assertNotIn("place_order", [name for name, _ in self.broker.calls])

    def test_rejected_entry_creates_no_local_position(self):
        self.broker.statuses = [{"state": "rejected", "filledQuantity": 0, "rejectReason": "blocked"}]
        with self.assertRaises(ExecutorError):
            self.executor.submit(make_intent())
        state = self.read_state()
        self.assertEqual({}, state["livePositions"])
        self.assertFalse(state["halted"])

    def test_partial_fill_halts_and_records_unresolved_order(self):
        self.broker.statuses = [{"state": "partially_filled", "filledQuantity": 1, "averageFillPrice": 1.20}]
        with self.assertRaises(BrokerLifecycleHalted):
            self.executor.submit(make_intent(quantity=2))
        state = self.read_state()
        self.assertTrue(state["halted"])
        self.assertEqual(1, state["unresolved"][0]["filledQuantity"])

    def test_unknown_status_halts_executor(self):
        self.broker.statuses = [{"state": "mystery_state", "filledQuantity": 0}]
        with self.assertRaises(BrokerLifecycleHalted):
            self.executor.submit(make_intent())
        self.assertTrue(self.read_state()["halted"])


class CloseLifecycleTests(ExecutorTestCase):
    def seed_filled_entry(self):
        entry = self.executor.submit(make_intent())
        self.broker.positions = [{
            "instrumentId": "inst-1", "quantity": 1, "side": "long", "expiry": "2026-09-04",
        }]
        return entry

    def test_close_removes_local_position_only_after_full_fill(self):
        self.seed_filled_entry()
        self.broker.statuses = [{"state": "filled", "filledQuantity": 1, "averageFillPrice": 1.10}]
        result = self.executor.submit(make_intent(action="exit"))
        self.assertEqual("filled", result["state"])
        self.assertEqual({}, self.read_state()["livePositions"])

    def test_partial_close_keeps_position_and_halts(self):
        self.seed_filled_entry()
        self.broker.statuses = [{"state": "partially_filled", "filledQuantity": 0}]
        with self.assertRaises(BrokerLifecycleHalted):
            self.executor.submit(make_intent(action="exit"))
        state = self.read_state()
        self.assertIn("paper-1", state["livePositions"])
        self.assertTrue(state["halted"])

    def test_rejected_close_keeps_position_and_halts(self):
        self.seed_filled_entry()
        self.broker.statuses = [{"state": "rejected", "filledQuantity": 0, "rejectReason": "closed"}]
        with self.assertRaises(BrokerLifecycleHalted):
            self.executor.submit(make_intent(action="exit"))
        state = self.read_state()
        self.assertIn("paper-1", state["livePositions"])
        self.assertTrue(state["halted"])


class ReconciliationTests(ExecutorTestCase):
    def seed_local_position(self):
        self.executor.submit(make_intent())

    def test_reconciliation_matches_instrument_quantity_side_and_expiry(self):
        self.seed_local_position()
        self.broker.positions = [{
            "instrumentId": "inst-1", "quantity": 1, "side": "long", "expiry": "2026-09-04",
        }]
        result = self.executor.reconcile()
        self.assertTrue(result["matched"])
        self.assertFalse(self.read_state()["halted"])

    def test_reconciliation_mismatch_halts(self):
        self.seed_local_position()
        self.broker.positions = [{
            "instrumentId": "inst-1", "quantity": 2, "side": "long", "expiry": "2026-09-04",
        }]
        with self.assertRaises(ReconciliationError) as ctx:
            self.executor.reconcile()
        self.assertIn("quantity", str(ctx.exception))
        self.assertTrue(self.read_state()["halted"])

    def test_corrupt_state_fails_closed(self):
        self.state_path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(StateCorruption):
            Executor(self.broker, state_path=self.state_path,
                     journal_path=self.journal_path, expected_identity=IDENTITY)


class IdentityAndMappingTests(ExecutorTestCase):
    def test_identity_mismatch_is_rejected(self):
        intent = make_intent()
        intent["strategyId"] = "old-strategy"
        with self.assertRaises(ExecutorError):
            self.executor.submit(intent)

    def test_client_order_id_is_uuid_shaped_and_deterministic(self):
        first = deterministic_client_order_id("paper-1", "entry")
        second = deterministic_client_order_id("paper-1", "entry")
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f-]{36}$")

    def test_qqq_uses_verified_qqq_chain_symbol(self):
        intent = make_intent(symbol="QQQ", chain_symbol="QQQ", instrument_id="qqq-inst")
        result = self.executor.submit(intent)
        self.assertEqual("QQQ", result["chainSymbol"])
        self.assertEqual("equity", result["underlyingType"])

    def test_chain_symbol_substitution_is_rejected(self):
        intent = make_intent(symbol="QQQ", chain_symbol="QQQW", instrument_id="qqq-inst")
        with self.assertRaises(ExecutorError):
            self.executor.submit(intent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
