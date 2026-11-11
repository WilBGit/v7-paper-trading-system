#!/usr/bin/env python3
"""Regression tests for the V7 live-evidence progress tracker."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "v7_live_gate_progress.py"


class LiveGateProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SCRIPT.exists():
            raise AssertionError(f"missing tracker: {SCRIPT}")
        spec = importlib.util.spec_from_file_location("v7_live_gate_progress", SCRIPT)
        assert spec and spec.loader
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_only_exact_current_release_pairs_count(self):
        release = {
            "engineVersion": "7.4.21-test",
            "strategyId": "v7421-test",
            "releaseManifestSha256": "manifest-current",
            "sourceCodeSha256": "source-current",
            "configSha256": "config-current",
        }
        events = [
            {"event": "PAPER_ENTRY_REAL_ASK", "positionId": "current", "entryAsk": 1.00, **release},
            {"event": "PAPER_EXIT_REAL_BID", "positionId": "current", "entryAsk": 1.00, "exitBid": 1.44, "pnl": 44.0, **release},
            {"event": "PAPER_ENTRY_REAL_ASK", "positionId": "old", **{**release, "engineVersion": "7.4.20-old"}},
            {"event": "PAPER_EXIT_REAL_BID", "positionId": "old", "entryAsk": 1.00, "exitBid": 2.00, "pnl": 100.0, **{**release, "engineVersion": "7.4.20-old"}},
            {"event": "PAPER_ENTRY_REAL_ASK", "positionId": "mixed", **release},
            {"event": "PAPER_EXIT_REAL_BID", "positionId": "mixed", "entryAsk": 1.00, "exitBid": 1.99, "pnl": 99.0, **{**release, "configSha256": "wrong"}},
        ]

        result = self.module.summarize_events(events, release, {"min_closed_trades": 25, "min_win_rate_pct": 60.0, "require_positive_pnl": True})

        self.assertEqual(1, result["closedTrades"])
        self.assertEqual(1, result["wins"])
        self.assertEqual(44.0, result["netPnl"])
        self.assertIn("NOT ELIGIBLE", result["gateState"])
        self.assertEqual(24, result["remainingTrades"])

    def test_carry_forward_counts_toward_combined_gate(self):
        release = {
            "engineVersion": "7.4.23-test",
            "strategyId": "v7423-test",
            "releaseManifestSha256": "manifest-current",
            "sourceCodeSha256": "source-current",
            "configSha256": "config-current",
        }
        carry_forward = {
            "closedTrades": 15,
            "wins": 7,
            "lossesOrBreakeven": 8,
            "netPnl": 86.0,
            "sourceEngineVersion": "7.4.21-test",
            "sourceStrategyId": "v7421-test",
            "sourceReleaseManifestSha256": "manifest-previous",
        }

        result = self.module.summarize_events(
            [],
            release,
            {"min_closed_trades": 25, "min_win_rate_pct": 0.0, "require_positive_pnl": True},
            carry_forward,
        )

        self.assertEqual(15, result["closedTrades"])
        self.assertEqual(15, result["carriedForwardTrades"])
        self.assertEqual(0, result["exactClosedTrades"])
        self.assertEqual(25, result["remainingTrades"])
        self.assertEqual(86.0, result["netPnl"])
        self.assertFalse(result["gateMet"])  # carry-forward does not satisfy exact gate
        self.assertIn("exact current-release 0/25", result["gateState"])

    def test_gate_met_is_review_required_not_auto_live(self):
        release = {
            "engineVersion": "7.4.21-test",
            "strategyId": "v7421-test",
            "releaseManifestSha256": "manifest-current",
            "sourceCodeSha256": "source-current",
            "configSha256": "config-current",
        }
        events = []
        for index in range(5):
            pid = f"trade-{index}"
            events.extend([
                {"event": "PAPER_ENTRY_REAL_ASK", "positionId": pid, "entryAsk": 1.00, **release},
                {"event": "PAPER_EXIT_REAL_BID", "positionId": pid, "entryAsk": 1.00, "exitBid": 1.10, "pnl": 10.0, **release},
            ])

        result = self.module.summarize_events(events, release, {"min_closed_trades": 5, "min_win_rate_pct": 60.0, "require_positive_pnl": True})

        self.assertEqual("REVIEW REQUIRED", result["gateState"])
        self.assertTrue(result["gateMet"])
        self.assertFalse(result["enableLiveAutomatically"])

    def test_carry_forward_does_not_satisfy_exact_gate(self):
        """Carry-forward does not satisfy the exact current-release gate."""
        release = {
            "engineVersion": "7.4.23-test",
            "strategyId": "v7423-test",
            "releaseManifestSha256": "manifest-current",
            "sourceCodeSha256": "source-current",
            "configSha256": "config-current",
        }
        carry_forward = {
            "closedTrades": 15,
            "wins": 7,
            "lossesOrBreakeven": 8,
            "netPnl": 86.0,
            "sourceEngineVersion": "7.4.21-test",
            "sourceStrategyId": "v7421-test",
            "sourceReleaseManifestSha256": "manifest-previous",
        }
        events = []
        for index in range(10):
            pid = f"trade-{index}"
            events.extend([
                {"event": "PAPER_ENTRY_REAL_ASK", "positionId": pid, "entryAsk": 1.00, **release},
                {"event": "PAPER_EXIT_REAL_BID", "positionId": pid, "entryAsk": 1.00, "exitBid": 1.50, "pnl": 50.0, **release},
            ])

        result = self.module.summarize_events(
            events,
            release,
            {"min_closed_trades": 25, "min_win_rate_pct": 0.0, "require_positive_pnl": True},
            carry_forward,
        )

        self.assertEqual(25, result["closedTrades"])
        self.assertEqual(15, result["carriedForwardTrades"])
        self.assertEqual(10, result["exactClosedTrades"])
        self.assertEqual(15, result["remainingTrades"])
        self.assertFalse(result["gateMet"])
        self.assertTrue(result["combinedGateMet"])
        self.assertIn("exact current-release 10/25", result["gateState"])

    def test_snapshot_labels_operator_promotion_policy(self):
        with patch.object(self.module, "_load_events", return_value=[]):
            snapshot = self.module.build_snapshot()

        self.assertEqual(0.0, snapshot["promotionGate"]["minWinRatePct"])
        self.assertEqual("operator_promotion", snapshot["gatePolicy"])
        self.assertNotIn("releaseQualityGate", snapshot)
        self.assertFalse(snapshot["enableLiveAutomatically"])

    def test_notification_only_changes_when_snapshot_changes(self):
        old = {"closedTrades": 1, "wins": 1, "netPnl": 44.0, "gateState": "NOT ELIGIBLE"}
        same = dict(old)
        changed = {**old, "closedTrades": 2}

        self.assertFalse(self.module.should_notify(old, same))
        self.assertTrue(self.module.should_notify(old, changed))


if __name__ == "__main__":
    unittest.main(verbosity=2)
