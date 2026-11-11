#!/usr/bin/env python3
"""Regression tests for the paper-only V7 ICT shadow overlay."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import v7_ict_shadow as ict


ET = ict.ET


def make_bar(index: int, *, open_: float, high: float, low: float, close: float) -> dict:
    dt = datetime(2026, 11, 3, 9, 30, tzinfo=ET) + timedelta(minutes=5 * index)
    return {
        "dt": dt,
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.hour * 100 + dt.minute,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1000.0,
    }


def bullish_ict_fixture() -> list[dict]:
    # A confirmed swing high at index 2 and swing low at index 3, a sell-side
    # liquidity sweep at index 6, bullish displacement/MSS at index 7, and a
    # still-open bullish FVG confirmed by index 8.
    values = [
        (100.0, 102.0, 99.0, 101.0),
        (101.0, 105.0, 100.0, 104.0),
        (104.0, 106.0, 98.0, 99.0),
        (99.0, 101.0, 95.0, 97.0),
        (97.0, 99.0, 96.0, 98.0),
        (98.0, 100.0, 97.0, 99.0),
        (99.0, 99.5, 94.0, 99.2),
        (99.2, 108.0, 100.0, 107.0),
        (107.0, 109.0, 101.0, 108.0),
    ]
    return [make_bar(i, open_=o, high=h, low=l, close=c) for i, (o, h, l, c) in enumerate(values)]


def bearish_ict_fixture() -> list[dict]:
    source = bullish_ict_fixture()
    mirrored = []
    for bar in source:
        mirrored.append({
            **bar,
            "open": 220.0 - bar["open"],
            "high": 220.0 - bar["low"],
            "low": 220.0 - bar["high"],
            "close": 220.0 - bar["close"],
        })
    return mirrored


class IctComponentTests(unittest.TestCase):
    def test_bullish_sweep_mss_fvg_and_discount_confluence(self):
        result = ict.evaluate_ict(bullish_ict_fixture(), symbol="SPY")

        self.assertTrue(result["signal"])
        self.assertEqual("bullish", result["direction"])
        self.assertEqual("sell_side", result["liquiditySweep"]["side"])
        self.assertTrue(result["marketStructureShift"]["confirmed"])
        self.assertEqual("bullish", result["fairValueGap"]["direction"])
        self.assertEqual("discount", result["location"])
        self.assertTrue(result["paperOnly"])

    def test_bearish_mirror_is_classified_without_direction_leak(self):
        result = ict.evaluate_ict(bearish_ict_fixture(), symbol="QQQ")

        self.assertTrue(result["signal"])
        self.assertEqual("bearish", result["direction"])
        self.assertEqual("buy_side", result["liquiditySweep"]["side"])
        self.assertEqual("bearish", result["fairValueGap"]["direction"])
        self.assertEqual("premium", result["location"])

    def test_filled_fvg_fails_closed(self):
        bars = bullish_ict_fixture()
        bars.append(make_bar(9, open_=108.0, high=109.0, low=99.0, close=108.0))

        result = ict.evaluate_ict(bars, symbol="SPY")

        self.assertFalse(result["signal"])
        self.assertEqual("fvg_filled", result["reason"])


class IctOverlayBoundaryTests(unittest.TestCase):
    def test_overlay_never_rewrites_core_v7_direction(self):
        setup = {"direction": "bullish", "score": 7, "rejectionReasons": []}
        result = ict.apply_ict_overlay(setup, bearish_ict_fixture(), symbol="SPY")

        self.assertEqual("bullish", result["direction"])
        self.assertFalse(result["ictConfluence"])
        self.assertEqual("bearish", result["ictDirection"])
        self.assertIn("ictOverlay", result)

    def test_missing_components_are_not_promoted(self):
        result = ict.evaluate_ict(bullish_ict_fixture()[:7], symbol="SPY")

        self.assertFalse(result["signal"])
        self.assertIn(result["reason"], {"insufficient_bars", "no_displacement_mss", "no_fvg", "no_unfilled_fvg"})
        self.assertTrue(result["paperOnly"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
