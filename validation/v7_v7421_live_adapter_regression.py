#!/usr/bin/env python3
"""Regression tests for the quarantined V7.4.21 intent adapter."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

QUARANTINE_DIR = Path.home() / ".hermes" / "quarantine"
if str(QUARANTINE_DIR) not in sys.path:
    sys.path.insert(0, str(QUARANTINE_DIR))

import v7_v7421_live_adapter as adapter  # noqa: E402

IDENTITY = {
    "engineVersion": "7.4.21-early-reversal-detection",
    "strategyId": "v7421-early-reversal-detection",
    "sourceCodeSha256": "source-hash",
    "configSha256": "config-hash",
    "releaseManifestSha256": "manifest-hash",
}


class IntentAdapterTests(unittest.TestCase):
    def setUp(self):
        self.position = {
            "positionId": "paper-20260904T100000",
            "symbol": "QQQ",
            "chainSymbol": "QQQ",
            "underlyingType": "equity",
            "occSymbol": "QQQ260904C00500000",
            "instrumentId": "qqq-instrument-1",
            "expiry": "2026-09-04",
            "direction": "bullish",
            "entryAsk": 1.23,
            "entryBid": 1.20,
            **IDENTITY,
        }

    def test_entry_copies_exact_contract_and_entry_ask(self):
        action = {"action": "PAPER_ENTRY", "position": self.position}
        intent = adapter.build_order_intent(action, expected_identity=IDENTITY)
        self.assertEqual("entry", intent["action"])
        self.assertEqual("QQQ", intent["symbol"])
        self.assertEqual("QQQ", intent["chainSymbol"])
        self.assertEqual("equity", intent["underlyingType"])
        self.assertEqual(self.position["occSymbol"], intent["occSymbol"])
        self.assertEqual(self.position["instrumentId"], intent["instrumentId"])
        self.assertEqual(1.23, intent["limitPrice"])
        self.assertEqual("buy", intent["side"])
        self.assertEqual("open", intent["positionEffect"])

    def test_exit_copies_strategy_bid_and_reason_without_recomputing(self):
        action = {
            "action": "PAPER_EXIT",
            "event": "PAPER_EXIT_REAL_BID",
            "positionId": self.position["positionId"],
            "occSymbol": self.position["occSymbol"],
            "reason": "trailing_stop",
            "entryAsk": 1.23,
            "exitBid": 1.10,
            "quoteSource": "Robinhood MCP",
            **IDENTITY,
        }
        intent = adapter.build_order_intent(action, position=self.position, expected_identity=IDENTITY)
        self.assertEqual("exit", intent["action"])
        self.assertEqual(1.10, intent["limitPrice"])
        self.assertEqual("trailing_stop", intent["reason"])
        self.assertEqual("sell", intent["side"])
        self.assertEqual("close", intent["positionEffect"])
        self.assertEqual(self.position["instrumentId"], intent["instrumentId"])

    def test_fallback_quote_exit_is_rejected(self):
        action = {
            "action": "PAPER_EXIT",
            "event": "PAPER_EXIT_REAL_BID",
            "positionId": self.position["positionId"],
            "occSymbol": self.position["occSymbol"],
            "reason": "time_stop",
            "exitBid": 1.0455,
            "quoteSource": "fallback_last_known_bid",
            "quoteError": "quote bridge unavailable",
            **IDENTITY,
        }
        with self.assertRaises(adapter.IntentRejected):
            adapter.build_order_intent(action, position=self.position, expected_identity=IDENTITY)

    def test_release_identity_mismatch_is_rejected(self):
        action = {"action": "PAPER_ENTRY", "position": self.position}
        wrong = {**IDENTITY, "engineVersion": "7.4.20-evidence-gate-wiring"}
        with self.assertRaises(adapter.IntentRejected):
            adapter.build_order_intent(action, expected_identity=wrong)

    def test_missing_identity_is_rejected(self):
        position = {key: value for key, value in self.position.items() if key != "sourceCodeSha256"}
        with self.assertRaises(adapter.IntentRejected):
            adapter.build_order_intent({"action": "PAPER_ENTRY", "position": position}, expected_identity=IDENTITY)

    def test_contract_identity_mismatch_is_rejected(self):
        position = {**self.position, "chainSymbol": "SPXW"}
        with self.assertRaises(adapter.IntentRejected):
            adapter.build_order_intent({"action": "PAPER_ENTRY", "position": position}, expected_identity=IDENTITY)

    def test_exit_event_must_match_position_identity(self):
        action = {
            "action": "PAPER_EXIT",
            "event": "PAPER_EXIT_REAL_BID",
            "positionId": self.position["positionId"],
            "occSymbol": "XSP260904P00500000",
            "reason": "hard_close",
            "exitBid": 1.10,
            "quoteSource": "Robinhood MCP",
            **IDENTITY,
        }
        with self.assertRaises(adapter.IntentRejected):
            adapter.build_order_intent(action, position=self.position, expected_identity=IDENTITY)

    def test_execute_passes_only_valid_intent_to_executor(self):
        received = []

        class FakeExecutor:
            def submit(self, intent):
                received.append(intent)
                return {"state": "filled", "orderId": "sandbox-order-1"}

        action = {"action": "PAPER_ENTRY", "position": self.position}
        result = adapter.execute_action(action, executor=FakeExecutor(), expected_identity=IDENTITY)
        self.assertEqual("sandbox-order-1", result["orderId"])
        self.assertEqual(1, len(received))
        self.assertEqual(self.position["occSymbol"], received[0]["occSymbol"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
