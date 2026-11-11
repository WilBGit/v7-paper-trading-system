#!/usr/bin/env python3
"""Regression coverage for the V7 live execution sidecar."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import v7_live_execution_daemon as daemon
import v7_live_order_bridge as bridge


class LiveExitFillTimeoutTests(unittest.TestCase):
    def test_live_exit_waits_30_seconds_before_timeout_cancel(self):
        position_id = "paper-test-exit-timeout"
        identity = {"engineVersion": "test-release"}
        event = {
            "event": "PAPER_EXIT_REAL_BID",
            "positionId": position_id,
            "engineVersion": "test-release",
            "symbol": "SPY",
            "occSymbol": "SPY260904C00750000",
            "exitBid": 1.25,
            "pnl": 10.0,
        }
        state = {
            "processedEntries": [position_id],
            "processedExits": [],
            "livePositions": {
                position_id: {
                    "orderId": "entry-order",
                    "instrumentId": "instrument-1",
                    "symbol": "SPY",
                    "occSymbol": "SPY260904C00750000",
                }
            },
            "retryBackoff": {},
        }

        with (
            patch.object(daemon, "_check_existing_order", return_value=None),
            patch.object(daemon, "_deliver_alert"),
            patch.object(bridge, "submit_exit_order", return_value={"orderId": "exit-order"}),
            patch.object(
                bridge,
                "wait_for_fill",
                return_value={"state": "filled", "fillPrice": 1.25, "filledQuantity": 1},
            ) as wait_for_fill,
            patch.object(bridge, "reconcile_positions", return_value={"healthy": True}),
        ):
            daemon._process_exit(event, state, identity)

        wait_for_fill.assert_called_once_with("exit-order", timeout_seconds=30.0)
        self.assertIn(position_id, state["processedExits"])
        self.assertNotIn(position_id, state["livePositions"])


class ReconciliationObservabilityTests(unittest.TestCase):
    def test_successful_reconciliation_records_timestamp(self):
        state = {
            "livePositions": {},
            "reconciliationHealthy": True,
        }
        result = {
            "healthy": True,
            "matched": [],
            "mismatched": [],
            "orphanedLocal": [],
            "orphanedBroker": [],
        }
        with patch.object(bridge, "reconcile_positions", return_value=result):
            observed = daemon._reconcile_live_state(state)

        self.assertTrue(observed["healthy"])
        recorded = state.get("lastReconciliationAt")
        self.assertIsInstance(recorded, str)
        self.assertIsNotNone(datetime.fromisoformat(recorded).tzinfo)

    def test_periodic_reconciliation_is_due_at_configured_interval(self):
        interval = daemon.RECONCILIATION_INTERVAL_SECONDS
        self.assertFalse(
            daemon._reconciliation_due(
                authorized=True,
                was_authorized=True,
                healthy=True,
                last_reconciliation_monotonic=10.0,
                now_monotonic=10.0 + interval - 0.01,
            )
        )
        self.assertTrue(
            daemon._reconciliation_due(
                authorized=True,
                was_authorized=True,
                healthy=True,
                last_reconciliation_monotonic=10.0,
                now_monotonic=10.0 + interval,
            )
        )
        self.assertTrue(
            daemon._reconciliation_due(
                authorized=True,
                was_authorized=True,
                healthy=False,
                last_reconciliation_monotonic=10.0,
                now_monotonic=10.1,
            )
        )


class KillSwitchTests(unittest.TestCase):
    def test_kill_switch_requires_explicit_boolean_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v7_kill_switch.json"
            with patch.object(daemon, "STATE_DIR", Path(tmp)):
                path.write_text(json.dumps({"active": 0}))
                self.assertTrue(daemon._kill_switch_active())
                path.write_text(json.dumps({"active": "false"}))
                self.assertTrue(daemon._kill_switch_active())
                path.write_text(json.dumps({"active": False}))
                self.assertFalse(daemon._kill_switch_active())


if __name__ == "__main__":
    unittest.main(verbosity=2)
