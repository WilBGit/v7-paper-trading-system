#!/usr/bin/env python3
"""V7.4.21 early signal-reversal regression tests.

Run with:
    python3 validation/v7421_signal_reversal_regression.py -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import v7_scalp_paper as engine


BASE_TIME = datetime(2026, 9, 1, 10, 0, tzinfo=engine.ET)


def position(
    direction: str,
    *,
    signal_close: float,
    macd_hist: float,
    ema9: float,
    ema21: float,
    momentum_pct: float,
) -> dict:
    return {
        "direction": direction,
        "entryVwap": 100.0,
        "signal": {
            "direction": direction,
            "close": signal_close,
            "signalBarEnd": BASE_TIME.isoformat(),
            "minTriggerMove": 0.02,
            "vwap": 100.0,
            "macdHist": macd_hist,
            "ema9": ema9,
            "ema21": ema21,
            "momentumPct": momentum_pct,
        },
    }


def bar(
    minutes: int,
    close: float,
    *,
    macd_hist: float,
    ema9: float,
    ema21: float,
    momentum_pct: float,
) -> dict:
    return {
        "dt": BASE_TIME + timedelta(minutes=minutes),
        "close": close,
        "macdHist": macd_hist,
        "ema9": ema9,
        "ema21": ema21,
        "momentumPct": momentum_pct,
    }


class EarlySignalReversalTests(unittest.TestCase):
    def test_macd_deceleration_requires_two_consecutive_bars(self):
        pos = position(
            "bearish", signal_close=100.20, macd_hist=-0.10,
            ema9=99.8, ema21=100.0, momentum_pct=-0.20,
        )
        first = bar(5, 99.90, macd_hist=-0.08, ema9=99.8, ema21=100.0, momentum_pct=-0.20)
        second = bar(10, 99.90, macd_hist=-0.05, ema9=99.8, ema21=100.0, momentum_pct=-0.20)

        self.assertIsNone(engine.signal_reversal_reason(pos, [first]))
        self.assertEqual(
            "signal_reversal_macd_deceleration",
            engine.signal_reversal_reason(pos, [first, second]),
        )

    def test_vwap_invalidation_is_independent_or_path_with_two_bar_debounce(self):
        pos = position(
            "bearish", signal_close=100.30, macd_hist=-0.10,
            ema9=99.8, ema21=100.0, momentum_pct=-0.20,
        )
        first = bar(5, 100.20, macd_hist=-0.10, ema9=99.8, ema21=100.0, momentum_pct=-0.20)
        second = bar(10, 100.20, macd_hist=-0.10, ema9=99.8, ema21=100.0, momentum_pct=-0.20)

        self.assertIsNone(engine.signal_reversal_reason(pos, [first]))
        self.assertEqual(
            "signal_reversal_vwap",
            engine.signal_reversal_reason(pos, [first, second]),
        )

    def test_ema_cross_is_independent_of_vwap_cross_with_two_bar_debounce(self):
        pos = position(
            "bearish", signal_close=100.20, macd_hist=-0.10,
            ema9=99.8, ema21=100.0, momentum_pct=-0.20,
        )
        first = bar(5, 99.90, macd_hist=-0.10, ema9=100.1, ema21=99.9, momentum_pct=-0.20)
        second = bar(10, 99.90, macd_hist=-0.10, ema9=100.2, ema21=99.8, momentum_pct=-0.20)

        self.assertIsNone(engine.signal_reversal_reason(pos, [first]))
        self.assertEqual(
            "signal_reversal_ema_cross",
            engine.signal_reversal_reason(pos, [first, second]),
        )

    def test_momentum_deceleration_is_detected_when_other_indicators_hold(self):
        pos = position(
            "bearish", signal_close=100.20, macd_hist=-0.10,
            ema9=99.8, ema21=100.0, momentum_pct=-0.20,
        )
        first = bar(5, 99.90, macd_hist=-0.10, ema9=99.8, ema21=100.0, momentum_pct=-0.15)
        second = bar(10, 99.90, macd_hist=-0.10, ema9=99.8, ema21=100.0, momentum_pct=-0.10)

        self.assertEqual(
            "signal_reversal_momentum_deceleration",
            engine.signal_reversal_reason(pos, [first, second]),
        )

    def test_bullish_reversal_checks_use_the_symmetric_direction(self):
        pos = position(
            "bullish", signal_close=99.70, macd_hist=0.10,
            ema9=100.2, ema21=100.0, momentum_pct=0.20,
        )
        first = bar(5, 99.80, macd_hist=0.08, ema9=99.9, ema21=100.1, momentum_pct=0.15)
        second = bar(10, 99.80, macd_hist=0.05, ema9=99.8, ema21=100.2, momentum_pct=0.10)

        self.assertEqual(
            "signal_reversal_macd_deceleration",
            engine.signal_reversal_reason(pos, [first, second]),
        )

    def test_close_based_fallback_requires_two_consecutive_completed_bars(self):
        pos = position(
            "bullish", signal_close=100.00, macd_hist=0.10,
            ema9=100.2, ema21=100.0, momentum_pct=0.20,
        )
        first = bar(5, 99.94, macd_hist=0.10, ema9=100.2, ema21=100.0, momentum_pct=0.20)
        second = bar(10, 99.93, macd_hist=0.10, ema9=100.2, ema21=100.0, momentum_pct=0.20)

        self.assertIsNone(engine.signal_reversal_reason(pos, [first]))
        self.assertEqual(
            "signal_reversal",
            engine.signal_reversal_reason(pos, [first, second]),
        )


if __name__ == "__main__":
    raise SystemExit(unittest.main())
