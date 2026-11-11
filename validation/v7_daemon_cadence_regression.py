#!/usr/bin/env python3
"""Regression coverage for the V7 paper-daemon scan cadence."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import v7_scalp_live_daemon as daemon  # noqa: E402
import v7_scalp_paper as engine  # noqa: E402


ET = ZoneInfo("America/New_York")


class DaemonCadenceTests(unittest.TestCase):
    NOW = datetime(2026, 9, 8, 10, 0, tzinfo=ET)

    def test_near_limit_paper_scan_is_one_second(self):
        state = engine.default_state("SPY")
        state["dailyTrades"] = 2
        decision = daemon.choose_mode(state, self.NOW, symbol="SPY")

        self.assertEqual("near_limit_scan", decision.mode)
        self.assertEqual("scan", decision.action)
        self.assertEqual(1.0, decision.interval_seconds)

    def test_active_scan_is_one_second(self):
        state = engine.default_state("SPY")
        decision = daemon.choose_mode(state, self.NOW, symbol="SPY")

        self.assertEqual("active_scan", decision.mode)
        self.assertEqual(1.0, decision.interval_seconds)

    def test_open_position_monitor_remains_one_second(self):
        state = engine.default_state("SPY")
        state["activePositions"] = [{"positionId": "paper-test"}]
        decision = daemon.choose_mode(state, self.NOW, symbol="SPY")

        self.assertEqual("position", decision.mode)
        self.assertEqual("monitor", decision.action)
        self.assertEqual(1.0, decision.interval_seconds)

    def test_daemon_constant_is_one_second(self):
        self.assertEqual(1.0, daemon.NEAR_LIMIT_SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
