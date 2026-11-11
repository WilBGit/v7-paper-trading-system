#!/usr/bin/env python3
"""Regression tests for V7 structural signal quality."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import v7_scalp_paper as engine
import v7_scalp_photon_alert as alert


class SignalQualityRegressionTests(unittest.TestCase):
    def test_premarket_breakout_does_not_inflate_reported_score(self) -> None:
        def make_bar(dt: object, close: float, volume: int = 1000) -> dict[str, object]:
            return {
                "dt": dt,
                "date": dt.strftime("%Y-%m-%d"),
                "time": dt.hour * 100 + dt.minute,
                "open": close - 0.03,
                "high": close + 0.05,
                "low": close - 0.05,
                "close": close,
                "volume": volume,
            }

        prior_closes = []
        value = 768.0
        for index in range(35):
            value += 0.09 if index % 3 else -0.04
            prior_closes.append(round(value, 4))
        prior = [
            make_bar(
                datetime(2026, 8, 25, 9, 30) + timedelta(minutes=5 * index),
                close,
                900 + (index % 5) * 20,
            )
            for index, close in enumerate(prior_closes)
        ]
        session_closes = [769.50, 769.30, 769.60, 769.55, 769.75, 770.10]
        session = [
            make_bar(
                datetime(2026, 8, 26, 9, 30) + timedelta(minutes=5 * index),
                close,
                1200 if index == 5 else 1000,
            )
            for index, close in enumerate(session_closes)
        ]

        setup = engine.signal(
            session,
            warmup_bars=prior,
            relative_volume=2.0,
            premarket_context={
                "ready": True,
                "sessionDate": "2026-08-26",
                "premarketHigh": 770.05,
                "premarketLow": 768.50,
            },
            symbol="SPY",
        )

        self.assertTrue(setup["premarketBreakout"])
        # V7.5: score may include squeeze/accel components; verify it's not inflated by premarket alone
        self.assertGreaterEqual(setup["independentScore"], 5)
        self.assertGreaterEqual(setup["qualificationScore"], 5)
        self.assertGreaterEqual(setup["score"], 5)

    def test_post_quote_rejects_score_seven_without_actual_turtle_breakout(self) -> None:
        setup = {
            "direction": "bullish",
            "score": 7,
            "relativeVolume": 3.18,
            "turtle": {
                "longBreakout": False,
                "shortBreakout": False,
            },
        }

        failure = engine.post_quote_signal_failure(setup, setup, symbol="QQQ")

        self.assertEqual(failure, "signal_nonstructural_turtle")

    def test_acceleration_without_structural_breakout_is_rejected(self) -> None:
        setup = {
            "direction": "bullish",
            "score": 7,
            "bullChecks": {
                "squeeze_breakout": False,
                "momentum_acceleration": True,
            },
            "turtle": {"longBreakout": False, "shortBreakout": False},
        }

        gated = engine.apply_structural_entry_gate(setup)

        self.assertIsNone(gated["direction"])
        self.assertIn("signal_nonstructural_turtle", gated["rejectionReasons"])

    def test_acceleration_is_telemetry_only_for_score_and_evidence(self) -> None:
        checks = {
            "ema_trend": True,
            "supertrend": True,
            "vwap": True,
            "turtle_breakout": False,
            "squeeze_breakout": False,
            "macd": True,
            "momentum": False,
            "relative_volume": True,
            "rsi": True,
            "momentum_acceleration": True,
        }

        self.assertEqual(6, engine._signal_score(checks))
        self.assertEqual(
            {"trend": 2, "mean_reversion": 1, "momentum": 1, "flow": 2},
            engine.evidence_categories(checks, "bullish"),
        )

    def test_live_trigger_requires_structural_breakout(self) -> None:
        setup = {
            "direction": None,
            "rawDirection": "bullish",
            "score": 7,
            "relativeVolume": 1.2,
            "rejectionReasons": ["missing_fresh_bullish_trigger"],
            "bullChecks": {
                "squeeze_breakout": False,
                "momentum_acceleration": True,
            },
            "turtle": {"longBreakout": False, "shortBreakout": False},
        }

        self.assertFalse(engine.live_trigger_eligible(setup, "SPY"))

    def test_alert_gate_rejects_high_quality_label_without_actual_turtle_breakout(self) -> None:
        position = {
            "symbol": "QQQ",
            "direction": "bullish",
            "optionType": "call",
            "entryUnderlying": 718.0,
            "entryAsk": 1.02,
            "entryBid": 0.99,
            "entryCost": 102.0,
            "strike": 718.0,
            "expiry": "2026-08-27T00:00:00-04:00",
            "signal": {
                "score": 7,
                "ema9": 718.2,
                "ema21": 717.8,
                "vwap": 717.5,
                "rsi": 68.0,
                "macdHist": 0.10,
                "supertrend": "up",
                "momentumPct": 0.10,
                "relativeVolume": 1.20,
                "turtle": {
                    "longBreakout": False,
                    "shortBreakout": False,
                },
            },
        }

        passed, reasons = alert.alert_quality_gate(position)

        self.assertFalse(passed)
        self.assertIn("structural_breakout_required", reasons)

        for direction, key in (("bullish", "longBreakout"), ("bearish", "shortBreakout")):
            setup = {
                "direction": direction,
                "score": 8,
                "rejectionReasons": [],
                "turtle": {
                    "longBreakout": key == "longBreakout",
                    "shortBreakout": key == "shortBreakout",
                },
            }

            gated = engine.apply_structural_entry_gate(setup)

            self.assertEqual(gated["direction"], direction)
            self.assertEqual(gated["rejectionReasons"], [])

    def test_bearish_structural_breakout_can_enter_before_rsi_is_oversold(self) -> None:
        direction, raw_direction, reasons = engine.qualify_signal_direction(
            bull_score=2,
            bear_score=7,
            close=767.10,
            vwap=769.00,
            upper=770.00,
            lower=767.50,
            current_rsi=19.0,
            min_edge=2,
            last_bar_close=767.10,
            prev_bar_close=767.80,
            min_trigger_move=0.02,
            allow_rsi_extreme_on_breakout=True,
        )

        self.assertEqual(direction, "bearish")
        self.assertEqual(raw_direction, "bearish")
        self.assertNotIn("rsi_oversold_19", reasons)

    def test_bullish_structural_breakout_can_enter_before_rsi_is_overbought(self) -> None:
        direction, raw_direction, reasons = engine.qualify_signal_direction(
            bull_score=7,
            bear_score=2,
            close=770.00,
            vwap=768.00,
            upper=770.50,
            lower=767.50,
            current_rsi=75.0,
            min_edge=2,
            last_bar_close=770.00,
            prev_bar_close=769.30,
            min_trigger_move=0.02,
            allow_rsi_extreme_on_breakout=True,
        )

        self.assertEqual(direction, "bullish")
        self.assertEqual(raw_direction, "bullish")
        self.assertNotIn("rsi_overbought_75", reasons)

    def test_relative_volume_floor_is_lowered_for_breakout_capture(self) -> None:
        self.assertEqual(engine.MIN_RELATIVE_VOLUME, 0.75)

    def test_alert_allows_structural_breakout_with_lower_volume_and_extreme_rsi(self) -> None:
        position = {
            "symbol": "QQQ",
            "direction": "bearish",
            "optionType": "put",
            "entryUnderlying": 718.0,
            "entryAsk": 1.02,
            "entryBid": 0.99,
            "entryCost": 102.0,
            "strike": 718.0,
            "expiry": "2026-08-27T00:00:00-04:00",
            "signal": {
                "score": 7,
                "ema9": 717.8,
                "ema21": 718.2,
                "vwap": 718.5,
                "rsi": 19.0,
                "macdHist": -0.10,
                "supertrend": "down",
                "momentumPct": -0.10,
                "relativeVolume": 0.80,
                "turtle": {
                    "longBreakout": False,
                    "shortBreakout": True,
                },
            },
        }

        passed, reasons = alert.alert_quality_gate(position)

        self.assertTrue(passed, reasons)
        self.assertNotIn("relative_volume_below_0.75", reasons)
        self.assertNotIn("rsi_oversold", reasons)


if __name__ == "__main__":
    unittest.main()
