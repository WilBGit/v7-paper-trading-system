#!/usr/bin/env python3
"""Regression tests for the breakeven_exit / breakeven_timeout protective exits.

V7.5.2 universe is SPY+QQQ; IWM/XSP were removed.  Fixtures use SPY.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import v7_scalp_paper as engine  # noqa: E402


class ExitReasonTests(unittest.TestCase):
    def _position(self, now: datetime, held_minutes: float):
        return {
            "symbol": "SPY",
            "direction": "bearish",
            "optionType": "put",
            "entryAsk": 1.72,
            "entryTime": (now - timedelta(minutes=held_minutes)).isoformat(),
            "peakBid": 1.72,
            "signal": {
                "direction": "bearish",
                "independentScore": 7,
                "bearChecks": {},
                "turtle": {"shortBreakout": False},
            },
        }

    def test_loss_below_three_percent_after_minimum_hold_is_breakeven_exit(self):
        now = datetime(2026, 9, 10, 11, 0, tzinfo=engine.ET)
        position = self._position(now, held_minutes=5.9)

        reason = engine.price_exit_reason(position, 1.66, now=now, peak_bid=1.72)

        self.assertEqual("breakeven_exit", reason)
        self.assertNotEqual("cut_loss_exit", reason)

    def test_small_negative_pnl_after_dynamic_hold_is_breakeven_timeout(self):
        now = datetime(2026, 9, 10, 11, 0, tzinfo=engine.ET)
        position = self._position(now, held_minutes=8.1)

        reason = engine.price_exit_reason(position, 1.71, now=now, peak_bid=1.72)

        self.assertEqual("breakeven_timeout", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
