#!/usr/bin/env python3
"""Durable V7.3 safety regression suite.

The filename intentionally does not start with test_ because Hermes' disk-cleanup
plugin classifies newly agent-authored test_* files under ~/.hermes as ephemeral.
Run with: python3 validation/v73_regression_suite.py -v
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import v7_rh_quote_bridge as quote_bridge
import v7_rh_quote_worker as quote_worker
import v7_scalp_paper as engine
import v7_scalp_photon_alert as photon
import v7_scalp_live_daemon as live_daemon
import v7_anomaly_watchdog as watchdog
import v7_scalp_weekly_report as weekly_report
import v7_trade_signal_monitor as trade_monitor
import trading_dream_audit as dream_audit
import v7_preflight_check as preflight


class RetiredUniverseTests(unittest.TestCase):
    def test_iwm_and_xsp_are_not_scanned_or_quoteable(self):
        """IWM and XSP stay out of the engine registry and quote allowlist."""
        self.assertEqual(("SPY", "QQQ"), tuple(engine.SYMBOLS))
        for symbol in ("IWM", "XSP"):
            self.assertNotIn(symbol, engine.SYMBOLS)
            self.assertNotIn(symbol, quote_bridge.ALLOWED_UNDERLYINGS)
            with self.assertRaises(ValueError) as raised:
                engine.config_for_symbol(symbol)
            self.assertIn(f"unknown_symbol:{symbol}", str(raised.exception))
        brief = (SCRIPTS / "v73_morning_premarket_brief.py").read_text()
        self.assertNotIn('"IWM"', brief)
        self.assertNotIn("'IWM'", brief)
        self.assertNotIn('"XSP"', brief)
        self.assertNotIn("'XSP'", brief)


class PreflightSessionTests(unittest.TestCase):
    def test_xsp_state_reset_is_not_an_active_symbol(self):
        """Retired XSP files must not be treated as a live scanner symbol."""
        with self.assertRaises(ValueError) as raised:
            engine.default_state("XSP")
        self.assertIn("unknown_symbol:XSP", str(raised.exception))

    def test_macro_calendar_check_rejects_overage_even_when_coverage_is_future(self):
        """Preflight must honor the engine's seven-day calendar freshness gate."""
        calendar = {
            "source": "official schedule review",
            "timezone": "America/New_York",
            "generatedAt": "2026-08-21T02:52:32-04:00",
            "coverageStart": "2026-08-21",
            "coverageThrough": "2026-09-04",
            "days": {},
        }
        fixed_now = datetime(2026, 8, 28, 9, 35, tzinfo=engine.ET)
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "v7_macro_calendar.json").write_text(json.dumps(calendar))
            with (
                patch.object(preflight, "STATE_DIR", state_dir),
                patch.object(preflight, "now_et", return_value=fixed_now),
            ):
                result = preflight.check_macro_calendar()

        self.assertEqual(preflight.FAIL, result["status"])
        self.assertIn("stale", result["detail"])

    def test_macro_calendar_check_passes_fresh_calendar_with_next_session_coverage(self):
        """A fresh calendar with valid coverage must pass preflight."""
        calendar = {
            "source": "official schedule review",
            "timezone": "America/New_York",
            "generatedAt": "2026-08-28T09:00:00-04:00",
            "coverageStart": "2026-08-28",
            "coverageThrough": "2026-09-04",
            "days": {"2026-08-31": []},
        }
        fixed_now = datetime(2026, 8, 28, 9, 35, tzinfo=engine.ET)
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "v7_macro_calendar.json").write_text(json.dumps(calendar))
            with (
                patch.object(preflight, "STATE_DIR", state_dir),
                patch.object(preflight, "now_et", return_value=fixed_now),
            ):
                result = preflight.check_macro_calendar()

        self.assertEqual(preflight.PASS, result["status"])
        self.assertIn("fresh", result["detail"])


class IwmOperationalCoverageTests(unittest.TestCase):
    def test_evidence_gate_warns_on_negative_exact_release_pnl(self):
        """Current-release losses must be visible as a quality warning."""
        release = {
            "engineVersion": "test-release",
            "strategyId": "test-strategy",
            "sourceCodeSha256": "a" * 64,
            "configSha256": "b" * 64,
        }
        shared = {"positionId": "paper-spy-negative", **release}
        events = [
            {"event": "PAPER_ENTRY_REAL_ASK", **shared},
            {"event": "PAPER_EXIT_REAL_BID", "pnl": -4.0, **shared},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "v7_scalp_trades_SPY.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events)
            )
            with (
                patch.object(preflight, "STATE_DIR", state_dir),
                patch.object(preflight, "active_release_identity", return_value=release),
            ):
                result = preflight.check_evidence_gate()

        self.assertEqual(preflight.WARN, result["status"])

    def test_evidence_gate_ignores_retired_iwm_ledger(self):
        """Leftover IWM ledgers must not count toward exact-release evidence."""
        release = {
            "engineVersion": "test-release",
            "strategyId": "test-strategy",
            "sourceCodeSha256": "a" * 64,
            "configSha256": "b" * 64,
        }
        shared = {"positionId": "paper-iwm-1", **release}
        events = [
            {"event": "PAPER_ENTRY_REAL_ASK", **shared},
            {"event": "PAPER_EXIT_REAL_BID", "pnl": 12.0, **shared},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "v7_scalp_trades_IWM.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events)
            )
            with (
                patch.object(preflight, "STATE_DIR", state_dir),
                patch.object(preflight, "active_release_identity", return_value=release),
            ):
                result = preflight.check_evidence_gate()

        self.assertNotIn("IWM", result["detail"])
        self.assertIn("exact current-release detail 0/", result["detail"])

    def test_watchdog_ignores_retired_iwm_ledger(self):
        """Leftover IWM hard closes must not generate scanner alerts."""
        fixed_now = datetime(2026, 8, 21, 10, 0, tzinfo=engine.ET)
        event = {
            "event": "PAPER_EXIT_REAL_BID",
            "positionId": "paper-20260821T100000",
            "reason": "hard_close",
            "pnlPct": -18.0,
            "pnl": -5.0,
            "heldMinutes": 25.0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "v7_scalp_trades_IWM.jsonl").write_text(json.dumps(event) + "\n")
            with (
                patch.object(watchdog, "STATE_DIR", state_dir),
                patch.object(watchdog, "now_et", return_value=fixed_now),
            ):
                alerts = watchdog.check_recent_hard_closes()

        self.assertFalse(any("IWM" in alert for alert in alerts))

    def test_preflight_anomalous_hold_ignores_retired_iwm_ledger(self):
        """Leftover IWM over-holds must not fail active-universe preflight."""
        event = {
            "event": "PAPER_EXIT_REAL_BID",
            "positionId": "paper-iwm-overhold",
            "heldMinutes": 42,
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "v7_scalp_trades_IWM.jsonl").write_text(json.dumps(event) + "\n")
            with patch.object(preflight, "STATE_DIR", state_dir):
                result = preflight.check_anomalous_holds()

        self.assertNotEqual(preflight.FAIL, result["status"])
        self.assertNotIn("IWM", result.get("detail", ""))

    def test_weekly_report_excludes_retired_iwm_ledger(self):
        """Weekly reporting must follow the active SPY/QQQ registry only."""
        events = [
            {"event": "PAPER_ENTRY_REAL_ASK", "positionId": "paper-iwm-report"},
            {"event": "PAPER_EXIT_REAL_BID", "positionId": "paper-iwm-report", "pnl": 8.0},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "v7_scalp_trades_IWM.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events)
            )
            stdout = io.StringIO()
            with (
                patch.object(weekly_report, "STATE_DIR", state_dir),
                contextlib.redirect_stdout(stdout),
            ):
                weekly_report.main()

        payload = json.loads(stdout.getvalue())
        symbols = [row["symbol"] for row in payload.get("symbols", [])]
        self.assertNotIn("IWM", symbols)

    def test_daily_report_wrapper_reads_engine_symbol_registry(self):
        """Daily reporting must follow the engine registry instead of a static ticker list."""
        wrapper = (SCRIPTS / "v7_scalp_report.sh").read_text()
        self.assertIn("engine.SYMBOLS", wrapper)

    def test_premarket_brief_reads_engine_symbol_registry(self):
        """The morning brief must publish levels for every active scanner symbol."""
        brief = (SCRIPTS / "v73_morning_premarket_brief.py").read_text()
        self.assertIn("for sym in engine.SYMBOLS:", brief)

    def test_nightly_audit_ignores_retired_iwm_state_and_ledger(self):
        """Leftover IWM files must not re-enter the active audit universe."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state_dir = home / "state"
            state_dir.mkdir()
            state_file = state_dir / "v7_scalp_state_IWM.json"
            log_file = state_dir / "v7_scalp_trades_IWM.jsonl"
            spy_state = state_dir / "v7_scalp_state_SPY.json"
            spy_log = state_dir / "v7_scalp_trades_SPY.jsonl"
            state_file.write_text("{}")
            log_file.write_text("{}\n")
            spy_state.write_text("{}")
            spy_log.write_text("{}\n")
            states, logs = dream_audit.discover_data_paths(None, home)

        self.assertNotIn("IWM", states)
        self.assertNotIn("IWM", logs)
        self.assertEqual(spy_state, states.get("SPY"))
        self.assertEqual(spy_log, logs.get("SPY"))


class TradeSignalMonitorTests(unittest.TestCase):
    def test_market_hours_end_at_v7_hard_close(self):
        """The monitor must not flag intentional off_hours after hard close."""
        def fixed_datetime(hour, minute):
            class FixedDateTime:
                @classmethod
                def now(cls, tz=None):
                    current = datetime(2026, 9, 25, hour, minute, tzinfo=trade_monitor.ET)
                    return current.astimezone(tz) if tz else current.replace(tzinfo=None)

            return FixedDateTime

        with patch.object(trade_monitor, "datetime", fixed_datetime(15, 54)):
            self.assertTrue(trade_monitor.is_market_hours())
        with patch.object(trade_monitor, "datetime", fixed_datetime(15, 55)):
            self.assertFalse(trade_monitor.is_market_hours())
        with patch.object(trade_monitor, "datetime", fixed_datetime(15, 57)):
            self.assertFalse(trade_monitor.is_market_hours())

    def test_exit_alert_reports_dollar_pnl_with_percent_as_secondary(self):
        alert = trade_monitor.format_exit_pnl(
            "QQQ",
            {
                "pnl": -4.0,
                "pnlPct": -3.08,
                "heldMinutes": 13.2,
                "reason": "breakeven_timeout",
            },
        )
        self.assertEqual(
            "QQQ EXIT: P&L=$-4.00 (-3.1%) held=13min reason=breakeven_timeout",
            alert,
        )

    def test_exit_alert_does_not_treat_percent_as_dollars_when_dollar_is_missing(self):
        alert = trade_monitor.format_exit_pnl(
            "QQQ",
            {"pnlPct": -3.08, "heldMinutes": 2.0, "reason": "test"},
        )
        self.assertEqual("QQQ EXIT: P&L=$N/A (-3.1%) held=2min reason=test", alert)


class AlertRoutingTests(unittest.TestCase):
    def test_direct_alerts_deliver_to_telegram_and_photon(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return Mock(returncode=0, stdout="", stderr="")

        with patch.object(live_daemon.subprocess, "run", side_effect=fake_run):
            live_daemon.deliver_alert("qualified paper alert")

        targets = [args[args.index("--to") + 1] for args, _ in calls]
        self.assertEqual(["telegram", "photon"], targets)
        for args, kwargs in calls:
            self.assertEqual("qualified paper alert", args[-1])
            self.assertTrue(kwargs["check"] is False)


class HardGateTests(unittest.TestCase):
    def test_score_seven_cannot_bypass_failed_relative_volume_gate(self):
        direction, raw_direction, reasons = engine.qualify_signal_direction(
            bull_score=7,
            bear_score=1,
            close=101.0,
            vwap=100.0,
            upper=101.0,
            lower=99.0,
            hard_gate_failures=["relative_volume_below_0.75"],
            current_rsi=55.0,
            min_edge=engine.MIN_DIRECTION_EDGE,
            last_bar_close=101.0,
            prev_bar_close=100.9,
            min_trigger_move=0.02,
        )
        self.assertIsNone(direction)
        self.assertEqual("bullish", raw_direction)
        self.assertIn("hard_gate:relative_volume_below_0.75", reasons)

    def test_bearish_setup_inside_three_sigma_is_not_chase(self):
        direction, raw_direction, reasons = engine.qualify_signal_direction(
            bull_score=1,
            bear_score=8,
            close=97.5,
            vwap=100.0,
            upper=101.0,
            lower=99.0,
            current_rsi=40.0,
            min_edge=engine.MIN_DIRECTION_EDGE,
            last_bar_close=97.5,
            prev_bar_close=97.6,
            min_trigger_move=0.02,
        )

        self.assertEqual("bearish", raw_direction)
        self.assertEqual("bearish", direction, reasons)

    @staticmethod
    def _momentum_bars(direction: str):
        bars = []
        for index in range(40):
            close = 105.0 - index * 0.15 if direction == "bearish" else 95.0 + index * 0.15
            bars.append({
                "date": "2026-08-10",
                "time": 940 + index * 5,
                "open": close,
                "high": close + 0.05,
                "low": close - 0.05,
                "close": close,
                "volume": 1000.0,
            })
        return bars

    def test_high_volume_does_not_inflate_score_or_admit_weak_setup(self):
        """V7.4.9: High volume is telemetry only and cannot admit a weak setup.

        This preserves the V7.4.8 fix that stopped high-volume substitution, then
        verifies the quality floor rejects the resulting honest score-6 setup.
        """
        bars = []
        for index in range(40):
            close = 99.4 - index * 0.005
            bars.append({
                "date": "2026-08-11", "time": 940 + index * 5,
                "open": close + 0.02, "high": close + 0.05,
                "low": close - 0.05, "close": close,
                "volume": 2500.0 if index >= 37 else 1000.0,
            })
        with (
            patch.object(engine, "ema", side_effect=lambda _values, period: 99.0 if period == 9 else 100.0),
            patch.object(engine, "vwap_bands", return_value=(100.0, 101.0, 99.0)),
            patch.object(engine, "rsi", return_value=50.0),
            patch.object(engine, "macd_hist", return_value=-0.2),
            patch.object(engine, "supertrend_direction", return_value="down"),
            patch.object(engine, "atr", return_value=0.3),
            patch.object(engine, "turtle_intraday_context", return_value={
                "entryHigh": 0.0, "entryLow": 0.0, "exitHigh": 0.0, "exitLow": 0.0,
                "n": 1.0, "longBreakout": False, "shortBreakout": False,
            }),
        ):
            result = engine.signal(bars)

        # High-volume telemetry should still fire (rel_vol >= 2.0, momentum missing)
        self.assertTrue(result["bearishHighVolumeConfirmation"])
        # But it must NOT inflate the checks
        self.assertFalse(result["bearChecks"]["turtle_breakout"])  # actual = False
        self.assertFalse(result["bearChecks"]["momentum"])  # not substituted
        # Score is honest: ema_trend + vwap + supertrend + macd + rsi + rel_vol = 6
        # (turtle=False, momentum=False)
        self.assertEqual(6, result["bearScore"])
        self.assertEqual(6, result["independentScore"])
        self.assertIn("bearish_volume_as_momentum", result["substitutionFlags"])
        # A score-6 setup without momentum or a real breakout is not actionable.
        self.assertIsNone(result["rawDirection"])
        self.assertIsNone(result["direction"])
        self.assertIn("score_below_7", result["rejectionReasons"])

    def test_honest_score_reflects_actual_turtle_breakout(self):
        """V7.4.8: When turtle breakout is real, it contributes to the score."""
        bars = []
        for index in range(40):
            close = 99.4 - index * 0.005
            bars.append({
                "date": "2026-08-11", "time": 940 + index * 5,
                "open": close + 0.02, "high": close + 0.05,
                "low": close - 0.05, "close": close,
                "volume": 1000.0,
            })
        with (
            patch.object(engine, "ema", side_effect=lambda _values, period: 99.0 if period == 9 else 100.0),
            patch.object(engine, "vwap_bands", return_value=(100.0, 101.0, 99.0)),
            patch.object(engine, "rsi", return_value=50.0),
            patch.object(engine, "macd_hist", return_value=-0.2),
            patch.object(engine, "supertrend_direction", return_value="down"),
            patch.object(engine, "atr", return_value=0.3),
            patch.object(engine, "turtle_intraday_context", return_value={
                "entryHigh": 0.0, "entryLow": 0.0, "exitHigh": 0.0, "exitLow": 0.0,
                "n": 1.0, "longBreakout": False, "shortBreakout": True,  # real breakout
            }),
        ):
            result = engine.signal(bars)

        self.assertTrue(result["bearChecks"]["turtle_breakout"])
        self.assertEqual(7, result["bearScore"])  # 6 + 1 for real turtle
        self.assertEqual(7, result["independentScore"])

    def test_signal_strength_uses_independent_score_not_volume(self):
        """V7.4.8: _entry_signal_strength must not give a volume bonus."""
        pos_low_vol = {
            "direction": "bullish",
            "signal": {
                "direction": "bullish",
                "independentScore": 6,
                "relativeVolume": 1.0,
                "bullChecks": {"ema_trend": True, "vwap": True, "supertrend": True,
                               "macd": True, "rsi": True, "momentum": True,
                               "relative_volume": True, "turtle_breakout": False},
            },
        }
        pos_high_vol = {
            "direction": "bullish",
            "signal": {
                "direction": "bullish",
                "independentScore": 6,
                "relativeVolume": 5.0,  # 5x volume — should NOT change strength
                "bullChecks": {"ema_trend": True, "vwap": True, "supertrend": True,
                               "macd": True, "rsi": True, "momentum": True,
                               "relative_volume": True, "turtle_breakout": False},
            },
        }
        self.assertAlmostEqual(
            engine._entry_signal_strength(pos_low_vol),
            engine._entry_signal_strength(pos_high_vol),
            places=6,
        )

    def test_crash_detector_reports_actual_score_instead_of_forcing_seven(self):
        bars = self._momentum_bars("bearish")
        with (
            patch.object(engine, "macd_hist", side_effect=lambda values: -0.20 if len(values) >= 40 else -0.10),
            patch.object(engine, "rsi", return_value=50.0),
            patch.object(engine, "ema", return_value=100.0),
            patch.object(engine, "supertrend_direction", return_value="flat"),
            patch.object(engine, "turtle_intraday_context", return_value={
                "entryHigh": 0.0, "entryLow": 0.0, "exitHigh": 0.0, "exitLow": 0.0,
                "n": 1.0, "longBreakout": False, "shortBreakout": False,
            }),
        ):
            result = engine.crash_signal(bars)
        self.assertIsNotNone(result)
        self.assertLess(result["bearScore"], 7)
        self.assertEqual(result["bearScore"], result["score"])

    def test_pump_detector_reports_actual_score_instead_of_forcing_seven(self):
        bars = self._momentum_bars("bullish")
        with (
            patch.object(engine, "macd_hist", side_effect=lambda values: 0.20 if len(values) >= 40 else 0.10),
            patch.object(engine, "rsi", return_value=50.0),
            patch.object(engine, "ema", return_value=100.0),
            patch.object(engine, "supertrend_direction", return_value="flat"),
            patch.object(engine, "turtle_intraday_context", return_value={
                "entryHigh": 0.0, "entryLow": 0.0, "exitHigh": 0.0, "exitLow": 0.0,
                "n": 1.0, "longBreakout": False, "shortBreakout": False,
            }),
        ):
            result = engine.pump_signal(bars)
        self.assertIsNotNone(result)
        self.assertLess(result["bullScore"], 7)
        self.assertEqual(result["bullScore"], result["score"])

    @staticmethod
    def _position(relative_volume: float = 1.20, supertrend: str = "up"):
        return {
            "symbol": "SPY",
            "direction": "bullish",
            "optionType": "call",
            "strike": 100.0,
            "expiry": "2026-08-11",
            "entryAsk": 1.20,
            "entryBid": 1.18,
            "entryCost": 120.0,
            "entryUnderlying": 100.0,
            "signal": {
                "score": 7,
                "ema9": 100.2,
                "ema21": 99.8,
                "vwap": 99.9,
                "rsi": 55.0,
                "macdHist": 0.2,
                "supertrend": supertrend,
                "momentumPct": 0.10,
                "relativeVolume": relative_volume,
                "turtle": {"longBreakout": True, "shortBreakout": False},
            },
        }

    def test_photon_rejects_relative_volume_below_engine_hard_gate(self):
        passed, reasons = photon.alert_quality_gate(self._position(relative_volume=0.74))
        self.assertFalse(passed)
        self.assertIn("relative_volume_below_0.75", reasons)

    def test_photon_uses_same_five_percent_spread_ceiling_as_engine(self):
        position = self._position()
        position["entryBid"] = 1.13
        passed, reasons = photon.alert_quality_gate(position)
        self.assertFalse(passed)
        self.assertIn("contract_spread_above_5pct", reasons)

    def test_photon_does_not_turn_score_seven_into_all_indicators_quota(self):
        passed, reasons = photon.alert_quality_gate(self._position(supertrend="flat"))
        self.assertTrue(passed, reasons)

    def test_photon_uses_symbol_specific_qqq_debit_range(self):
        position = self._position()
        position.update({
            "symbol": "QQQ",
            "entryAsk": 0.50,
            "entryBid": 0.49,
            "entryCost": 50.0,
        })
        passed, reasons = photon.alert_quality_gate(position)
        self.assertTrue(passed, reasons)


class TrendConfluenceTests(unittest.TestCase):
    @staticmethod
    def _orb_day(close: float = 100.20):
        day = []
        for index in range(12):
            bar_close = 99.80 if index < 6 else close
            day.append({
                "date": "2026-09-25",
                "time": 930 + index * 5,
                "open": bar_close - 0.02,
                "high": 100.00 if index < 6 else bar_close + 0.03,
                "low": 99.50 if index < 6 else bar_close - 0.03,
                "close": bar_close,
                "volume": 1000.0,
            })
        return day

    @staticmethod
    def _trend_setup(*, relative_volume=True, orb_breakout=True):
        bull_checks = {
            "ema_trend": True,
            "vwap": True,
            "supertrend": False,
            "macd": True,
            "rsi": True,
            "momentum": True,
            "relative_volume": relative_volume,
            "turtle_breakout": False,
            "squeeze_breakout": False,
            "momentum_acceleration": False,
        }
        return {
            "direction": None,
            "rawDirection": None,
            "score": 6 if relative_volume else 5,
            "independentScore": 6 if relative_volume else 5,
            "bullScore": 6 if relative_volume else 5,
            "bearScore": 2,
            "qualificationBullScore": 6 if relative_volume else 5,
            "qualificationBearScore": 2,
            "close": 100.20,
            "vwap": 100.00,
            "rsi": 60.0,
            "momentumPct": 0.20,
            "relativeVolume": 1.20 if relative_volume else 0.50,
            "bullChecks": bull_checks,
            "bearChecks": {key: False for key in bull_checks},
            "orbContext": {
                "ready": True,
                "orbHigh": 100.00,
                "orbLow": 99.50,
                "rangePct": 0.50,
                "bullBreakout": orb_breakout,
                "bearBreakout": False,
                "breakoutPct": 0.20 if orb_breakout else 0.0,
            },
            "turtle": {"longBreakout": False, "shortBreakout": False, "n": 1.0},
            "rejectionReasons": ["score_below_7"],
        }

    def test_opening_range_context_requires_a_confirmed_close_beyond_range(self):
        self.assertFalse(engine.opening_range_context(self._orb_day()[:6])["ready"])
        self.assertTrue(engine.opening_range_context(self._orb_day()[:7])["ready"])
        context = engine.opening_range_context(self._orb_day())

        self.assertTrue(context["ready"])
        self.assertTrue(context["bullBreakout"])
        self.assertFalse(context["bearBreakout"])
        self.assertEqual(100.0, context["orbHigh"])
        self.assertGreaterEqual(context["breakoutPct"], 0.10)

    def test_trend_confluence_uses_ema_macd_volume_and_orb_without_lowering_base_gate(self):
        setup = engine.apply_trend_confluence_gate(
            self._trend_setup(), symbol="SPY", bar_time=1100,
        )

        self.assertEqual("bullish", setup["direction"])
        self.assertTrue(setup["trendConfluence"])
        self.assertEqual("orb_trend_confluence", setup["entryStructure"])
        self.assertEqual(6, setup["trendEntryScoreFloor"])
        self.assertEqual("bullish", engine.apply_symbol_entry_score_gate(setup, symbol="SPY")["direction"])
        self.assertEqual("bullish", engine.apply_structural_entry_gate(setup)["direction"])
        self.assertIsNone(engine.entry_directional_drift_reason(setup, 100.15))

    def test_trend_confluence_requires_directional_momentum(self):
        # V7.5.3 (operator-approved 2026-10-12): momentum is a core requirement.
        # Exact-release paper evidence: momentum-failed ORB entries 3W/7L,
        # -$51 (PF 0.45); momentum-confirmed entries 7W/5L, +$85 (PF 2.27).
        setup = self._trend_setup()
        setup["bullChecks"]["momentum"] = False

        result = engine.apply_trend_confluence_gate(
            setup, symbol="SPY", bar_time=1100,
        )

        self.assertIsNone(result["direction"])
        self.assertFalse(result.get("trendConfluence"))

    def test_trend_confluence_momentum_plus_one_supporting_check_qualifies(self):
        setup = self._trend_setup()
        setup["bullChecks"]["rsi"] = False

        result = engine.apply_trend_confluence_gate(
            setup, symbol="SPY", bar_time=1100,
        )

        self.assertEqual("bullish", result["direction"])
        self.assertEqual(["momentum", "vwap"], result["trendSupportingChecks"])

    def test_trend_confluence_fails_closed_without_volume_or_orb_confirmation(self):
        no_volume = engine.apply_trend_confluence_gate(
            self._trend_setup(relative_volume=False), symbol="SPY", bar_time=1100,
        )
        no_orb = engine.apply_trend_confluence_gate(
            self._trend_setup(orb_breakout=False), symbol="SPY", bar_time=1100,
        )

        self.assertIsNone(no_volume["direction"])
        self.assertIsNone(no_orb["direction"])

    def test_trend_confluence_does_not_bypass_rsi_or_anti_chase_safety(self):
        rsi_extreme = self._trend_setup()
        rsi_extreme["rsi"] = 75.0
        chased = self._trend_setup()
        chased["rejectionReasons"] = ["bullish_chase_above_2.0_vwap_sigma"]

        rsi_result = engine.apply_trend_confluence_gate(
            rsi_extreme, symbol="SPY", bar_time=1100,
        )
        chase_result = engine.apply_trend_confluence_gate(
            chased, symbol="SPY", bar_time=1100,
        )

        self.assertIsNone(rsi_result["direction"])
        self.assertIsNone(chase_result["direction"])

    def test_orb_trend_and_squeeze_entries_use_structural_exit_class(self):
        trend_position = {
            "direction": "bullish",
            "optionType": "call",
            "entryAsk": 1.0,
            "turtleN": 1.0,
            "signal": self._trend_setup(),
        }
        trend_position["signal"] = engine.apply_trend_confluence_gate(
            trend_position["signal"], symbol="SPY", bar_time=1100,
        )
        squeeze_position = {
            "direction": "bullish",
            "optionType": "call",
            "entryAsk": 1.0,
            "signal": {
                "direction": "bullish",
                "bullChecks": {"squeeze_breakout": True},
                "turtle": {"longBreakout": False},
            },
        }

        self.assertFalse(engine._is_nonstructural_signal(trend_position))
        self.assertFalse(engine._is_nonstructural_signal(squeeze_position))
        self.assertGreater(
            engine.profit_target_pct_for_position(trend_position),
            engine.FAST_PROFIT_TARGET_PCT,
        )




class QuoteValidationTests(unittest.TestCase):
    NOW = datetime(2026, 8, 7, 15, 0, tzinfo=engine.ET)

    def test_crossed_market_is_rejected(self):
        passed, reason = engine.quality_gate({
            "occ_symbol": "SPY260811C00100000",
            "expiry": "2026-08-11",
            "strike": 100.0,
            "bid": 1.20,
            "ask": 1.00,
        }, "bullish", 100.0)
        self.assertFalse(passed)
        self.assertEqual("crossed_quote", reason)

    def test_six_percent_entry_spread_is_rejected(self):
        passed, reason = engine.quality_gate({
            "occ_symbol": "SPY260811C00100000",
            "expiry": "2026-08-11",
            "strike": 100.0,
            "bid": 0.94,
            "ask": 1.00,
        }, "bullish", 100.0)
        self.assertFalse(passed)
        self.assertTrue(reason.startswith("wide_spread_"), reason)

    def test_normalized_open_interest_field_passes_liquidity_gate(self):
        passed, reason = engine.quality_gate({
            "occ_symbol": "SPY260811C00100000",
            "expiry": "2026-08-11",
            "strike": 100.0,
            "bid": 1.25,
            "ask": 1.30,
            "open_interest": 1000,
            "volume": 100,
        }, "bullish", 100.0)
        self.assertTrue(passed, reason)
        self.assertEqual("ok", reason)

    def test_broker_native_open_interest_fallback_passes_liquidity_gate(self):
        passed, reason = engine.quality_gate({
            "occ_symbol": "SPY260811C00100000",
            "expiry": "2026-08-11",
            "strike": 100.0,
            "bid": 1.25,
            "ask": 1.30,
            "openInterest": 1000,
            "volume": 100,
        }, "bullish", 100.0)
        self.assertTrue(passed, reason)
        self.assertEqual("ok", reason)

    def test_non_finite_option_liquidity_is_rejected(self):
        for field, value in (
            ("open_interest", float("nan")),
            ("open_interest", float("inf")),
            ("volume", float("nan")),
            ("volume", float("inf")),
        ):
            contract = {
                "occ_symbol": "SPY260811C00100000",
                "expiry": "2026-08-11",
                "strike": 100.0,
                "bid": 0.99,
                "ask": 1.00,
                "open_interest": 100,
                "volume": 100,
            }
            contract[field] = value
            with self.subTest(field=field, value=value):
                passed, reason = engine.quality_gate(contract, "bullish", 100.0)
                self.assertFalse(passed, reason)

    def test_contract_identity_rejects_wrong_option_type(self):
        passed, reason = engine.validate_contract_identity({
            "occ_symbol": "SPY260810P00772000",
            "expiry": "2026-08-10",
            "strike": 772.0,
            "bid": 1.60,
            "ask": 1.62,
        }, expected_symbol="SPY", expected_option_type="call", expected_dte=1, now=self.NOW)
        self.assertFalse(passed)
        self.assertEqual("option_type_mismatch", reason)

    def _metadata(self, **overrides):
        metadata = {
            "kind": "option_candidates",
            "symbol": "SPY",
            "source": "Robinhood MCP",
            "timestamp": "2026-08-07T19:00:00Z",
        }
        metadata.update(overrides)
        return metadata

    def _validate_metadata(self, metadata):
        return engine.validate_quote_metadata(
            metadata,
            expected_kind="option_candidates",
            expected_symbol="SPY",
            max_age_seconds=60,
            now=self.NOW,
        )

    def test_quote_metadata_rejects_stale_timestamp(self):
        passed, reason = self._validate_metadata(self._metadata(timestamp="2026-08-07T18:58:00Z"))
        self.assertFalse(passed)
        self.assertEqual("stale_quote", reason)

    def test_quote_metadata_rejects_untrusted_source(self):
        passed, reason = self._validate_metadata(self._metadata(source="model estimate"))
        self.assertFalse(passed)
        self.assertEqual("untrusted_quote_source", reason)

    def test_quote_metadata_rejects_wrong_payload_kind(self):
        passed, reason = self._validate_metadata(self._metadata(kind="equity_quote"))
        self.assertFalse(passed)
        self.assertEqual("quote_kind_mismatch", reason)

    def test_quote_metadata_rejects_wrong_symbol(self):
        passed, reason = self._validate_metadata(self._metadata(symbol="QQQ"))
        self.assertFalse(passed)
        self.assertEqual("quote_symbol_mismatch", reason)

    def test_entry_candidate_integration_accepts_only_valid_bound_contract(self):
        candidates = self._metadata(dte_target=1, call={
            "occ_symbol": "SPY260810C00774000",
            "expiry": "2026-08-10",
            "strike": 774.0,
            "bid": 1.60,
            "ask": 1.62,
            "open_interest": 1000,
            "volume": 100,
            "instrument_id": "rh-spy-call-774",
        })
        contract, reason = engine.select_valid_entry_contract(
            candidates, direction="bullish", underlying=773.0, now=self.NOW,
        )
        self.assertIs(contract, candidates["call"])
        self.assertEqual("ok", reason)

    def test_entry_candidate_without_nonempty_instrument_id_is_rejected(self):
        candidates = self._metadata(dte_target=1, call={
            "occ_symbol": "SPY260810C00774000",
            "expiry": "2026-08-10",
            "strike": 774.0,
            "bid": 1.60,
            "ask": 1.62,
            "open_interest": 1000,
            "volume": 100,
            "instrument_id": "",
        })
        contract, reason = engine.select_valid_entry_contract(
            candidates, direction="bullish", underlying=773.0, now=self.NOW,
        )
        self.assertIsNone(contract)
        self.assertEqual("missing_instrument_id", reason)

    def test_zero_bid_is_valid_fail_safe_exit_mark(self):
        bid, reason = engine.validate_exit_quote({
            "kind": "option_quote",
            "source": "Robinhood MCP",
            "timestamp": "2026-08-07T19:00:00Z",
            "occ_symbol": "SPY260810C00774000",
            "bid": 0.0,
            "ask": 0.01,
        }, expected_occ_symbol="SPY260810C00774000", now=self.NOW)
        self.assertEqual(0.0, bid)
        self.assertEqual("ok", reason)

    def test_quote_age_uses_provider_event_timestamp(self):
        age = engine.quote_freshness_seconds(
            {"timestamp": "2026-08-07T18:59:59.250Z"}, now=self.NOW,
        )
        self.assertEqual(0.75, age)


class DeterministicBridgeTests(unittest.TestCase):
    def test_sync_mcp_discovery_is_not_wrapped_as_a_coroutine(self):
        discovered = quote_bridge._run_discovery(lambda: ["read_only_tool"])
        self.assertEqual(["read_only_tool"], discovered)

    def test_equity_midpoint_timestamp_uses_older_book_side(self):
        timestamp = quote_bridge._equity_quote_timestamp({
            "venue_bid_time": "2026-08-07T19:59:59.100Z",
            "venue_ask_time": "2026-08-07T19:59:59.900Z",
            "venue_last_non_reg_trade_time": "2026-08-07T20:00:01Z",
        }, "fallback")
        self.assertEqual("2026-08-07T19:59:59.100Z", timestamp)

    def test_equity_wrapper_composes_with_engine_metadata_validator(self):
        raw = {"data": {"results": [{"quote": {
            "symbol": "SPY",
            "bid_price": "771.00",
            "ask_price": "771.08",
            "venue_bid_time": "2026-08-07T19:59:59.100Z",
            "venue_ask_time": "2026-08-07T19:59:59.900Z",
        }}]}}
        with patch.object(quote_bridge, "mcp_call", return_value=raw):
            payload = quote_bridge.quote_equity("SPY")

        passed, reason = engine.validate_quote_metadata(
            payload,
            expected_kind="equity_quote",
            expected_symbol="SPY",
            max_age_seconds=5,
            now=datetime(2026, 8, 7, 16, 0, tzinfo=engine.ET),
        )
        self.assertTrue(passed, reason)

    def test_equity_wrapper_supports_qqq_with_identity_validation(self):
        raw = {"data": {"results": [{"quote": {
            "symbol": "QQQ",
            "bid_price": "692.00",
            "ask_price": "692.04",
            "venue_bid_time": "2026-08-13T15:00:00.100Z",
            "venue_ask_time": "2026-08-13T15:00:00.200Z",
        }}]}}
        with patch.object(quote_bridge, "mcp_call", return_value=raw) as call:
            payload = quote_bridge.quote_equity("QQQ")

        call.assert_called_once_with("get_equity_quotes", {"symbols": ["QQQ"]})
        self.assertEqual("QQQ", payload["symbol"])
        self.assertEqual("QQQ", payload["quote"]["symbol"])

    def test_qqq_exact_instrument_quote_accepts_qqq_occ_identity(self):
        calls = []

        def caller(method, arguments):
            calls.append((method, arguments))
            return {"data": {"results": [{"quote": {
                "instrument_id": "qqq-instrument-733-call",
                "bid_price": "0.72",
                "ask_price": "0.75",
                "mark_price": "0.735",
                "updated_at": "2026-08-13T15:00:00Z",
            }}]}}

        payload = quote_bridge.build_option_quote_by_instrument(
            "qqq-instrument-733-call",
            "QQQ260814C00733000",
            caller=caller,
        )

        self.assertEqual("QQQ", payload["symbol"])
        self.assertEqual("QQQ260814C00733000", payload["occ_symbol"])
        self.assertEqual(
            [("get_option_quotes", {"instrument_ids": ["qqq-instrument-733-call"]})],
            calls,
        )

    def test_mcp_dispatch_envelope_is_normalized(self):
        raw = json.dumps({
            "result": json.dumps({"data": {"results": [{"quote": {"symbol": "SPY"}}]}}),
            "structuredContent": {"data": {"results": [{"quote": {"symbol": "SPY"}}]}},
        })
        dispatcher = Mock(return_value=raw)
        payload = quote_bridge.mcp_call("get_equity_quotes", {"symbols": ["SPY"]}, dispatcher=dispatcher)
        self.assertEqual("SPY", payload["data"]["results"][0]["quote"]["symbol"])
        dispatcher.assert_called_once_with(
            "mcp__robinhood_trading__get_equity_quotes", {"symbols": ["SPY"]}
        )

    def test_engine_option_quote_flag_is_supported(self):
        args = quote_bridge.build_parser().parse_args([
            "--option-quote", "SPY260810C00774000"
        ])
        self.assertEqual("SPY260810C00774000", args.option)

    def test_scalp_candidate_wrapper_normalizes_for_engine(self):
        raw = {
            "source": "robinhood_mcp",
            "timestamp": "2026-08-07T15:00:00-04:00",
            "underlying": 773.20,
            "expirationDate": "2026-08-10",
            "candidates": [{
                "symbol": "SPY",
                "occSymbol": "SPY260810C00774000",
                "instrumentId": "instrument-774-call",
                "type": "call",
                "strike": 774.0,
                "expirationDate": "2026-08-10",
                "bid": 1.45,
                "ask": 1.49,
                "mark": 1.47,
                "updatedAt": "2026-08-07T19:59:59Z",
            }],
        }
        with patch.object(quote_bridge, "build_scalp_candidates", return_value=raw):
            payload = quote_bridge.quote_scalp_candidate(
                "SPY", "call", 773.20, 1, 75.0, 175.0, 0.60,
            )

        self.assertEqual("Robinhood MCP", payload["source"])
        self.assertEqual("option_candidates", payload["kind"])
        self.assertEqual("SPY", payload["symbol"])
        self.assertEqual(1, payload["dte_target"])
        self.assertEqual("SPY260810C00774000", payload["call"]["occ_symbol"])
        self.assertEqual("instrument-774-call", payload["call"]["instrument_id"])
        self.assertEqual("2026-08-10", payload["call"]["expiry"])
        self.assertEqual("2026-08-07T19:59:59Z", payload["timestamp"])

    def test_qqq_scalp_candidate_wrapper_passes_symbol_to_builder(self):
        raw = {
            "source": "robinhood_mcp",
            "timestamp": "2026-08-13T15:00:00Z",
            "underlying": 732.53,
            "expirationDate": "2026-08-14",
            "candidates": [{
                "symbol": "QQQ",
                "occSymbol": "QQQ260814C00733000",
                "instrumentId": "qqq-instrument-733-call",
                "type": "call",
                "strike": 733.0,
                "expirationDate": "2026-08-14",
                "bid": 0.72,
                "ask": 0.75,
                "mark": 0.735,
                "updatedAt": "2026-08-13T15:00:00Z",
            }],
        }
        with patch.object(quote_bridge, "build_scalp_candidates", return_value=raw) as builder:
            payload = quote_bridge.quote_scalp_candidate(
                "QQQ", "call", 732.53, 1, 30.0, 100.0, 0.60,
            )

        builder.assert_called_once_with(
            symbol="QQQ", option_type="call", underlying=732.53, dte=1,
            min_debit=30.0, max_debit=100.0, max_otm_pct=0.60,
        )
        self.assertEqual("QQQ", payload["symbol"])
        self.assertEqual("QQQ260814C00733000", payload["call"]["occ_symbol"])

    def test_exact_instrument_quote_avoids_chain_rebuild(self):
        calls = []

        def caller(method, arguments):
            calls.append((method, arguments))
            if method == "get_option_quotes":
                return {"data": {"results": [{"quote": {
                    "instrument_id": "instrument-774-call",
                    "bid_price": "1.62",
                    "ask_price": "1.65",
                    "mark_price": "1.635",
                    "updated_at": "2026-08-07T20:00:00Z",
                }}]}}
            self.fail(f"unexpected method {method}")

        payload = quote_bridge.build_option_quote_by_instrument(
            "instrument-774-call",
            "SPY260810C00774000",
            caller=caller,
        )

        self.assertEqual(
            [("get_option_quotes", {"instrument_ids": ["instrument-774-call"]})],
            calls,
        )
        self.assertEqual("Robinhood MCP", payload["source"])
        self.assertEqual("option_quote", payload["kind"])
        self.assertEqual("SPY260810C00774000", payload["occ_symbol"])
        self.assertEqual("instrument-774-call", payload["instrument_id"])
        self.assertEqual(1.62, payload["bid"])
        self.assertEqual("2026-08-07T20:00:00Z", payload["timestamp"])

    def test_monitor_bridge_uses_saved_instrument_id(self):
        args = engine.option_quote_bridge_args({
            "occSymbol": "SPY260810C00774000",
            "instrumentId": "instrument-774-call",
        })
        self.assertEqual(
            ("--option", "SPY260810C00774000", "--instrument-id", "instrument-774-call"),
            args,
        )

    def test_dte_uses_eastern_session_date_not_utc_date(self):
        from datetime import timezone
        sunday_evening_et_as_utc = datetime(2026, 8, 10, 0, 4, tzinfo=timezone.utc)
        chains = [{
            "id": "chain-1", "symbol": "SPY",
            "expiration_dates": ["2026-08-10", "2026-08-11"],
        }]
        _, expiration = quote_bridge._target_expiration(chains, sunday_evening_et_as_utc, 1)
        self.assertEqual("2026-08-10", expiration)

    @unittest.skip("XSP removed from active universe")
    def test_one_dte_skips_labor_day_market_holiday(self):
        friday_before_labor_day = datetime(2026, 9, 4, 11, 0, tzinfo=engine.ET)
        chains = [{
            "id": "xsp-chain", "symbol": "XSP",
            "expiration_dates": ["2026-09-04", "2026-09-08", "2026-09-09"],
        }]
        _, expiration = quote_bridge._target_expiration(
            chains, friday_before_labor_day, 1, "XSP"
        )
        self.assertEqual("2026-09-08", expiration)

    def test_exact_occ_quote_joins_verified_instrument_identity(self):
        now = datetime(2026, 8, 9, 19, 0, tzinfo=engine.ET)

        def caller(method, arguments):
            if method == "get_option_chains":
                return {"data": {"chains": [{"id": "chain-1", "symbol": "SPY", "expiration_dates": ["2026-08-10"]}]}}
            if method == "get_option_instruments":
                return {"data": {"instruments": [{
                    "id": "i-774", "chain_id": "chain-1", "chain_symbol": "SPY",
                    "expiration_date": "2026-08-10", "strike_price": "774",
                    "type": "call", "state": "active", "tradability": "tradable",
                }]}}
            if method == "get_option_quotes":
                return {"data": {"results": [{"quote": {
                    "instrument_id": "i-774", "ask_price": "1.49", "bid_price": "1.46",
                    "mark_price": "1.475", "updated_at": "2026-08-07T20:14:59Z",
                }}]}}
            self.fail(f"unexpected method {method}")

        payload = quote_bridge.build_option_quote(
            "SPY260810C00774000", caller=caller, observed_at=now
        )
        quote = payload["quote"]
        self.assertEqual("SPY260810C00774000", quote["occSymbol"])
        self.assertEqual("i-774", quote["instrumentId"])
        self.assertEqual("robinhood_mcp", quote["quoteSource"])

    def test_scalp_candidates_join_instrument_identity_to_quotes(self):
        now = datetime(2026, 8, 9, 19, 0, tzinfo=engine.ET)

        def caller(method, arguments):
            if method == "get_equity_quotes":
                return {"data": {"results": [{"quote": {"symbol": "SPY", "last_trade_price": "773.20", "venue_last_trade_time": "2026-08-07T19:59:59Z"}}]}}
            if method == "get_option_chains":
                return {"data": {"chains": [{"id": "chain-1", "symbol": "SPY", "expiration_dates": ["2026-08-10"]}]}}
            if method == "get_option_instruments":
                return {"data": {"instruments": [
                    {"id": "i-774", "chain_id": "chain-1", "chain_symbol": "SPY", "expiration_date": "2026-08-10", "strike_price": "774", "type": "call", "state": "active", "tradability": "tradable"},
                    {"id": "i-780", "chain_id": "chain-1", "chain_symbol": "SPY", "expiration_date": "2026-08-10", "strike_price": "780", "type": "call", "state": "active", "tradability": "tradable"},
                ]}}
            if method == "get_option_quotes":
                self.assertEqual(["i-774"], arguments["instrument_ids"])
                return {"data": {"results": [{"quote": {
                    "instrument_id": "i-774", "ask_price": "1.49", "bid_price": "1.46",
                    "mark_price": "1.475", "updated_at": "2026-08-07T20:14:59Z",
                    "delta": "0.43", "volume": 63221, "open_interest": 2018,
                }}]}}
            self.fail(f"unexpected method {method}")

        payload = quote_bridge.build_scalp_candidates(
            option_type="call", underlying=773.20, dte=1,
            min_debit=75.0, max_debit=175.0, max_otm_pct=0.60,
            caller=caller, observed_at=now,
        )
        self.assertEqual("robinhood_mcp", payload["source"])
        self.assertEqual(1, len(payload["candidates"]))
        candidate = payload["candidates"][0]
        self.assertEqual("SPY260810C00774000", candidate["occSymbol"])
        self.assertEqual("i-774", candidate["instrumentId"])
        self.assertEqual(1.49, candidate["ask"])
        self.assertEqual("2026-08-07T20:14:59Z", candidate["updatedAt"])

    def test_bridge_rejects_non_allowlisted_mcp_method_before_dispatch(self):
        dispatcher = Mock()
        with self.assertRaises(quote_bridge.BridgeError):
            quote_bridge.mcp_call("place_option_order", {}, dispatcher=dispatcher)
        dispatcher.assert_not_called()


@unittest.skip("XSP removed from active universe")
class XspParityQuoteTests(unittest.TestCase):
    def test_xsp_parity_quote_uses_matched_option_legs_without_equity_endpoint(self):
        """XSP spot must be derived from verified call/put books, not an ETF quote."""
        calls = []
        now = datetime(2026, 8, 9, 20, 20, tzinfo=engine.ET)

        def caller(method, arguments):
            calls.append((method, arguments))
            if method == "get_option_chains":
                return {"data": {"chains": [{
                    "id": "xsp-chain", "symbol": "XSP", "expiration_dates": ["2026-08-10"],
                }]}}
            if method == "get_option_instruments":
                option_type = arguments["type"]
                return {"data": {"instruments": [{
                    "id": f"xsp-{option_type}-760", "chain_id": "xsp-chain",
                    "chain_symbol": "XSP", "expiration_date": "2026-08-10",
                    "strike_price": "760", "type": option_type, "state": "active",
                    "tradability": "tradable",
                }]}}
            if method == "get_option_quotes":
                instrument_id = arguments["instrument_ids"][0]
                is_call = "call" in instrument_id
                return {"data": {"results": [{"quote": {
                    "instrument_id": instrument_id,
                    "bid_price": "0.95" if is_call else "0.80",
                    "ask_price": "1.00" if is_call else "0.85",
                    "mark_price": "0.975" if is_call else "0.825",
                    "updated_at": "2026-08-10T00:20:00Z",
                }}]}}
            self.fail(f"unexpected method {method}")

        payload = quote_bridge.build_index_quote(
            "XSP", reference_underlying=760.0, dte=1, caller=caller, observed_at=now,
        )

        self.assertEqual("index_quote", payload["kind"])
        self.assertEqual("XSP", payload["symbol"])
        self.assertEqual(760.10, payload["quote"]["bid_price"])
        self.assertEqual(760.20, payload["quote"]["ask_price"])
        self.assertEqual(760.15, payload["quote"]["mark_price"])
        self.assertEqual("760.0", payload["pair"]["strike"])
        self.assertNotIn("get_equity_quotes", [method for method, _arguments in calls])
        passed, reason = engine.validate_quote_metadata(
            payload, expected_kind="index_quote", expected_symbol="XSP", max_age_seconds=5, now=now,
        )
        self.assertTrue(passed, reason)

    def test_xsp_candidate_selection_uses_parity_spot_without_equity_quote(self):
        calls = []
        now = datetime(2026, 8, 9, 20, 20, tzinfo=engine.ET)

        def caller(method, arguments):
            calls.append((method, arguments))
            if method == "get_option_chains":
                return {"data": {"chains": [{
                    "id": "xsp-chain", "symbol": "XSP", "expiration_dates": ["2026-08-10"],
                }]}}
            if method == "get_option_instruments":
                option_type = arguments["type"]
                return {"data": {"instruments": [{
                    "id": f"xsp-{option_type}-760", "chain_id": "xsp-chain",
                    "chain_symbol": "XSP", "expiration_date": "2026-08-10",
                    "strike_price": "760", "type": option_type, "state": "active",
                    "tradability": "tradable",
                }]}}
            if method == "get_option_quotes":
                instrument_id = arguments["instrument_ids"][0]
                is_call = "call" in instrument_id
                return {"data": {"results": [{"quote": {
                    "instrument_id": instrument_id,
                    "bid_price": "0.95" if is_call else "0.80",
                    "ask_price": "1.00" if is_call else "0.85",
                    "mark_price": "0.975" if is_call else "0.825",
                    "updated_at": "2026-08-10T00:20:00Z",
                }}]}}
            self.fail(f"unexpected method {method}")

        raw = quote_bridge.build_scalp_candidates(
            symbol="XSP", option_type="call", underlying=760.0, dte=1,
            min_debit=75.0, max_debit=175.0, max_otm_pct=0.60,
            caller=caller, observed_at=now,
        )

        self.assertEqual(760.15, raw["underlying"])
        self.assertEqual("index_quote", raw["underlyingQuote"]["kind"])
        self.assertEqual(1, len(raw["candidates"]))
        self.assertNotIn("get_equity_quotes", [method for method, _arguments in calls])

    def test_xsp_candidate_payload_carries_index_quote_provenance(self):
        raw = {
            "underlying": 760.15,
            "equityQuote": {"symbol": "XSP", "bid_price": 760.10, "ask_price": 760.20},
            "underlyingQuote": {
                "kind": "index_quote", "symbol": "XSP", "timestamp": "2026-08-10T00:20:00Z",
                "quote": {"symbol": "XSP", "bid_price": 760.10, "ask_price": 760.20},
            },
            "chainId": "xsp-chain", "expirationDate": "2026-08-10",
            "candidates": [{
                "symbol": "XSP", "occSymbol": "XSP260810C00760000", "instrumentId": "xsp-call-760",
                "type": "call", "strike": 760.0, "expirationDate": "2026-08-10",
                "bid": 0.95, "ask": 1.00, "mark": 0.975,
                "updatedAt": "2026-08-10T00:20:00Z",
            }],
        }
        with patch.object(quote_bridge, "build_scalp_candidates", return_value=raw):
            payload = quote_bridge.quote_scalp_candidate(
                "XSP", "call", 760.0, 1, 75.0, 175.0, 0.60,
            )
        self.assertEqual("index_quote", payload["underlying_quote_kind"])
        self.assertEqual("index_quote", payload["underlying_quote"]["kind"])
        self.assertEqual("XSP", payload["underlying_quote"]["symbol"])


@unittest.skip("XSP removed from active universe")
class XspQuoteWorkerTests(unittest.TestCase):
    def test_worker_index_operation_uses_explicit_reference_and_never_equity_op(self):
        expected = {"kind": "index_quote", "symbol": "XSP"}
        with patch.object(quote_bridge, "quote_index", return_value=expected) as quote_index:
            payload = quote_worker.dispatch({
                "op": "index", "symbol": "XSP", "reference_underlying": 760.0, "dte": 1,
            })
        self.assertEqual(expected, payload)
        quote_index.assert_called_once_with("XSP", reference_underlying=760.0, dte=1)


class ProvenanceTests(unittest.TestCase):
    def test_scan_diagnostic_records_gate_funnel_and_release(self):
        result = {
            "action": "SKIP", "reason": "no_qualified_signal",
            "signal": {
                "detector": "normal", "score": 4, "relativeVolume": 0.74,
                "direction": None, "rawDirection": "bullish",
                "signalBarStart": "2026-08-07T14:30:00-04:00",
                "signalBarEnd": "2026-08-07T14:35:00-04:00",
                "rejectionReasons": ["hard_gate:relative_volume_below_0.75"],
            },
        }
        record = photon.build_scan_diagnostic(result, exit_code=0)
        self.assertEqual("signal", record["stage"])
        self.assertFalse(record["gates"]["score"])
        self.assertFalse(record["gates"]["relativeVolume"])
        self.assertEqual(engine.ENGINE_VERSION, record["engineVersion"])
        self.assertEqual(64, len(record["sourceCodeSha256"]))
        self.assertTrue(record["opportunityEpisodeId"])

    def test_scan_audit_records_qqq_symbol(self):
        result = {
            "action": "SKIP", "reason": "no_qualified_signal",
            "signal": {
                "score": 0, "direction": None,
                "signalBarStart": "2026-08-13T11:05:00-04:00",
                "signalBarEnd": "2026-08-13T11:10:00-04:00",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "scan-attempts.jsonl"
            with patch.object(photon, "SCAN_AUDIT", audit_path):
                photon._audit("", result, 0, symbol="QQQ")
            record = json.loads(audit_path.read_text())

        self.assertEqual("QQQ", record["symbol"])
        self.assertEqual("QQQ", record["diagnostic"]["symbol"])

    def test_qqq_entry_alert_labels_underlying_as_qqq(self):
        position = {
            "symbol": "QQQ", "optionType": "call", "strike": 733.0,
            "expiry": "2026-08-14", "entryUnderlying": 732.53,
            "entryAsk": 0.75, "entryCost": 75.0,
            "signal": {
                "score": 7, "ema9": 733.0, "ema21": 732.0,
                "macdHist": 0.1, "supertrend": "up", "close": 732.53,
                "vwap": 732.0, "rsi": 55.0, "relativeVolume": 1.2,
                "momentumPct": 0.1,
            },
        }
        alert = photon.format_entry_alert(position)
        self.assertIn("QQQ $732.53", alert)
        self.assertNotIn("SPY $732.53", alert)

    def test_bridge_preserves_snapshot_path(self):
        completed = __import__("subprocess").CompletedProcess(
            args=["bridge"],
            returncode=0,
            stdout='{"ok":true,"data":{"kind":"option_quote"},"snapshot":"/tmp/quote.json"}\n',
            stderr="",
        )
        with patch.object(engine.subprocess, "run", return_value=completed):
            payload = engine.bridge("--option", "SPY260810C00774000")
        self.assertEqual("/tmp/quote.json", payload["_snapshotPath"])

    def test_every_ledger_event_has_release_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "events.jsonl"
            with patch.object(engine, "log_path", return_value=ledger):
                engine.log({"event": "TEST_EVENT"})
            event = __import__("json").loads(ledger.read_text())
        self.assertTrue(event["eventId"])
        self.assertEqual(engine.ENGINE_VERSION, event["engineVersion"])
        self.assertEqual(engine.STRATEGY_ID, event["strategyId"])
        self.assertEqual(64, len(event["sourceCodeSha256"]))
        self.assertEqual(64, len(event["configSha256"]))

    def test_config_hash_includes_qqq_contract_config(self):
        baseline = engine.config_sha256()
        qqq = engine.SYMBOLS["QQQ"]
        with patch.dict(
            engine.SYMBOLS,
            {"QQQ": replace(qqq, max_debit=qqq.max_debit + 1.0)},
        ):
            self.assertNotEqual(baseline, engine.config_sha256())

    def test_config_hash_includes_loss_cooldown_gate(self):
        baseline = engine.config_sha256()
        with patch.object(engine, "MIN_SCORE_AFTER_LOSS", engine.MIN_SCORE_AFTER_LOSS + 1):
            self.assertNotEqual(baseline, engine.config_sha256())


@unittest.skip("XSP removed from active universe")
class XspSessionGateTests(unittest.TestCase):
    @staticmethod
    def _block(now, state):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(engine, "calendar_block", return_value=None),
                patch.object(engine, "KILL_SWITCH", Path(tmp) / "missing-kill-switch.json"),
            ):
                return engine.market_block(now, state, symbol="XSP")

    def test_xsp_session_key_bridges_sunday_overnight_to_monday(self):
        sunday_evening = datetime(2026, 8, 9, 20, 20, tzinfo=engine.ET)
        monday_morning = datetime(2026, 8, 10, 8, 55, tzinfo=engine.ET)

        self.assertEqual("2026-08-10", engine.trading_session_key("XSP", sunday_evening))
        self.assertEqual("2026-08-10", engine.trading_session_key("XSP", monday_morning))

    def test_xsp_market_gate_allows_only_broker_supported_session_windows(self):
        state = engine.default_state("XSP")
        self.assertEqual("market_closed", self._block(datetime(2026, 8, 9, 20, 20, tzinfo=engine.ET), state))
        self.assertEqual("market_closed", self._block(datetime(2026, 8, 10, 8, 55, tzinfo=engine.ET), state))
        self.assertIsNone(self._block(datetime(2026, 8, 10, 9, 35, tzinfo=engine.ET), state))
        self.assertIsNone(self._block(datetime(2026, 8, 10, 16, 14, tzinfo=engine.ET), state))

        self.assertEqual("market_closed", self._block(datetime(2026, 8, 10, 9, 25, tzinfo=engine.ET), state))
        self.assertEqual("market_closed", self._block(datetime(2026, 8, 10, 16, 15, tzinfo=engine.ET), state))
        self.assertEqual("market_closed", self._block(datetime(2026, 8, 10, 17, 0, tzinfo=engine.ET), state))
        self.assertEqual("market_closed", self._block(datetime(2026, 8, 10, 20, 14, tzinfo=engine.ET), state))
        self.assertEqual("market_closed", self._block(datetime(2026, 8, 14, 20, 20, tzinfo=engine.ET), state))

    def test_xsp_has_no_entry_count_cap_but_retains_daily_loss_cap(self):
        state = engine.default_state("XSP")
        state["dailyTrades"] = 99
        now = datetime(2026, 8, 10, 10, 0, tzinfo=engine.ET)
        self.assertIsNone(self._block(now, state))

        state["dailyGrossLoss"] = engine.MAX_DAILY_LOSS
        self.assertEqual("daily_loss_limit", self._block(now, state))

    def test_xsp_hard_close_precedes_both_session_gaps(self):
        self.assertFalse(engine.hard_close_due(datetime(2026, 8, 10, 9, 19, tzinfo=engine.ET), "XSP"))
        self.assertTrue(engine.hard_close_due(datetime(2026, 8, 10, 9, 20, tzinfo=engine.ET), "XSP"))
        self.assertFalse(engine.hard_close_due(datetime(2026, 8, 10, 16, 54, tzinfo=engine.ET), "XSP"))
        self.assertTrue(engine.hard_close_due(datetime(2026, 8, 10, 16, 55, tzinfo=engine.ET), "XSP"))


@unittest.skip("XSP removed from active universe")
class XspSessionBarTests(unittest.TestCase):
    @staticmethod
    def _bar(when: datetime, close: float) -> dict:
        return {
            "dt": when, "date": when.astimezone(engine.ET).date().isoformat(),
            "time": when.astimezone(engine.ET).hour * 100 + when.astimezone(engine.ET).minute,
            "open": close - 0.1, "high": close + 0.2, "low": close - 0.2,
            "close": close, "volume": 1000.0,
        }

    def test_xsp_completed_bars_span_overnight_session_and_exclude_gaps(self):
        sunday = datetime(2026, 8, 9, tzinfo=engine.ET)
        monday = datetime(2026, 8, 10, tzinfo=engine.ET)
        bars = [
            self._bar(sunday.replace(hour=20, minute=10), 759.0),  # before XSP opens
            self._bar(sunday.replace(hour=20, minute=15), 760.0),
            self._bar(monday.replace(hour=8, minute=55), 761.0),
            self._bar(monday.replace(hour=9, minute=20), 762.0),
            self._bar(monday.replace(hour=9, minute=25), 763.0),  # exchange gap
            self._bar(monday.replace(hour=9, minute=30), 764.0),
        ]
        day = engine.completed_day_bars(
            bars, now=monday.replace(hour=9, minute=35), symbol="XSP",
        )
        self.assertEqual([2015, 855, 920, 930], [bar["time"] for bar in day])
        self.assertTrue(all(engine.xsp_bar_session_key(bar) == "2026-08-10" for bar in day))


@unittest.skip("XSP removed from active universe")
class XspSessionSignalTests(unittest.TestCase):
    @staticmethod
    def _bar(when: datetime, index: int) -> dict:
        close = 760.0 + index * 0.05
        return {
            "dt": when, "date": when.date().isoformat(),
            "time": when.hour * 100 + when.minute,
            "open": close - 0.03, "high": close + 0.08, "low": close - 0.08,
            "close": close, "volume": 1000.0 + index * 10.0,
        }

    def test_xsp_signal_uses_overnight_session_without_premarket_target(self):
        start = datetime(2026, 8, 10, 0, 0, tzinfo=engine.ET)
        bars = [self._bar(start + timedelta(minutes=5 * index), index) for index in range(40)]
        day = engine.completed_day_bars(bars, now=datetime(2026, 8, 10, 3, 25, tzinfo=engine.ET), symbol="XSP")
        warmup, relative_volume, source, context = engine.session_signal_inputs(
            bars, day, now=datetime(2026, 8, 10, 3, 25, tzinfo=engine.ET), symbol="XSP",
        )
        setup = engine.signal(
            day, warmup_bars=warmup, relative_volume=relative_volume,
            premarket_context=context, symbol="XSP",
        )
        self.assertEqual(40, setup["sessionBarsUsed"])
        self.assertNotIn("insufficient_bars", setup.get("rejectionReasons", []))
        self.assertIsNone(setup["premarketContext"])
        self.assertEqual("not_applicable_for_index_session", context["reason"])
        self.assertEqual("intraday_session", source)


@unittest.skip("XSP removed from active universe")
class XspRiskSessionTests(unittest.TestCase):
    def test_xsp_risk_counters_follow_the_cross_midnight_session(self):
        state = engine.default_state("XSP")
        state.update({
            "lastResetDate": "2026-08-10", "dailyTrades": 9,
            "dailyPnl": -21.0, "dailyLoss": 21.0, "dailyGrossLoss": 34.0,
        })
        sunday_open = datetime(2026, 8, 9, 20, 20, tzinfo=engine.ET)
        monday_overnight = datetime(2026, 8, 10, 0, 20, tzinfo=engine.ET)
        engine.reset_daily(state, symbol="XSP", now=sunday_open)
        self.assertEqual(("2026-08-10", 9, 34.0),
                         (state["lastResetDate"], state["dailyTrades"], state["dailyGrossLoss"]))
        engine.reset_daily(state, symbol="XSP", now=monday_overnight)
        self.assertEqual(("2026-08-10", 9, 34.0),
                         (state["lastResetDate"], state["dailyTrades"], state["dailyGrossLoss"]))

        state["lastResetDate"] = "2026-08-09"
        engine.reset_daily(state, symbol="XSP", now=sunday_open)
        self.assertEqual(("2026-08-10", 0, 0.0),
                         (state["lastResetDate"], state["dailyTrades"], state["dailyGrossLoss"]))


@unittest.skip("XSP removed from active universe")
class XspEngineQuoteTests(unittest.TestCase):
    def setUp(self):
        engine._LAST_LIVE_EQUITY_VENUE_TS.clear()

    @staticmethod
    def _setup(score: int) -> dict:
        return {
            "direction": None, "rawDirection": "bullish", "score": score,
            "relativeVolume": 1.2, "close": 760.0, "minTriggerMove": 0.02,
            "vwap": 759.0, "vwapUpper": 761.0, "vwapLower": 757.0, "rsi": 55.0,
            "bullScore": score, "bearScore": 1,
            "rejectionReasons": ["missing_fresh_bullish_trigger"],
            "turtle": {"longBreakout": True, "shortBreakout": False},
        }

    @staticmethod
    def _index_quote() -> dict:
        return {
            "kind": "index_quote", "source": "Robinhood MCP", "symbol": "XSP",
            "timestamp": "2026-08-10T14:00:00Z",
            "quote": {
                "symbol": "XSP", "bid_price": 760.10, "ask_price": 760.20,
                "updated_at": "2026-08-10T14:00:00Z",
            },
        }

    def test_xsp_live_trigger_accepts_index_quote_at_score_seven(self):
        now = datetime(2026, 8, 10, 10, 0, tzinfo=engine.ET)
        with patch.object(engine, "qualify_signal_direction", return_value=("bullish", "bullish", [])):
            confirmed = engine.apply_live_trigger_confirmation(
                self._setup(8), self._index_quote(), now=now, symbol="XSP",
            )
        self.assertEqual("bullish", confirmed["direction"])
        self.assertTrue(confirmed["liveTrigger"])

        self.assertTrue(engine.live_trigger_eligible(self._setup(7), symbol="XSP"))
        self.assertTrue(engine.live_trigger_eligible(self._setup(7), symbol="SPY"))

    def test_xsp_score_gate_applies_to_initial_and_post_quote_setups(self):
        setup = self._setup(7)
        setup["direction"] = "bullish"
        gated = engine.apply_symbol_entry_score_gate(setup, symbol="XSP")
        self.assertEqual("bullish", gated["direction"])
        self.assertNotIn("symbol_score_below_gate_8", gated["rejectionReasons"])
        self.assertEqual("bullish", engine.apply_symbol_entry_score_gate(setup, symbol="SPY")["direction"])
        self.assertIsNone(engine.post_quote_signal_failure(setup, setup, symbol="XSP"))

    def test_xsp_scan_transaction_passes_its_symbol_to_the_market_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime(2026, 8, 9, 20, 20, tzinfo=engine.ET)
            with (
                patch.object(engine, "ROOT", Path(tmp)),
                patch.object(engine, "now_et", return_value=now),
                patch.object(engine, "market_block", return_value="test_xsp_gate") as gate,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = engine._scan_transaction("XSP")
        self.assertEqual(0, code)
        self.assertEqual("XSP", gate.call_args.kwargs["symbol"])


@unittest.skip("XSP removed from active universe")
class XspCandidateProvenanceTests(unittest.TestCase):
    @staticmethod
    def _candidates(underlying_kind: str = "index_quote") -> dict:
        timestamp = "2026-08-10T14:00:00Z"
        return {
            "kind": "option_candidates", "source": "Robinhood MCP", "symbol": "XSP",
            "timestamp": timestamp, "dte_target": 1, "underlying": 760.15,
            "underlying_quote": {
                "kind": underlying_kind, "source": "Robinhood MCP", "symbol": "XSP",
                "timestamp": timestamp,
                "quote": {"symbol": "XSP", "bid_price": 760.10, "ask_price": 760.20},
            },
            "call": {
                "symbol": "XSP", "occ_symbol": "XSP260811C00760000", "instrument_id": "call-760",
                "option_type": "call", "strike": 760.0, "expiry": "2026-08-11",
                "bid": 0.98, "ask": 1.00, "mark": 0.99, "updated_at": timestamp,
                "quote_source": "Robinhood MCP",
            },
        }

    def test_xsp_contract_requires_a_fresh_index_quote_backing_spot(self):
        now = datetime(2026, 8, 10, 10, 0, tzinfo=engine.ET)
        contract, reason = engine.select_valid_entry_contract(
            self._candidates(), direction="bullish", underlying=760.15, now=now, symbol="XSP",
        )
        self.assertIsNotNone(contract, reason)
        rejected, reason = engine.select_valid_entry_contract(
            self._candidates("equity_quote"), direction="bullish", underlying=760.15, now=now, symbol="XSP",
        )
        self.assertIsNone(rejected)
        self.assertEqual("underlying_quote_kind_mismatch", reason)

    def test_xsp_parity_assembly_delay_does_not_use_direct_equity_quote_ttl(self):
        """A fresh candidate may outlive the parity-call assembly by a few seconds."""
        candidates = self._candidates()
        candidates["timestamp"] = "2026-08-10T14:00:08Z"
        candidates["call"]["updated_at"] = "2026-08-10T14:00:08Z"
        candidates["underlying_quote"]["timestamp"] = "2026-08-10T14:00:00Z"
        now = datetime(2026, 8, 10, 10, 0, 8, tzinfo=engine.ET)

        contract, reason = engine.select_valid_entry_contract(
            candidates, direction="bullish", underlying=760.15, now=now, symbol="XSP",
        )

        self.assertIsNotNone(contract, reason)
        self.assertEqual("call-760", contract["instrument_id"])

        candidates["underlying_quote"]["timestamp"] = "2026-08-10T13:59:51Z"
        rejected, reason = engine.select_valid_entry_contract(
            candidates, direction="bullish", underlying=760.15, now=now, symbol="XSP",
        )
        self.assertIsNone(rejected)
        self.assertEqual("underlying_stale_quote", reason)


@unittest.skip("XSP removed from active universe")
class XspStandaloneQuoteTests(unittest.TestCase):
    def test_xsp_manual_engine_quote_uses_index_bridge_with_es_reference(self):
        bars = [{"close": 7601.0}]
        expected = {"kind": "index_quote", "symbol": "XSP"}
        with (
            patch.object(engine, "fetch_bars", return_value=bars),
            patch.object(engine, "bridge", return_value=expected) as bridge_call,
        ):
            payload = engine.fetch_live_equity_quote("XSP")
        self.assertIs(expected, payload)
        bridge_call.assert_called_once_with(
            "--index", "XSP", "--underlying", "760.1", "--dte", "1",
        )


class EarlySessionScalpTests(unittest.TestCase):
    @staticmethod
    def _bar(session_date, time_value, close, *, open_value=None, volume=1000.0):
        hour, minute = divmod(time_value, 100)
        when = datetime.fromisoformat(f"{session_date}T{hour:02d}:{minute:02d}:00-04:00")
        opened = close if open_value is None else open_value
        return {
            "dt": when, "date": session_date, "time": time_value,
            "open": opened, "high": max(opened, close) + 0.05,
            "low": min(opened, close) - 0.05, "close": close,
            "volume": volume,
        }

    def test_entry_window_starts_after_first_completed_opening_bar(self):
        self.assertEqual(935, engine.ENTRY_START)
        state = engine.default_state()
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(engine, "calendar_block", return_value=None),
                patch.object(engine, "KILL_SWITCH", Path(tmp) / "missing-kill-switch.json"),
            ):
                self.assertEqual(
                    "outside_entry_window",
                    engine.market_block(datetime(2026, 8, 11, 9, 34, tzinfo=engine.ET), state),
                )
                self.assertIsNone(
                    engine.market_block(datetime(2026, 8, 11, 9, 35, tzinfo=engine.ET), state)
                )

    def test_daemon_tracks_premarket_before_options_can_trade(self):
        decision = live_daemon.choose_mode(
            {"activePositions": [], "dailyTrades": 0},
            datetime(2026, 8, 11, 8, 0, tzinfo=engine.ET),
        )
        self.assertEqual(
            ("premarket_watch", 60.0, "premarket"),
            (decision.mode, decision.interval_seconds, decision.action),
        )

    def test_daemon_tracks_opening_price_until_first_bar_completes(self):
        decision = live_daemon.choose_mode(
            {"activePositions": [], "dailyTrades": 0},
            datetime(2026, 8, 11, 9, 32, tzinfo=engine.ET),
        )
        self.assertEqual(
            ("opening_watch", 1.0, "premarket"),
            (decision.mode, decision.interval_seconds, decision.action),
        )

    def test_premarket_context_tracks_breakout_targets_before_open(self):
        bars = [
            self._bar("2026-08-10", 1555, 100.0, volume=2_000_000),
            self._bar("2026-08-11", 400, 100.20, volume=0),
            self._bar("2026-08-11", 700, 100.70, volume=0),
            self._bar("2026-08-11", 925, 100.55, volume=0),
        ]
        context = engine.build_premarket_context(
            bars,
            now=datetime(2026, 8, 11, 9, 29, tzinfo=engine.ET),
            symbol="SPY",
        )
        self.assertTrue(context["ready"])
        self.assertEqual(100.0, context["previousClose"])
        self.assertEqual(100.75, context["premarketHigh"])
        self.assertEqual(100.15, context["premarketLow"])
        self.assertEqual(100.75, context["bullishBreakoutTarget"])
        self.assertEqual(100.15, context["bearishBreakoutTarget"])
        self.assertEqual(100.55, context["currentPrice"])
        self.assertEqual("inside_premarket_range", context["targetState"])
        self.assertEqual("2026-08-11", context["sessionDate"])

    def test_extended_hours_bars_never_enter_regular_session_signal_bars(self):
        bars = [
            self._bar("2026-08-11", 925, 100.0),
            self._bar("2026-08-11", 930, 100.1),
            self._bar("2026-08-11", 935, 100.2),
            self._bar("2026-08-11", 1600, 100.3),
        ]
        completed = engine.completed_day_bars(
            bars,
            now=datetime(2026, 8, 11, 9, 40, tzinfo=engine.ET),
        )
        self.assertEqual([930, 935], [bar["time"] for bar in completed])

    def test_opening_relative_volume_uses_same_clock_bars_from_prior_sessions(self):
        history = [
            self._bar("2026-08-07", 930, 99.0, volume=1_000),
            self._bar("2026-08-10", 930, 100.0, volume=2_000),
        ]
        day = [self._bar("2026-08-11", 930, 101.0, open_value=100.8, volume=2_250)]
        ratio, source = engine.relative_volume_for_session(day, history)
        self.assertEqual(1.5, ratio)
        self.assertEqual("historical_same_clock", source)

    def test_first_completed_bar_can_score_with_prior_session_warmup(self):
        prior = [
            self._bar(
                "2026-08-10",
                ((13 * 60 + index * 5) // 60) * 100 + (13 * 60 + index * 5) % 60,
                99.0 + index * 0.01,
            )
            for index in range(35)
        ]
        day = [self._bar("2026-08-11", 930, 100.20, open_value=100.00, volume=2_000)]
        premarket = {
            "ready": True, "sessionDate": "2026-08-11", "symbol": "SPY",
            "premarketHigh": 100.15, "premarketLow": 99.60,
            "premarketMid": 99.875, "premarketLast": 100.10,
        }
        with (
            patch.object(engine, "ema", side_effect=lambda _values, period: 100.1 if period == 9 else 99.9),
            patch.object(engine, "vwap_bands", return_value=(100.05, 100.15, 99.95)),
            patch.object(engine, "rsi", return_value=55.0),
            patch.object(engine, "macd_hist", return_value=0.2),
            patch.object(engine, "supertrend_direction", return_value="up"),
            patch.object(engine, "atr", return_value=1.0),
            patch.object(engine, "turtle_intraday_context", return_value={
                "entryHigh": 0.0, "entryLow": 0.0, "exitHigh": 0.0, "exitLow": 0.0,
                "n": 1.0, "longBreakout": False, "shortBreakout": False,
            }),
        ):
            result = engine.signal(
                day,
                warmup_bars=prior,
                relative_volume=1.2,
                premarket_context=premarket,
            )
        self.assertEqual("bullish", result["direction"], result)
        self.assertGreaterEqual(result["score"], 7)
        self.assertEqual(1, result["sessionBarsUsed"])
        self.assertEqual(35, result["warmupBarsUsed"])
        self.assertTrue(result["premarketBreakout"])
        self.assertAlmostEqual(0.2, result["momentumPct"], places=6)

    def test_prior_session_warmup_never_enters_intraday_turtle_channel(self):
        prior = [
            self._bar(
                "2026-08-10",
                ((13 * 60 + index * 5) // 60) * 100 + (13 * 60 + index * 5) % 60,
                99.0 + index * 0.01,
            )
            for index in range(35)
        ]
        day = [
            self._bar(
                "2026-08-11",
                ((9 * 60 + 30 + index * 5) // 60) * 100
                + (9 * 60 + 30 + index * 5) % 60,
                100.0 + index * 0.02,
            )
            for index in range(7)
        ]
        turtle_inputs = []

        def capture_turtle(bars):
            turtle_inputs.append(list(bars))
            return {
                "entryHigh": 0.0, "entryLow": 0.0, "exitHigh": 0.0, "exitLow": 0.0,
                "n": 1.0, "longBreakout": False, "shortBreakout": False,
            }

        with (
            patch.object(engine, "ema", return_value=100.0),
            patch.object(engine, "vwap_bands", return_value=(100.0, 101.0, 99.0)),
            patch.object(engine, "rsi", return_value=50.0),
            patch.object(engine, "macd_hist", return_value=0.0),
            patch.object(engine, "supertrend_direction", return_value="neutral"),
            patch.object(engine, "atr", return_value=1.0),
            patch.object(engine, "turtle_intraday_context", side_effect=capture_turtle),
        ):
            engine.signal(day, warmup_bars=prior, relative_volume=1.0)

        self.assertEqual(1, len(turtle_inputs))
        self.assertEqual(day, turtle_inputs[0])
        self.assertTrue(all(bar["date"] == "2026-08-11" for bar in turtle_inputs[0]))

    def test_opening_breakout_rejects_entry_after_price_is_already_extended(self):
        prior = [
            self._bar(
                "2026-08-10",
                ((13 * 60 + index * 5) // 60) * 100 + (13 * 60 + index * 5) % 60,
                99.0,
            )
            for index in range(35)
        ]
        day = [self._bar("2026-08-11", 930, 102.0, open_value=100.0, volume=2_000)]
        premarket = {
            "ready": True, "sessionDate": "2026-08-11", "symbol": "SPY",
            "premarketHigh": 100.20, "premarketLow": 99.60,
            "premarketMid": 99.90, "premarketLast": 100.10,
        }
        with (
            patch.object(engine, "ema", side_effect=lambda _values, period: 101.0 if period == 9 else 100.0),
            patch.object(engine, "vwap_bands", return_value=(101.0, 101.2, 100.8)),
            patch.object(engine, "rsi", return_value=55.0),
            patch.object(engine, "macd_hist", return_value=0.2),
            patch.object(engine, "supertrend_direction", return_value="up"),
            patch.object(engine, "atr", return_value=1.0),
            patch.object(engine, "turtle_intraday_context", return_value={
                "entryHigh": 0.0, "entryLow": 0.0, "exitHigh": 0.0, "exitLow": 0.0,
                "n": 1.0, "longBreakout": False, "shortBreakout": False,
            }),
        ):
            result = engine.signal(
                day,
                warmup_bars=prior,
                relative_volume=1.2,
                premarket_context=premarket,
            )
        self.assertIsNone(result["direction"])
        self.assertIn("hard_gate:opening_breakout_already_extended", result["rejectionReasons"])

    def test_high_volume_cannot_bypass_unbroken_premarket_target(self):
        prior = [
            self._bar(
                "2026-08-10",
                ((13 * 60 + index * 5) // 60) * 100 + (13 * 60 + index * 5) % 60,
                101.0 - index * 0.01,
            )
            for index in range(35)
        ]
        day = [self._bar("2026-08-11", 930, 100.0, open_value=100.2, volume=4_000)]
        premarket = {
            "ready": True, "sessionDate": "2026-08-11", "symbol": "SPY",
            "premarketHigh": 100.50, "premarketLow": 99.50,
            "premarketMid": 100.0, "premarketLast": 100.10,
        }
        with (
            patch.object(engine, "ema", side_effect=lambda _values, period: 99.9 if period == 9 else 100.1),
            patch.object(engine, "vwap_bands", return_value=(100.1, 100.2, 100.0)),
            patch.object(engine, "rsi", return_value=50.0),
            patch.object(engine, "macd_hist", return_value=-0.2),
            patch.object(engine, "supertrend_direction", return_value="down"),
            patch.object(engine, "atr", return_value=1.0),
            patch.object(engine, "turtle_intraday_context", return_value={
                "entryHigh": 0.0, "entryLow": 0.0, "exitHigh": 0.0, "exitLow": 0.0,
                "n": 1.0, "longBreakout": False, "shortBreakout": False,
            }),
        ):
            result = engine.signal(
                day,
                warmup_bars=prior,
                relative_volume=2.0,
                premarket_context=premarket,
            )
        self.assertIsNone(result["direction"])
        self.assertFalse(result["bearishHighVolumeConfirmation"])
        self.assertIn("hard_gate:premarket_target_not_broken", result["rejectionReasons"])

    def test_live_price_cannot_bypass_opening_breakout_extension_limit(self):
        setup = {
            "openingContinuation": True,
            "rawDirection": "bullish",
            "premarketBreakoutTarget": 100.20,
            "openingAtr": 1.0,
        }
        self.assertIsNone(engine.opening_breakout_extension_failure(setup, 100.70))
        self.assertEqual(
            "opening_breakout_already_extended",
            engine.opening_breakout_extension_failure(setup, 100.71),
        )


class MarketDataTimingTests(unittest.TestCase):
    def test_entry_window_allows_1530_et_for_safe_late_setup(self):
        now = datetime(2026, 8, 11, 15, 30, tzinfo=engine.ET)
        state = engine.default_state()
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(engine, "calendar_block", return_value=None),
                patch.object(engine, "KILL_SWITCH", Path(tmp) / "missing-kill-switch.json"),
            ):
                reason = engine.market_block(now, state)
        self.assertIsNone(reason)

    def test_stale_macro_calendar_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            calendar_path = Path(tmp) / "calendar.json"
            calendar_path.write_text(__import__("json").dumps({
                "source": "fixture", "timezone": "America/New_York",
                "generatedAt": "2026-07-01T12:00:00-04:00",
                "coverageStart": "2026-07-01", "coverageThrough": "2026-07-14",
                "days": {},
            }))
            with patch.object(engine, "CALENDAR_PATH", calendar_path):
                reason = engine.calendar_block(datetime(2026, 8, 10, 10, 0, tzinfo=engine.ET))
        self.assertEqual("macro_calendar_stale_or_incomplete", reason)

    def test_incomplete_five_minute_bar_is_excluded(self):
        now = datetime(2026, 8, 7, 14, 57, tzinfo=engine.ET)
        bars = [
            {"dt": datetime(2026, 8, 7, 14, 50, tzinfo=engine.ET), "date": "2026-08-07"},
            {"dt": datetime(2026, 8, 7, 14, 55, tzinfo=engine.ET), "date": "2026-08-07"},
        ]
        completed = engine.completed_day_bars(bars, now=now)
        self.assertEqual([bars[0]], completed)

    def test_stale_completed_bar_is_rejected(self):
        now = datetime(2026, 8, 7, 15, 0, tzinfo=engine.ET)
        bars = [{"dt": datetime(2026, 8, 7, 14, 30, tzinfo=engine.ET), "date": "2026-08-07"}]
        passed, reason = engine.validate_completed_bar_freshness(bars, now=now)
        self.assertFalse(passed)
        self.assertEqual("stale_completed_bar", reason)

    @unittest.skip("XSP removed from active universe")
    def test_index_option_bar_gets_wider_staleness_window(self):
        # XSP bars during overnight sessions can be 15+ minutes apart.
        # A bar that is stale for SPY (600s) should still be OK for XSP.
        now = datetime(2026, 8, 7, 15, 0, tzinfo=engine.ET)
        # Bar at 14:49 + 5min = completed at 14:54, age = 360s: SPY still fresh
        # Bar at 14:44 + 5min = completed at 14:49, age = 660s: SPY stale, XSP fresh
        bars_spy_stale = [{"dt": datetime(2026, 8, 7, 14, 44, tzinfo=engine.ET), "date": "2026-08-07"}]
        # SPY: stale (660s > 600s)
        passed_spy, reason_spy = engine.validate_completed_bar_freshness(
            bars_spy_stale, now=now, symbol="SPY",
        )
        self.assertFalse(passed_spy)
        self.assertEqual("stale_completed_bar", reason_spy)
        # XSP: still fresh (660s < 1800s)
        passed_xsp, reason_xsp = engine.validate_completed_bar_freshness(
            bars_spy_stale, now=now, symbol="XSP",
        )
        self.assertTrue(passed_xsp)
        self.assertEqual("ok", reason_xsp)

    @unittest.skip("XSP removed from active universe")
    def test_index_option_bar_still_rejected_when_truly_stale(self):
        # XSP bar 35 minutes old should be rejected even with the wider window.
        now = datetime(2026, 8, 7, 15, 0, tzinfo=engine.ET)
        bars = [{"dt": datetime(2026, 8, 7, 14, 20, tzinfo=engine.ET), "date": "2026-08-07"}]
        passed, reason = engine.validate_completed_bar_freshness(
            bars, now=now, symbol="XSP",
        )
        self.assertFalse(passed)
        self.assertEqual("stale_completed_bar", reason)


class ReplayDiagnosticsTests(unittest.TestCase):
    def test_episode_grouping_separates_gaps_and_direction_changes(self):
        import v73_opportunity_replay as replay
        rows = [
            {"date": "2026-08-07", "direction": "bullish", "observableAt": "2026-08-07T14:10:00-04:00"},
            {"date": "2026-08-07", "direction": "bullish", "observableAt": "2026-08-07T14:20:00-04:00"},
            {"date": "2026-08-07", "direction": "bearish", "observableAt": "2026-08-07T14:25:00-04:00"},
            {"date": "2026-08-07", "direction": "bullish", "observableAt": "2026-08-07T14:45:00-04:00"},
        ]
        episodes = replay.group_opportunities(rows, max_gap_minutes=15)
        self.assertEqual(3, len(episodes))
        self.assertEqual(2, episodes[0]["barCount"])


class EntryTransactionTests(unittest.TestCase):
    def setUp(self):
        # Clear module-level venue timestamp caches between tests.
        engine._LAST_LIVE_EQUITY_VENUE_TS.clear()
        engine._LAST_OPTION_VENUE_TS.clear()

    def test_post_quote_direction_flip_is_rejected(self):
        original = {"direction": "bullish", "score": 8, "relativeVolume": 1.2}
        refreshed = {"direction": "bearish", "score": 8, "relativeVolume": 1.2}
        self.assertEqual(
            "signal_direction_changed",
            engine.post_quote_signal_failure(original, refreshed),
        )

    def test_post_quote_momentum_failure_is_rejected(self):
        original = self._live_trigger_setup("bullish")
        original.update(direction="bullish", bullChecks={"momentum": True})
        refreshed = self._live_trigger_setup("bullish")
        refreshed.update(direction="bullish", bullChecks={"momentum": False})
        self.assertEqual(
            "momentum_not_confirmed",
            engine.post_quote_signal_failure(original, refreshed, symbol="SPY"),
        )

    @staticmethod
    def _live_trigger_setup(direction="bullish", *, rel_volume=1.2):
        return {
            "direction": None, "rawDirection": direction,
            "score": 7, "bullScore": 7 if direction == "bullish" else 1,
            "bearScore": 7 if direction == "bearish" else 1,
            "relativeVolume": rel_volume,
            "close": 100.0, "vwap": 100.0, "vwapUpper": 101.0, "vwapLower": 99.0,
            "rsi": 50.0, "minTriggerMove": 0.05,
            "turtle": {
                "n": 0.5, "entryHigh": 100.05, "entryLow": 99.95,
                "exitHigh": 100.02, "exitLow": 99.98,
                "longBreakout": direction == "bullish",
                "shortBreakout": direction == "bearish",
            },
            "rejectionReasons": [
                "insufficient_bullish_trigger" if direction == "bullish"
                else "insufficient_bearish_trigger"
            ],
        }

    @staticmethod
    def _equity_quote(now, bid, ask):
        return {
            "kind": "equity_quote", "source": "Robinhood MCP", "symbol": "SPY",
            "timestamp": now.isoformat(),
            "quote": {"symbol": "SPY", "bid_price": str(bid), "ask_price": str(ask)},
        }

    def test_live_quote_confirms_completed_bar_bullish_timing(self):
        now = datetime(2026, 8, 7, 15, 0, tzinfo=engine.ET)
        result = engine.apply_live_trigger_confirmation(
            self._live_trigger_setup("bullish"),
            self._equity_quote(now, 100.07, 100.09),
            now=now,
        )
        self.assertEqual("bullish", result["direction"])
        self.assertTrue(result["liveTrigger"])
        self.assertEqual(100.08, result["close"])
        self.assertEqual(7, result["score"])
        self.assertEqual(1.2, result["relativeVolume"])

    def test_live_quote_cannot_bypass_relative_volume_gate(self):
        now = datetime(2026, 8, 7, 15, 0, tzinfo=engine.ET)
        setup = self._live_trigger_setup("bullish", rel_volume=0.74)
        result = engine.apply_live_trigger_confirmation(
            setup, self._equity_quote(now, 100.10, 100.12), now=now,
        )
        self.assertIsNone(result["direction"])
        self.assertFalse(engine.live_trigger_eligible(setup))

    def test_live_quote_preserves_persisted_hard_gate_reasons(self):
        now = datetime(2026, 8, 7, 15, 0, tzinfo=engine.ET)
        for reason in (
            "hard_gate:vwap_no_conviction",
            "hard_gate:evidence_mean_reversion_zero",
            "hard_gate:premarket_target_not_broken",
        ):
            with self.subTest(reason=reason):
                setup = self._live_trigger_setup("bullish")
                setup["rejectionReasons"] = [reason, "insufficient_bullish_trigger"]
                result = engine.apply_live_trigger_confirmation(
                    setup, self._equity_quote(now, 100.07, 100.09), now=now,
                )
                self.assertFalse(engine.live_trigger_eligible(setup))
                self.assertIsNone(result["direction"])
                self.assertEqual([reason, "insufficient_bullish_trigger"], result["rejectionReasons"])

    def test_post_quote_recheck_rejects_refreshed_hard_gate(self):
        original = self._live_trigger_setup("bullish")
        original["direction"] = "bullish"
        refreshed = dict(original)
        refreshed["bullChecks"] = {"momentum": True}
        refreshed["rejectionReasons"] = ["hard_gate:vwap_no_conviction"]
        self.assertEqual(
            "hard_gate:vwap_no_conviction",
            engine.post_quote_signal_failure(original, refreshed, symbol="SPY"),
        )

    def test_live_quote_cannot_revive_momentum_failed_setup(self):
        now = datetime(2026, 8, 7, 15, 0, tzinfo=engine.ET)
        setup = self._live_trigger_setup("bullish")
        setup["bullChecks"] = {"momentum": False}
        setup["bearChecks"] = {"momentum": False}
        setup = dict(setup, direction="bullish")
        rejected = engine.apply_normal_path_momentum_gate(setup)
        self.assertIsNone(rejected["direction"])
        self.assertFalse(engine.live_trigger_eligible(rejected, symbol="SPY"))
        confirmed = engine.apply_live_trigger_confirmation(
            rejected, self._equity_quote(now, 100.07, 100.09), now=now,
        )
        self.assertIsNone(confirmed["direction"])

    def test_stale_live_quote_cannot_confirm_entry(self):
        now = datetime(2026, 8, 7, 15, 0, tzinfo=engine.ET)
        quote = self._equity_quote(
            datetime(2026, 8, 7, 14, 59, 50, tzinfo=engine.ET), 100.10, 100.12,
        )
        result = engine.apply_live_trigger_confirmation(
            self._live_trigger_setup("bullish"), quote, now=now,
        )
        self.assertIsNone(result["direction"])
        self.assertEqual("stale_quote", result["liveTriggerReason"])

    def test_live_quote_must_move_far_enough(self):
        now = datetime(2026, 8, 7, 15, 0, tzinfo=engine.ET)
        result = engine.apply_live_trigger_confirmation(
            self._live_trigger_setup("bearish"),
            self._equity_quote(now, 99.97, 99.99),
            now=now,
        )
        self.assertIsNone(result["direction"])
        self.assertIn("insufficient_bearish_trigger", result["liveTriggerRejectionReasons"])

    def test_quote_bridge_runs_without_state_lock(self):
        fixed_now = datetime(2026, 8, 7, 14, 0, tzinfo=engine.ET)
        bar = {
            "dt": fixed_now.replace(minute=55) - __import__("datetime").timedelta(hours=1),
            "date": "2026-08-07", "time": 1355,
            "open": 100.0, "high": 100.2, "low": 99.9, "close": 100.1, "volume": 1000.0,
        }
        setup = {
            "direction": "bullish", "rawDirection": "bullish", "score": 8,
            "bullScore": 8, "bearScore": 1, "relativeVolume": 1.2,
            "close": 100.1, "vwap": 100.0, "rsi": 55.0,
            # V7.5.4: normal-path entries require directional momentum; real
            # signal() setups always populate these checks.
            "bullChecks": {"momentum": True},
            "bearChecks": {"momentum": True},
            "turtle": {"n": 0.5, "entryHigh": 100.2, "entryLow": 99.8,
                       "exitHigh": 100.1, "exitLow": 99.9,
                       "longBreakout": True, "shortBreakout": False},
        }
        contract = {
            "occ_symbol": "SPY260810C00100000", "expiry": "2026-08-10",
            "instrument_id": "instrument-100-call",
            "strike": 100.0, "bid": 0.98, "ask": 1.00,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "state.json"
            ledger_path = root / "ledger.jsonl"
            lock_path = root / "state.lock"

            def quote_bridge(*_args):
                self.assertIn("--option-type", _args)
                self.assertEqual("call", _args[_args.index("--option-type") + 1])
                self.assertIn("--underlying", _args)
                self.assertEqual("100.1", _args[_args.index("--underlying") + 1])
                # Reservation must be durable before the slow external call.
                pre_quote_state = __import__("json").loads(state_path.read_text())
                self.assertIsInstance(pre_quote_state.get("pendingEntry"), dict)
                self.assertGreater(pre_quote_state["pendingEntry"]["riskReserved"], 0)
                # This succeeds only when scan has released the state lock.
                with engine.exclusive_state_lock():
                    pass
                return {
                    "kind": "option_candidates", "source": "Robinhood MCP",
                    "symbol": "SPY", "timestamp": fixed_now.isoformat(),
                    "dte_target": 1, "call": contract, "_snapshotPath": "/tmp/quote.json",
                }

            captured = io.StringIO()
            with (
                patch.object(engine, "state_path", return_value=state_path),
                patch.object(engine, "log_path", return_value=ledger_path),
                patch.object(engine, "state_lock_path", return_value=lock_path),
                patch.object(engine, "KILL_SWITCH", root / "kill.json"),
                patch.object(engine, "CALENDAR_PATH", root / "calendar.json"),
                patch.object(engine, "calendar_block", return_value=None),
                patch.object(engine, "now_et", return_value=fixed_now),
                patch.object(engine, "fetch_bars", return_value=[bar]),
                patch.object(engine, "completed_day_bars", return_value=[bar]),
                patch.object(engine, "validate_completed_bar_freshness", return_value=(True, "ok")),
                patch.object(engine, "signal", return_value=dict(setup)),
                patch.object(engine, "bridge", side_effect=quote_bridge),
                patch.object(engine, "select_valid_entry_contract", return_value=(contract, "ok")),
                contextlib.redirect_stdout(captured),
            ):
                result = engine.scan()
                state = engine.load_state()

        self.assertEqual(0, result, captured.getvalue())
        self.assertEqual(1, len(state["activePositions"]), captured.getvalue())


class SignalQualityFixTests(unittest.TestCase):
    def test_equity_momentum_uses_v743_fixed_floor_for_early_trend(self):
        session = []
        start = datetime(2026, 8, 26, 9, 30, tzinfo=engine.ET)
        closes = [100.00, 100.00, 100.00, 100.00, 100.00, 100.00, 100.60]
        for index, close in enumerate(closes):
            when = start + timedelta(minutes=5 * index)
            session.append({
                "dt": when,
                "date": "2026-08-26",
                "time": 930 + 5 * index,
                "open": close - 0.02,
                "high": close + 0.03,
                "low": close - 0.03,
                "close": close,
                "volume": 1000,
            })
        warmup = [
            dict(session[-1], date="2026-08-25", dt=start - timedelta(minutes=5 * (40 - index)))
            for index in range(40)
        ]
        turtle = {
            "n": 0, "upper": 101.0, "lower": 99.0,
            "longBreakout": False, "shortBreakout": False,
            "exitLow": 99.0, "exitHigh": 101.0,
        }
        with (
            patch.object(engine, "ema", side_effect=lambda values, period: 101.0 if period == 9 else 100.0),
            patch.object(engine, "vwap_bands", return_value=(100.0, 101.0, 99.0)),
            patch.object(engine, "supertrend_direction", return_value="up"),
            patch.object(engine, "macd_hist", return_value=0.20),
            patch.object(engine, "rsi", return_value=50.0),
            patch.object(engine, "atr", return_value=10.0),
            patch.object(engine, "turtle_intraday_context", return_value=turtle),
        ):
            result = engine.signal(
                session,
                warmup_bars=warmup,
                relative_volume=1.2,
                symbol="SPY",
            )
        self.assertTrue(result["bullChecks"]["momentum"])
        self.assertEqual(7, result["score"])

    def test_opening_premarket_substitute_does_not_count_as_structural_score(self):
        session = []
        start = datetime(2026, 8, 26, 9, 30, tzinfo=engine.ET)
        closes = [100.00, 100.10, 100.20, 100.30, 100.40, 100.50]
        for index, close in enumerate(closes):
            when = start + timedelta(minutes=5 * index)
            session.append({
                "dt": when,
                "date": "2026-08-26",
                "time": 930 + 5 * index,
                "open": close - 0.02,
                "high": close + 0.03,
                "low": close - 0.03,
                "close": close,
                "volume": 1000,
            })
        warmup = [
            dict(session[-1], date="2026-08-25", dt=start - timedelta(minutes=5 * (40 - index)))
            for index in range(40)
        ]
        turtle = {
            "n": 0, "upper": 101.0, "lower": 99.0,
            "longBreakout": False, "shortBreakout": False,
            "exitLow": 99.0, "exitHigh": 101.0,
        }
        with (
            patch.object(engine, "ema", side_effect=lambda values, period: 101.0 if period == 9 else 100.0),
            patch.object(engine, "vwap_bands", return_value=(100.0, 101.0, 99.0)),
            patch.object(engine, "supertrend_direction", return_value="down"),
            patch.object(engine, "macd_hist", return_value=0.20),
            patch.object(engine, "rsi", return_value=50.0),
            patch.object(engine, "atr", return_value=1.0),
            patch.object(engine, "turtle_intraday_context", return_value=turtle),
        ):
            result = engine.signal(
                session,
                warmup_bars=warmup,
                relative_volume=1.2,
                premarket_context={
                    "ready": True,
                    "sessionDate": "2026-08-26",
                    "premarketHigh": 100.40,
                    "premarketLow": 99.00,
                },
                symbol="SPY",
            )
        self.assertEqual(6, result["score"])
        self.assertEqual(6, result["independentScore"])
        self.assertTrue(result["bullChecks"]["turtle_breakout"] is False)
        self.assertIn("premarket_breakout_context", result["qualificationFlags"])

    def test_nonstructural_bullish_entry_rejects_underlying_that_has_reversed(self):
        setup = {
            "direction": "bullish",
            "close": 299.685,
            "minTriggerMove": 0.02,
            "turtle": {"longBreakout": False, "shortBreakout": False},
        }
        reason = engine.entry_directional_drift_reason(setup, 299.655)
        self.assertEqual("entry_underlying_against_signal", reason)

    def test_nonstructural_bearish_entry_rejects_underlying_that_has_reversed(self):
        setup = {
            "direction": "bearish",
            "close": 708.525,
            "minTriggerMove": 0.02,
            "turtle": {"longBreakout": False, "shortBreakout": False},
        }
        reason = engine.entry_directional_drift_reason(setup, 708.590)
        self.assertEqual("entry_underlying_against_signal", reason)

    def test_signal_reversal_requires_two_completed_bars_after_entry(self):
        position = {
            "direction": "bullish",
            "signal": {
                "direction": "bullish",
                "close": 100.00,
                "signalBarEnd": "2026-08-26T10:00:00-04:00",
                "minTriggerMove": 0.02,
            },
        }
        first = {
            "dt": datetime(2026, 8, 26, 10, 5, tzinfo=engine.ET),
            "close": 99.94,
        }
        second = {
            "dt": datetime(2026, 8, 26, 10, 10, tzinfo=engine.ET),
            "close": 99.93,
        }
        self.assertIsNone(engine.signal_reversal_reason(position, [first]))
        self.assertEqual("signal_reversal", engine.signal_reversal_reason(position, [first, second]))

    def test_structural_breakout_keeps_existing_entry_timing_path(self):
        setup = {
            "direction": "bullish",
            "close": 100.00,
            "minTriggerMove": 0.02,
            "turtle": {"longBreakout": True, "shortBreakout": False},
        }
        self.assertIsNone(engine.entry_directional_drift_reason(setup, 99.50))

    def test_detector_without_structural_breakout_gets_same_quality_protection(self):
        setup = {
            "direction": "bullish",
            "close": 100.00,
            "minTriggerMove": 0.02,
            "pump_detector": True,
            "turtle": {"longBreakout": False, "shortBreakout": False},
        }
        self.assertEqual(
            "entry_underlying_against_signal",
            engine.entry_directional_drift_reason(setup, 99.50),
        )
        self.assertEqual(
            8.0,
            engine.profit_target_pct_for_position({
                "direction": "bullish",
                "optionType": "call",
                "entryAsk": 1.0,
                "signal": setup,
            }),
        )

    def test_nonstructural_signal_uses_fast_profit_target(self):
        position = {
            "direction": "bearish",
            "optionType": "put",
            "entryAsk": 1.49,
            "signal": {
                "direction": "bearish",
                "independentScore": 7,
                "bearChecks": {
                    "ema_trend": True, "vwap": True, "supertrend": True,
                    "macd": True, "rsi": True, "momentum": True,
                    "relative_volume": True, "turtle_breakout": False,
                },
                "turtle": {"shortBreakout": False},
            },
        }
        self.assertEqual(8.0, engine.profit_target_pct_for_position(position))


class ExitPolicyTests(unittest.TestCase):
    def test_bearish_put_uses_fifteen_percent_profit_target(self):
        position = {"direction": "bearish", "optionType": "put"}

        self.assertEqual(15.0, engine.profit_target_pct_for_position(position))

    def test_bullish_call_uses_fifteen_percent_profit_target(self):
        position = {"direction": "bullish", "optionType": "call"}

        self.assertEqual(15.0, engine.profit_target_pct_for_position(position))

    def test_bearish_put_uses_fifteen_percent_stop_loss(self):
        position = {"direction": "bearish", "optionType": "put"}

        self.assertEqual(15.0, engine.stop_loss_pct_for_position(position))

    def test_bullish_call_uses_twenty_percent_stop_loss(self):
        position = {"direction": "bullish", "optionType": "call"}

        self.assertEqual(20.0, engine.stop_loss_pct_for_position(position))

    def test_soft_exit_reasons_require_two_ticks(self):
        self.assertIn("stop_loss", engine.SOFT_EXIT_REASONS)
        self.assertIn("breakeven_stop", engine.SOFT_EXIT_REASONS)
        self.assertIn("trailing_stop", engine.SOFT_EXIT_REASONS)
        self.assertEqual(2, engine.MIN_CONFIRMATION_TICKS)

    def test_hard_exit_reasons_not_soft(self):
        self.assertNotIn("profit_target", engine.SOFT_EXIT_REASONS)
        self.assertNotIn("time_stop", engine.SOFT_EXIT_REASONS)
        self.assertNotIn("hard_close", engine.SOFT_EXIT_REASONS)
        self.assertNotIn("vwap_invalidation", engine.SOFT_EXIT_REASONS)
        self.assertNotIn("turtle_opposite_breakout", engine.SOFT_EXIT_REASONS)


class SpreadFilterTests(unittest.TestCase):
    def test_relative_spread_passes_when_spread_under_30pct_of_target(self):
        # A $0.50 spread on a $1.20 contract (debit $120, within $75-$175).
        # Flat spread: 0.50/1.20 = 41.7% — exceeds 5% flat cap, so use a tighter one.
        # Use ask=$1.30, bid=$1.25 → spread 3.85%, debit $130, target 15% = $19.50.
        # Spread $5 / target $19.50 = 25.6% — under 30%, should pass.
        contract = {
            "bid": 1.25,
            "ask": 1.30,
            "strike": 749.0,
            "open_interest": 1000,
            "volume": 100,
        }
        ok, reason = engine.quality_gate(contract, "bullish", 750.0)
        self.assertTrue(ok, reason)

    def test_relative_spread_rejects_when_spread_exceeds_30pct_of_target(self):
        # A $0.40 spread on a $1.30 contract (debit $130, within range).
        # Flat spread: 0.40/1.30 = 30.8% — exceeds 5% flat cap, caught earlier.
        # Need spread under 5% flat but over 30% of target.
        # ask=$1.28, bid=$1.22 → spread 4.69%, debit $128, target 15% = $19.20.
        # Spread $6 / target $19.20 = 31.25% — over 30%.
        # Actually spread_pct = (1.28-1.22)/1.28*100 = 4.69% — under 5% flat cap.
        contract = {"bid": 1.22, "ask": 1.28, "strike": 749.0}
        ok, reason = engine.quality_gate(contract, "bullish", 750.0)
        self.assertFalse(ok)
        self.assertIn("spread_vs_target", reason)

    def test_bearish_relative_spread_uses_bearish_target(self):
        # Same contract but for a put. With bearish target at 15%, same math.
        contract = {"bid": 1.22, "ask": 1.28, "strike": 751.0}
        ok, reason = engine.quality_gate(contract, "bearish", 750.0)
        self.assertFalse(ok)
        self.assertIn("spread_vs_target", reason)


class StaleVenueGuardTests(unittest.TestCase):
    def setUp(self):
        engine._LAST_LIVE_EQUITY_VENUE_TS.clear()
        engine._LAST_OPTION_VENUE_TS.clear()
        engine._LAST_STALE_QUOTE_REFRESH_AT.clear()

    def test_first_live_trigger_quote_is_not_stale(self):
        now = datetime(2026, 8, 7, 15, 0, tzinfo=engine.ET)
        setup = {
            "direction": None, "rawDirection": "bullish", "score": 8,
            "bullScore": 8, "bearScore": 1, "relativeVolume": 1.2,
            "close": 100.0, "vwap": 100.0, "vwapUpper": 101.0, "vwapLower": 99.0,
            "rsi": 50.0, "minTriggerMove": 0.05,
            "rejectionReasons": ["insufficient_bullish_trigger"],
        }
        quote = {
            "kind": "equity_quote", "source": "Robinhood MCP", "symbol": "SPY",
            "timestamp": now.isoformat(),
            "quote": {"symbol": "SPY", "bid_price": "100.10", "ask_price": "100.12"},
        }
        result = engine.apply_live_trigger_confirmation(setup, quote, now=now)
        # First call should not be stale.
        self.assertNotEqual(result.get("liveTriggerReason"), "stale_venue_timestamp")

    def test_repeated_venue_timestamp_is_rejected(self):
        now = datetime(2026, 8, 7, 15, 0, tzinfo=engine.ET)
        setup = {
            "direction": None, "rawDirection": "bullish", "score": 8,
            "bullScore": 8, "bearScore": 1, "relativeVolume": 1.2,
            "close": 100.0, "vwap": 100.0, "vwapUpper": 101.0, "vwapLower": 99.0,
            "rsi": 50.0, "minTriggerMove": 0.05,
            "turtle": {
                "n": 0.5, "entryHigh": 100.05, "entryLow": 99.95,
                "exitHigh": 100.02, "exitLow": 99.98,
                "longBreakout": True, "shortBreakout": False,
            },
            "rejectionReasons": ["insufficient_bullish_trigger"],
        }
        quote = {
            "kind": "equity_quote", "source": "Robinhood MCP", "symbol": "SPY",
            "timestamp": now.isoformat(),
            "quote": {"symbol": "SPY", "bid_price": "100.10", "ask_price": "100.12"},
        }
        # First call stores the timestamp.
        engine.apply_live_trigger_confirmation(setup, quote, now=now)
        # Second call with same timestamp should be rejected as stale.
        result = engine.apply_live_trigger_confirmation(setup, quote, now=now)
        self.assertEqual(result.get("liveTriggerReason"), "stale_venue_timestamp")

    def test_repeated_option_venue_timestamp_holds_without_error(self):
        now = datetime(2026, 8, 13, 14, 55, tzinfo=engine.ET)
        occ_symbol = "SPY260814C00778000"
        timestamp = now.isoformat()
        state = engine.default_state("SPY")
        state["lastResetDate"] = engine.today_key()
        state["activePositions"] = [{
            "positionId": "paper-duplicate-quote",
            "symbol": "SPY",
            "direction": "bullish",
            "optionType": "call",
            "occSymbol": occ_symbol,
            "entryAsk": 1.00,
            "entryTime": (now.replace(minute=54)).isoformat(),
            "peakBid": 1.00,
        }]
        quote = {
            "kind": "option_quote",
            "source": "Robinhood MCP",
            "timestamp": timestamp,
            "occ_symbol": occ_symbol,
            "bid": 1.01,
            "ask": 1.02,
        }
        engine._LAST_OPTION_VENUE_TS[occ_symbol] = timestamp
        output = io.StringIO()
        with (
            patch.object(engine, "load_state", return_value=state),
            patch.object(engine, "save_state"),
            patch.object(engine, "log"),
            patch.object(engine, "bridge", return_value=quote),
            patch.object(engine, "now_et", return_value=now),
            contextlib.redirect_stdout(output),
        ):
            rc = engine._monitor_locked("SPY")

        payload = json.loads(output.getvalue())
        self.assertEqual(0, rc)
        self.assertEqual("HOLD", payload["action"])
        self.assertTrue(payload["staleVenue"])
        self.assertEqual(1.02, payload["ask"])
        self.assertEqual(1.0, payload["pnl"])

    def test_stale_exit_quote_refreshes_once_then_holds_fail_closed(self):
        """A stale provider timestamp is retried, never accepted as executable."""
        now = datetime(2026, 8, 13, 14, 55, tzinfo=engine.ET)
        occ_symbol = "SPY260814C00778000"
        stale_timestamp = (now - timedelta(minutes=1)).isoformat()
        state = engine.default_state("SPY")
        state["lastResetDate"] = engine.today_key()
        state["activePositions"] = [{
            "positionId": "paper-stale-exit-refresh",
            "symbol": "SPY",
            "direction": "bullish",
            "optionType": "call",
            "occSymbol": occ_symbol,
            "entryAsk": 1.00,
            "entryTime": (now - timedelta(minutes=1)).isoformat(),
            "peakBid": 1.00,
            "lastQuote": {"bid": 1.00, "ask": 1.01, "quoteTimestamp": stale_timestamp},
        }]
        stale_quote = {
            "kind": "option_quote",
            "source": "Robinhood MCP",
            "timestamp": stale_timestamp,
            "occ_symbol": occ_symbol,
            "bid": 1.00,
            "ask": 1.01,
        }
        output = io.StringIO()
        with (
            patch.object(engine, "load_state", return_value=state),
            patch.object(engine, "save_state"),
            patch.object(engine, "log"),
            patch.object(engine, "bridge", return_value=stale_quote),
            patch.object(engine, "refresh_option_quote", return_value=stale_quote, create=True) as refresh,
            patch.object(engine, "now_et", return_value=now),
            contextlib.redirect_stdout(output),
        ):
            rc = engine._monitor_locked("SPY")

        payload = json.loads(output.getvalue())
        self.assertEqual(0, rc)
        self.assertEqual("HOLD", payload["action"])
        self.assertTrue(payload["staleQuote"])
        self.assertTrue(payload["quoteRefreshAttempted"])
        self.assertEqual("stale_quote", payload["quoteReason"])
        self.assertNotIn("invalid_exit_quote", output.getvalue())
        refresh.assert_called_once_with("--option", occ_symbol)
        self.assertEqual([state["activePositions"][0]], state["activePositions"])

    def test_duplicate_option_timestamp_preserves_soft_exit_confirmation(self):
        now = datetime(2026, 8, 13, 14, 55, tzinfo=engine.ET)
        occ_symbol = "SPY260814C00778000"
        timestamp = now.isoformat()
        state = engine.default_state("SPY")
        state["lastResetDate"] = engine.today_key()
        state["activePositions"] = [{
            "positionId": "paper-duplicate-quote-pending",
            "symbol": "SPY",
            "direction": "bullish",
            "optionType": "call",
            "occSymbol": occ_symbol,
            "entryAsk": 1.00,
            "entryTime": (now - timedelta(minutes=6)).isoformat(),
            "peakBid": 1.00,
            "pendingExitReason": "breakeven_exit",
            "exitConfirmCount": 1,
        }]
        quote = {
            "kind": "option_quote",
            "source": "Robinhood MCP",
            "timestamp": timestamp,
            "occ_symbol": occ_symbol,
            "bid": 1.01,
            "ask": 1.02,
        }
        engine._LAST_OPTION_VENUE_TS[occ_symbol] = timestamp
        output = io.StringIO()
        with (
            patch.object(engine, "load_state", return_value=state),
            patch.object(engine, "save_state"),
            patch.object(engine, "log"),
            patch.object(engine, "bridge", return_value=quote),
            patch.object(engine, "now_et", return_value=now),
            contextlib.redirect_stdout(output),
        ):
            rc = engine._monitor_locked("SPY")

        self.assertEqual(0, rc)
        self.assertEqual("HOLD", json.loads(output.getvalue())["action"])
        self.assertEqual("breakeven_exit", state["activePositions"][0]["pendingExitReason"])
        self.assertEqual(1, state["activePositions"][0]["exitConfirmCount"])

    def test_repeated_option_timestamp_cannot_block_time_stop(self):
        now = datetime(2026, 8, 13, 14, 55, tzinfo=engine.ET)
        occ_symbol = "SPY260814C00778000"
        timestamp = now.isoformat()
        state = engine.default_state("SPY")
        state["lastResetDate"] = engine.today_key()
        # Position held 36 min with a score-8 signal → dynamic max hold = ~31 min
        # so 36 min >> dynamic threshold, time_stop should fire regardless.
        state["activePositions"] = [{
            "positionId": "paper-duplicate-quote-time-stop",
            "symbol": "SPY",
            "direction": "bullish",
            "optionType": "call",
            "occSymbol": occ_symbol,
            "entryAsk": 1.00,
            "entryTime": (now - timedelta(minutes=36)).isoformat(),
            "peakBid": 1.00,
            "signal": {"score": 8, "direction": "bullish", "relativeVolume": 1.0,
                        "bullChecks": {"ema_trend": True, "vwap": True, "supertrend": True,
                                       "macd": True, "rsi": True, "momentum": True,
                                       "relative_volume": True, "turtle_breakout": True}},
        }]
        quote = {
            "kind": "option_quote",
            "source": "Robinhood MCP",
            "timestamp": timestamp,
            "occ_symbol": occ_symbol,
            "bid": 1.01,
            "ask": 1.02,
        }
        engine._LAST_OPTION_VENUE_TS[occ_symbol] = timestamp
        output = io.StringIO()
        with (
            patch.object(engine, "load_state", return_value=state),
            patch.object(engine, "save_state"),
            patch.object(engine, "log"),
            patch.object(engine, "bridge", return_value=quote),
            patch.object(engine, "now_et", return_value=now),
            contextlib.redirect_stdout(output),
        ):
            rc = engine._monitor_locked("SPY")

        payload = json.loads(output.getvalue())
        self.assertEqual(0, rc)
        self.assertEqual("PAPER_EXIT", payload["action"])
        self.assertEqual("time_stop", payload["reason"])
        self.assertEqual([], state["activePositions"])


class RiskControlTests(unittest.TestCase):
    def test_crash_detector_bypasses_consecutive_loss_cooldown(self):
        state = engine.default_state()
        state["consecutiveLosses"] = 3
        setup = {"direction": "bearish", "score": 7, "crash_detector": True}

        self.assertFalse(engine.loss_cooldown_blocks(state, setup))

    def test_normal_score_seven_is_blocked_after_two_losses(self):
        state = engine.default_state()
        state["consecutiveLosses"] = 2
        setup = {"direction": "bullish", "score": 7, "crash_detector": False}

        self.assertTrue(engine.loss_cooldown_blocks(state, setup))

    def test_post_quote_recheck_keeps_late_entry_score_bump(self):
        original = {"direction": "bullish", "score": 8}
        refreshed = {
            "direction": "bullish",
            "score": 5,  # V7.4.9: below late-entry floor 8 (7 + 1 bump)
            "relativeVolume": 1.2,
            "signalBarEnd": "2026-08-24T14:35:00-04:00",
            "turtle": {"longBreakout": True, "shortBreakout": False},
        }

        self.assertEqual(
            "signal_score_below_gate",
            engine.post_quote_signal_failure(original, refreshed, symbol="SPY"),
        )

    def test_only_one_pending_entry_can_reserve_risk(self):
        state = engine.default_state()
        now = datetime(2026, 8, 7, 14, 0, tzinfo=engine.ET)
        setup = {"direction": "bullish", "score": 8, "relativeVolume": 1.2}
        first, reason = engine.create_entry_reservation(state, setup=setup, now=now)
        self.assertEqual("ok", reason)
        self.assertIsNotNone(first)
        self.assertEqual(35.0, first["riskReserved"])
        self.assertEqual(175.0, first["maxDebit"])
        second, reason = engine.create_entry_reservation(state, setup=setup, now=now)
        self.assertIsNone(second)
        self.assertEqual("pending_entry_exists", reason)

    def test_entry_reserves_remaining_daily_loss_budget(self):
        state = engine.default_state()
        state["dailyGrossLoss"] = 39.0
        allowed, reason, planned_risk, remaining = engine.entry_risk_budget(
            state, entry_cost=175.0,
        )
        self.assertFalse(allowed)
        self.assertEqual("insufficient_daily_risk_budget", reason)
        self.assertEqual(35.0, planned_risk)
        self.assertEqual(1.0, remaining)

    def test_portfolio_loss_reconciles_from_ledgers_instead_of_state_files(self):
        now = datetime(2026, 10, 12, 15, 0, tzinfo=engine.ET)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_paths = {}
            ledger_paths = {}
            for symbol in engine.SYMBOLS:
                state_path = root / f"{symbol}-state.json"
                state = engine.default_state(symbol)
                state.update({"lastResetDate": "2026-10-12", "dailyGrossLoss": 0.0})
                state_path.write_text(json.dumps(state))
                state_paths[symbol] = state_path
                ledger_paths[symbol] = root / f"{symbol}-ledger.jsonl"
            ledger_paths["QQQ"].write_text("".join(
                json.dumps(event) + "\n"
                for event in (
                    {"event": "PAPER_ENTRY_REAL_ASK", "positionId": "qqq-1", "timestamp": now.isoformat()},
                    {"event": "PAPER_EXIT_REAL_BID", "positionId": "qqq-1", "pnl": -60.0, "timestamp": now.isoformat()},
                )
            ))
            with (
                patch.object(engine, "state_path", side_effect=lambda symbol: state_paths[symbol]),
                patch.object(engine, "log_path", side_effect=lambda symbol: ledger_paths[symbol]),
                patch.object(engine, "now_et", return_value=now),
            ):
                self.assertEqual(60.0, engine._portfolio_daily_gross_loss())

    def test_portfolio_loss_rejects_corrupt_symbol_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_paths = {}
            ledger_paths = {}
            for symbol in engine.SYMBOLS:
                state_path = root / f"{symbol}-state.json"
                if symbol == "QQQ":
                    state_path.write_text("{not-json")
                else:
                    state_path.write_text(json.dumps(engine.default_state(symbol)))
                state_paths[symbol] = state_path
                ledger_paths[symbol] = root / f"{symbol}-ledger.jsonl"
            with (
                patch.object(engine, "state_path", side_effect=lambda symbol: state_paths[symbol]),
                patch.object(engine, "log_path", side_effect=lambda symbol: ledger_paths[symbol]),
            ):
                with self.assertRaises(engine.StateCorruptionError):
                    engine._portfolio_daily_gross_loss()


class PreflightSafetyTests(unittest.TestCase):
    def test_json_mode_emits_only_one_json_document(self):
        pass_result = lambda *args: {"check": "test", "status": preflight.PASS, "detail": "ok"}
        with (
            patch.object(preflight, "check_state_reset", side_effect=pass_result),
            patch.object(preflight, "check_contract_feasibility", side_effect=pass_result),
            patch.object(preflight, "check_premarket_snapshot", side_effect=pass_result),
            patch.object(preflight, "check_daemon_health", side_effect=pass_result),
            patch.object(preflight, "check_source_hash", side_effect=pass_result),
            patch.object(preflight, "check_macro_calendar", side_effect=pass_result),
            patch.object(preflight, "check_ledger_integrity", side_effect=pass_result),
            patch.object(preflight, "check_cron_health", side_effect=pass_result),
            patch.object(preflight, "check_evidence_gate", side_effect=pass_result),
            patch.object(preflight, "check_brief_coverage", side_effect=pass_result),
            patch.object(preflight, "check_reconcile_bug", side_effect=pass_result),
            patch.object(preflight, "check_otm_adaptive", side_effect=pass_result),
            patch.object(preflight, "check_sleep_interval_bug", side_effect=pass_result),
            patch.object(preflight, "check_anomalous_holds", side_effect=pass_result),
            patch.object(preflight, "check_cold_reset_guard", side_effect=pass_result),
            patch.object(preflight, "check_max_hold_safety_net", side_effect=pass_result),
            patch.object(preflight, "check_runtime_interval", side_effect=pass_result),
            patch.object(preflight, "sys", preflight.sys),
            patch("sys.argv", ["v7_preflight_check.py", "--json"]),
            contextlib.redirect_stdout(stdout := io.StringIO()),
        ):
            self.assertEqual(0, preflight.main())
        payload = json.loads(stdout.getvalue())
        self.assertIsInstance(payload, list)

    def test_known_historical_long_hold_is_warned_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "v7_scalp_trades_QQQ.jsonl").write_text(json.dumps({
                "event": "PAPER_EXIT_REAL_BID",
                "positionId": "paper-20260819T102513",
                "heldMinutes": 329.9,
            }) + "\n")
            with patch.object(preflight, "STATE_DIR", state_dir):
                result = preflight.check_anomalous_holds()

        self.assertEqual("WARN", result["status"])
        self.assertIn("acknowledged historical", result["detail"])

    def test_evidence_gate_excludes_legacy_and_mixed_release_pairs(self):
        current = {
            "engineVersion": engine.ENGINE_VERSION,
            "strategyId": engine.STRATEGY_ID,
            "releaseManifestSha256": engine.RELEASE_MANIFEST_SHA256,
            "sourceCodeSha256": engine.source_code_sha256(),
            "configSha256": engine.config_sha256(),
        }
        legacy = {
            "engineVersion": "legacy",
            "strategyId": "legacy",
            "releaseManifestSha256": "0" * 64,
            "sourceCodeSha256": "0" * 64,
            "configSha256": "0" * 64,
        }
        events = [
            {"event": "PAPER_ENTRY_REAL_ASK", "positionId": "current", **current},
            {"event": "PAPER_EXIT_REAL_BID", "positionId": "current", "pnl": 10.0, **current},
            {"event": "PAPER_ENTRY_REAL_ASK", "positionId": "legacy", **legacy},
            {"event": "PAPER_EXIT_REAL_BID", "positionId": "legacy", "pnl": 100.0, **legacy},
            {"event": "PAPER_ENTRY_REAL_ASK", "positionId": "mixed", **current},
            {"event": "PAPER_EXIT_REAL_BID", "positionId": "mixed", "pnl": 50.0, **legacy},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "v7_scalp_trades_SPY.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events)
            )
            with patch.object(preflight, "STATE_DIR", state_dir):
                result = preflight.check_evidence_gate()

        self.assertIn("exact current-release detail 1/5", result["detail"])
        self.assertIn("1W/0 nonwins", result["detail"])
        self.assertIn("P&L=$10.00", result["detail"])

    def test_source_hash_requires_matching_daemon_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine_path = root / "v7_scalp_paper.py"
            daemon_path = root / "v7_scalp_live_daemon.py"
            engine_path.write_bytes(b"engine source")
            daemon_path.write_bytes(b"daemon source")
            state_dir = root / "state"
            state_dir.mkdir()
            (state_dir / "v7_scalp_live_runtime.json").write_text(json.dumps({
                "engineSourceSha256": hashlib.sha256(engine_path.read_bytes()).hexdigest(),
                "daemonSourceSha256": "0" * 64,
                "configSha256": engine.config_sha256(),
            }))
            with (
                patch.object(preflight, "SCRIPTS_DIR", root),
                patch.object(preflight, "STATE_DIR", state_dir),
            ):
                result = preflight.check_source_hash()

        self.assertEqual("FAIL", result["status"])
        self.assertIn("daemon", result["detail"].lower())

    def test_source_hash_requires_matching_execution_config_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine_path = root / "v7_scalp_paper.py"
            daemon_path = root / "v7_scalp_live_daemon.py"
            engine_path.write_bytes(b"engine source")
            daemon_path.write_bytes(b"daemon source")
            state_dir = root / "state"
            state_dir.mkdir()
            (state_dir / "v7_scalp_live_runtime.json").write_text(json.dumps({
                "engineSourceSha256": hashlib.sha256(engine_path.read_bytes()).hexdigest(),
                "daemonSourceSha256": hashlib.sha256(daemon_path.read_bytes()).hexdigest(),
                "configSha256": "0" * 64,
            }))
            with (
                patch.object(preflight, "SCRIPTS_DIR", root),
                patch.object(preflight, "STATE_DIR", state_dir),
            ):
                result = preflight.check_source_hash()

        self.assertEqual("FAIL", result["status"])
        self.assertIn("config", result["detail"].lower())


class DaemonPriorityTests(unittest.TestCase):
    def test_active_position_symbol_runs_before_idle_symbol(self):
        # V7.5.2 universe is SPY+QQQ; IWM/XSP were removed. Fixtures stay
        # within the active registry so this guard keeps covering the
        # active-position priority behavior of the deployed daemon.
        states = {
            "SPY": {"activePositions": []},
            "QQQ": {"activePositions": [{"positionId": "open-qqq"}]},
        }
        order_fn = getattr(live_daemon, "symbol_iteration_order", lambda _: list(engine.SYMBOLS))

        result = order_fn(states)
        self.assertEqual("QQQ", result[0])
        self.assertLess(result.index("QQQ"), result.index("SPY"))
        self.assertEqual(sorted(engine.SYMBOLS), sorted(result))

        # Priority must flip with the position: SPY holding the open
        # position leads while QQQ idles.
        flipped = order_fn({
            "SPY": {"activePositions": [{"positionId": "open-spy"}]},
            "QQQ": {"activePositions": []},
        })
        self.assertEqual("SPY", flipped[0])
        self.assertLess(flipped.index("SPY"), flipped.index("QQQ"))

    def test_run_loop_does_not_write_production_scan_audit(self):
        """The priority-order run_loop test must not pollute the production audit.

        That test drives the real daemon loop with mocked engine calls. An
        unmocked photon._audit previously appended fabricated
        ``{"action": "NONE"}`` rows (including stale SPY/IWM/XSP symbols) to
        the live scan-attempts file on every suite run. This guard re-runs the
        sibling test and fails if the production audit changes.
        """
        audit_path = photon.SCAN_AUDIT
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        baseline = audit_path.read_text() if audit_path.exists() else ""

        result = unittest.TestResult()
        sibling = DaemonPriorityTests("test_run_loop_uses_position_priority_order")
        sibling.run(result)
        self.assertEqual([], result.failures + result.errors)

        after = audit_path.read_text() if audit_path.exists() else ""
        self.assertEqual(baseline, after)

    def test_run_loop_uses_position_priority_order(self):
        states = {
            "SPY": {"activePositions": [], "dailyTrades": 0},
            "QQQ": {
                "activePositions": [{
                    "positionId": "open-qqq",
                    "entryTime": datetime.now(engine.ET).isoformat(),
                }],
                "dailyTrades": 0,
            },
        }
        calls = []

        class FakeRuntime:
            last_equity_quote = {}

            def install(self):
                pass

            def restore(self):
                pass

            def poll_equity(self, _symbol):
                return {"mid": 1.0}

            def cold_reset(self):
                pass

        def mode_for(state, _now, **_kw):
            if state.get("activePositions"):
                return live_daemon.ModeDecision("position", 1.0, "monitor", "open")
            return live_daemon.ModeDecision("scan", 1.0, "scan", "idle")

        expected_order = ["QQQ", "SPY"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(live_daemon, "LOCK_PATH", root / "daemon.lock"),
                patch.object(live_daemon, "PID_PATH", root / "daemon.pid"),
                patch.object(live_daemon, "LiveRuntime", FakeRuntime),
                patch.object(live_daemon, "_initialize_state"),
                patch.object(live_daemon, "_read_state", side_effect=lambda symbol: states[symbol]),
                patch.object(live_daemon, "choose_mode", side_effect=mode_for),
                patch.object(live_daemon, "symbol_iteration_order", return_value=expected_order),
                patch.object(live_daemon, "_capture_engine_call", side_effect=lambda action, symbol: (calls.append((action, symbol)) or (0, {"action": "NONE"}, ""))),
                patch.object(live_daemon.photon, "_audit"),
                patch.object(live_daemon, "_sleep_until"),
                patch.object(live_daemon, "_append_event"),
                patch.object(live_daemon, "write_runtime_status"),
                patch.object(live_daemon, "_STOP", False),
            ):
                self.assertEqual(0, live_daemon.run_loop(max_iterations=1))

        self.assertEqual([("monitor", "QQQ"), ("scan", "SPY")], calls)


class QuoteWorkerRetryTests(unittest.TestCase):
    def test_transient_502_is_retried_on_a_fresh_worker(self):
        runtime = live_daemon.LiveRuntime()
        first_worker = Mock()
        first_worker.call.side_effect = RuntimeError(
            "mcp_dispatch_error:API error 502: nginx"
        )
        second_worker = Mock()
        expected = {"kind": "equity_quote", "symbol": "SPY"}
        second_worker.call.return_value = expected

        with (
            patch.object(
                runtime, "_start_quote_worker",
                side_effect=[first_worker, second_worker],
            ),
            patch.object(runtime, "_close_quote_worker") as close_worker,
            patch.object(live_daemon.time, "sleep"),
        ):
            result = runtime._worker_call({"op": "equity", "symbol": "SPY"})

        self.assertEqual(expected, result)
        first_worker.call.assert_called_once_with({"op": "equity", "symbol": "SPY"})
        second_worker.call.assert_called_once_with({"op": "equity", "symbol": "SPY"})
        self.assertEqual(1, close_worker.call_count)

    def test_transient_quote_gap_is_retried_on_a_fresh_worker(self):
        runtime = live_daemon.LiveRuntime()
        first_worker = Mock()
        first_worker.call.side_effect = RuntimeError(
            "index_parity_no_valid_executable_pair"
        )
        second_worker = Mock()
        expected = {"kind": "equity_quote", "symbol": "QQQ"}
        second_worker.call.return_value = expected

        with (
            patch.object(
                runtime, "_start_quote_worker",
                side_effect=[first_worker, second_worker],
            ),
            patch.object(runtime, "_close_quote_worker") as close_worker,
            patch.object(live_daemon.time, "sleep"),
        ):
            result = runtime._worker_call({"op": "equity", "symbol": "QQQ"})

        self.assertEqual(expected, result)
        first_worker.call.assert_called_once_with({"op": "equity", "symbol": "QQQ"})
        second_worker.call.assert_called_once_with({"op": "equity", "symbol": "QQQ"})
        close_worker.assert_called_once_with()

    def test_non_transient_bridge_error_is_not_retried(self):
        runtime = live_daemon.LiveRuntime()
        worker = Mock()
        worker.call.side_effect = RuntimeError("invalid_live_equity_quote")

        with (
            patch.object(runtime, "_start_quote_worker", return_value=worker),
            patch.object(runtime, "_close_quote_worker") as close_worker,
            patch.object(live_daemon.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid_live_equity_quote"):
                runtime._worker_call({"op": "equity", "symbol": "SPY"})

        worker.call.assert_called_once_with({"op": "equity", "symbol": "SPY"})
        close_worker.assert_called_once_with()
        sleep.assert_not_called()

    def test_persistent_502_exhausts_bounded_retries_and_fails_closed(self):
        runtime = live_daemon.LiveRuntime()
        workers = [Mock(), Mock(), Mock()]
        for worker in workers:
            worker.call.side_effect = RuntimeError(
                "mcp_dispatch_error:API error 502: nginx"
            )

        with (
            patch.object(runtime, "_start_quote_worker", side_effect=workers),
            patch.object(runtime, "_close_quote_worker") as close_worker,
            patch.object(live_daemon.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "API error 502"):
                runtime._worker_call({"op": "index", "symbol": "XSP"})

        self.assertEqual(3, sum(worker.call.call_count for worker in workers))
        self.assertEqual(3, close_worker.call_count)
        self.assertEqual(2, sleep.call_count)
        sleep.assert_has_calls([unittest.mock.call(0.5), unittest.mock.call(1.0)])


class StateDurabilityTests(unittest.TestCase):
    def test_unmatched_ledger_exit_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "trades.jsonl"
            ledger.write_text(__import__("json").dumps({
                "event": "PAPER_EXIT_REAL_BID", "positionId": "missing",
                "pnl": -10.0, "timestamp": "2026-08-07T12:00:00-04:00",
            }) + "\n")
            with patch.object(engine, "log_path", return_value=ledger):
                with self.assertRaises(engine.StateCorruptionError):
                    engine.reconcile_state_from_ledger(
                        engine.default_state(),
                        now=datetime(2026, 8, 7, 15, 0, tzinfo=engine.ET),
                    )

    def test_ledger_rebuilds_consecutive_loss_streak(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "trades.jsonl"
            events = [
                {"event": "PAPER_ENTRY_REAL_ASK", "positionId": "p1", "timestamp": "2026-08-07T09:55:00-04:00"},
                {"event": "PAPER_EXIT_REAL_BID", "positionId": "p1", "pnl": 12.0, "timestamp": "2026-08-07T10:00:00-04:00"},
                {"event": "PAPER_ENTRY_REAL_ASK", "positionId": "p2", "timestamp": "2026-08-07T10:55:00-04:00"},
                {"event": "PAPER_EXIT_REAL_BID", "positionId": "p2", "pnl": -8.0, "timestamp": "2026-08-07T11:00:00-04:00"},
                {"event": "PAPER_ENTRY_REAL_ASK", "positionId": "p3", "timestamp": "2026-08-07T11:55:00-04:00"},
                {"event": "PAPER_EXIT_REAL_BID", "positionId": "p3", "pnl": -4.0, "timestamp": "2026-08-07T12:00:00-04:00"},
            ]
            ledger.write_text("".join(__import__("json").dumps(event) + "\n" for event in events))
            state = engine.default_state()
            state["consecutiveLosses"] = 0
            with patch.object(engine, "log_path", return_value=ledger):
                rebuilt = engine.reconcile_state_from_ledger(
                    state,
                    now=datetime(2026, 8, 7, 15, 0, tzinfo=engine.ET),
                )
            self.assertEqual(2, rebuilt["consecutiveLosses"])
            self.assertEqual(0.0, rebuilt["dailyPnl"])
            self.assertEqual(12.0, rebuilt["dailyGrossLoss"])

    def test_state_lock_rejects_overlapping_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "state.lock"
            with patch.object(engine, "state_lock_path", return_value=lock_path):
                with engine.exclusive_state_lock():
                    with self.assertRaises(engine.StateLockBusy):
                        with engine.exclusive_state_lock():
                            pass

    def test_atomic_json_write_replaces_complete_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text('{"old":true}\n')
            engine.atomic_write_json(state_path, {"new": True})
            self.assertEqual({"new": True}, __import__("json").loads(state_path.read_text()))
            self.assertFalse(state_path.with_suffix(".json.tmp").exists())

    def test_load_state_normalizes_stale_static_config_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "state.json"
            stale = engine.default_state("QQQ")
            stale.update({
                "version": "old-release",
                "strategyId": "old-strategy",
                "sourceCodeSha256": "0" * 64,
                "configSha256": "1" * 64,
                "maxDebit": 100.0,
            })
            state_path.write_text(__import__("json").dumps(stale, indent=2) + "\n")
            before = state_path.read_bytes()
            with (
                patch.object(engine, "state_path", return_value=state_path),
                patch.object(engine, "log_path", return_value=root / "missing-ledger.jsonl"),
            ):
                loaded = engine.load_state("QQQ")

            self.assertEqual(engine.SYMBOLS["QQQ"].max_debit, loaded["maxDebit"])
            self.assertTrue(loaded.get("_stateMetadataNeedsPersist"))
            self.assertEqual(before, state_path.read_bytes())

    def test_daemon_state_read_persists_normalized_static_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "state.json"
            lock_path = root / "state.lock"
            stale = engine.default_state("QQQ")
            stale.update({
                "lastResetDate": engine.today_key(),
                "version": "old-release",
                "strategyId": "old-strategy",
                "sourceCodeSha256": "0" * 64,
                "configSha256": "1" * 64,
                "maxDebit": 100.0,
            })
            state_path.write_text(__import__("json").dumps(stale, indent=2) + "\n")
            with (
                patch.object(engine, "state_path", return_value=state_path),
                patch.object(engine, "state_lock_path", return_value=lock_path),
                patch.object(engine, "log_path", return_value=root / "missing-ledger.jsonl"),
            ):
                loaded = live_daemon._read_state("QQQ")

            persisted = __import__("json").loads(state_path.read_text())
            self.assertEqual(engine.SYMBOLS["QQQ"].max_debit, loaded["maxDebit"])
            self.assertEqual(engine.SYMBOLS["QQQ"].max_debit, persisted["maxDebit"])
            self.assertNotIn("_stateMetadataNeedsPersist", persisted)

    def test_daily_report_does_not_mutate_engine_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "state.json"
            lock_path = root / "state.lock"
            state = engine.default_state()
            state["stateRevision"] = 17
            state_path.write_text(__import__("json").dumps(state, indent=2) + "\n")
            before = state_path.read_bytes()
            with (
                patch.object(engine, "state_path", return_value=state_path),
                patch.object(engine, "state_lock_path", return_value=lock_path),
                patch.object(engine, "log_path", return_value=root / "missing-ledger.jsonl"),
                patch("sys.stdout", new_callable=__import__("io").StringIO),
            ):
                self.assertEqual(0, engine.report())
            self.assertEqual(before, state_path.read_bytes())

    def test_empty_state_object_fails_schema_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "state.json"
            state_path.write_text("{}\n")
            with (
                patch.object(engine, "state_path", return_value=state_path),
                patch.object(engine, "log_path", return_value=root / "missing-ledger.jsonl"),
            ):
                with self.assertRaises(engine.StateCorruptionError):
                    engine.load_state()

    def test_corrupt_state_fails_closed_instead_of_resetting(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text("{not-json")
            with patch.object(engine, "state_path", return_value=state_path):
                with self.assertRaises(engine.StateCorruptionError):
                    engine.load_state()

    def test_negative_daily_gross_loss_fails_schema_validation(self):
        state = engine.default_state()
        state["dailyGrossLoss"] = -1.0
        with self.assertRaisesRegex(engine.StateCorruptionError, "dailyGrossLoss"):
            engine.validate_state_schema(state)


class AdaptiveDaemonTests(unittest.TestCase):
    def test_runtime_status_preserves_reason_when_all_symbols_are_intentionally_idle(self):
        decisions = [
            live_daemon.ModeDecision("off_hours", 300.0, None, "outside_entry_window"),
            live_daemon.ModeDecision("off_hours", 300.0, None, "outside_entry_window"),
        ]
        self.assertEqual(
            ("off_hours", "outside_entry_window"),
            live_daemon.aggregate_runtime_mode(decisions),
        )

    def test_safety_net_state_corruption_is_observable_and_keeps_health_unhealthy(self):
        states = {symbol: {"activePositions": [], "dailyTrades": 0} for symbol in engine.SYMBOLS}
        read_count = 0

        def read_state(symbol):
            nonlocal read_count
            read_count += 1
            # Priority reads happen first; the fourth read is QQQ's safety-net
            # read after SPY's healthy symbol iteration has already completed.
            if read_count == 4 and symbol == "QQQ":
                raise engine.StateCorruptionError("state_invalid_nonnegative_number:dailyGrossLoss")
            return states[symbol]

        class FakeRuntime:
            last_equity_quote = {}

            def install(self):
                pass

            def restore(self):
                pass

            def cold_reset(self):
                pass

        def mode_for(_state, _now, **_kwargs):
            return live_daemon.ModeDecision("active_scan", 1.0, "scan", "test")

        def engine_call(_action, symbol):
            if symbol == "SPY":
                raise RuntimeError("healthy-symbol-test-failure")
            return 0, {"action": "NONE"}, ""

        events = []
        statuses = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(live_daemon, "LOCK_PATH", root / "daemon.lock"),
                patch.object(live_daemon, "PID_PATH", root / "daemon.pid"),
                patch.object(live_daemon, "LiveRuntime", FakeRuntime),
                patch.object(live_daemon, "SPREAD_PAPER_ENABLED", False),
                patch.object(live_daemon, "_initialize_state"),
                patch.object(live_daemon, "_read_state", side_effect=read_state),
                patch.object(live_daemon, "choose_mode", side_effect=mode_for),
                patch.object(live_daemon, "_capture_engine_call", side_effect=engine_call),
                patch.object(live_daemon.photon, "_audit"),
                patch.object(live_daemon, "_sleep_until"),
                patch.object(live_daemon, "_append_event", side_effect=events.append),
                patch.object(live_daemon, "write_runtime_status", side_effect=statuses.append),
                patch.object(live_daemon, "_STOP", False),
            ):
                self.assertEqual(0, live_daemon.run_loop(max_iterations=1))

        self.assertTrue(any(event.get("event") == "SYMBOL_ITERATION_ERROR" for event in events))
        self.assertTrue(any(event.get("event") == "MAX_HOLD_SAFETY_NET_STATE_ERROR" for event in events))
        health_records = [record for record in statuses if "consecutiveFailures" in record]
        self.assertTrue(health_records)
        self.assertGreater(health_records[0]["consecutiveFailures"], 0)

    def test_runtime_status_publishes_process_frozen_source_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime_path = Path(tmp) / "runtime.json"
            with (
                patch.object(live_daemon, "RUNTIME_STATUS_PATH", runtime_path),
                patch.object(engine, "source_code_sha256", return_value="f" * 64),
                patch.object(engine, "config_sha256", return_value="e" * 64),
            ):
                live_daemon.write_runtime_status({"processId": 12345})

            record = __import__("json").loads(runtime_path.read_text())

        self.assertEqual(
            live_daemon.PROCESS_ENGINE_SOURCE_SHA256,
            record["engineSourceSha256"],
        )
        self.assertEqual(
            live_daemon.PROCESS_DAEMON_SOURCE_SHA256,
            record["daemonSourceSha256"],
        )
        self.assertEqual(
            live_daemon.PROCESS_CONFIG_SHA256,
            record["configSha256"],
        )
        self.assertEqual(engine.STRATEGY_ID, record["strategyId"])
        self.assertNotEqual("f" * 64, record["engineSourceSha256"])
        self.assertNotEqual("e" * 64, record["configSha256"])
        self.assertTrue(all(
            len(record[field]) == 64
            for field in ("engineSourceSha256", "daemonSourceSha256", "configSha256")
        ))

    def test_position_mode_polls_every_second(self):
        decision = live_daemon.choose_mode(
            {"activePositions": [{"positionId": "paper-1"}], "dailyTrades": 1},
            datetime(2026, 8, 11, 10, 0, tzinfo=engine.ET),
        )
        self.assertEqual(("position", 1.0, "monitor"),
                         (decision.mode, decision.interval_seconds, decision.action))

    @unittest.skip("XSP removed from active universe")
    def test_xsp_daemon_has_no_count_cap_and_uses_index_worker_for_rescaled_signal_bars(self):
        monday = datetime(2026, 8, 10, 10, 20, tzinfo=engine.ET)
        decision = live_daemon.choose_mode(
            {"activePositions": [], "dailyTrades": 99}, monday, symbol="XSP",
        )
        self.assertEqual(("active_scan", 1.0, "scan"),
                         (decision.mode, decision.interval_seconds, decision.action))

        raw_bars = [
            {
                "dt": datetime(2026, 8, 10, 10, 15, tzinfo=engine.ET),
                "date": "2026-08-10", "time": 1015,
                "open": 7598.0, "high": 7602.0, "low": 7596.0,
                "close": 7600.0, "volume": 1234.0,
            },
            {
                "dt": datetime(2026, 8, 10, 10, 20, tzinfo=engine.ET),
                "date": "2026-08-10", "time": 1020,
                "open": 7600.0, "high": 7604.0, "low": 7599.0,
                "close": 7601.0, "volume": 1500.0,
            },
        ]
        payload = {
            "kind": "index_quote", "source": "Robinhood MCP", "symbol": "XSP",
            "timestamp": "2026-08-10T14:20:00Z", "fetched_at": "2026-08-10T14:20:00Z",
            "quote": {"symbol": "XSP", "bid_price": 760.10, "ask_price": 760.20,
                      "updated_at": "2026-08-10T14:20:00Z"},
        }
        runtime = live_daemon.LiveRuntime()
        runtime._original_fetch_bars = Mock(return_value=raw_bars)
        runtime._worker_call = Mock(return_value=payload)

        quote = runtime.poll_equity("XSP")
        scaled = runtime.fetch_bars("XSP", force=True)

        self.assertAlmostEqual(760.15, quote["mid"])
        self.assertAlmostEqual(760.15, scaled[-1]["close"], places=6)
        self.assertEqual("ES=F", scaled[-1]["signalFeed"])
        requests = [call.args[0] for call in runtime._worker_call.call_args_list]
        self.assertTrue(requests and all(request["op"] == "index" for request in requests))
        self.assertTrue(all(request["symbol"] == "XSP" for request in requests))
        self.assertTrue(all(abs(request["reference_underlying"] - 760.1) < 0.01 for request in requests))

    @unittest.skip("XSP removed from active universe")
    def test_xsp_daemon_stays_off_hours_during_broker_quote_gap(self):
        sunday = datetime(2026, 8, 9, 20, 20, tzinfo=engine.ET)
        decision = live_daemon.choose_mode(
            {"activePositions": [], "dailyTrades": 0}, sunday, symbol="XSP",
        )
        self.assertEqual("off_hours", decision.mode)
        self.assertEqual("market_closed", decision.reason)

    def test_active_scanner_polls_every_second(self):
        decision = live_daemon.choose_mode(
            {"activePositions": [], "dailyTrades": 0},
            datetime(2026, 8, 11, 10, 0, tzinfo=engine.ET),
        )
        self.assertEqual(("active_scan", 1.0, "scan"),
                         (decision.mode, decision.interval_seconds, decision.action))

    def test_opening_watch_transitions_exactly_to_entry_start(self):
        before = live_daemon.choose_mode(
            {"activePositions": [], "dailyTrades": 0},
            datetime(2026, 8, 11, 9, 34, 59, 500000, tzinfo=engine.ET),
        )
        at_start = live_daemon.choose_mode(
            {"activePositions": [], "dailyTrades": 0},
            datetime(2026, 8, 11, 9, 35, 0, tzinfo=engine.ET),
        )
        self.assertEqual(("opening_watch", 1.0, "premarket"),
                         (before.mode, before.interval_seconds, before.action))
        self.assertEqual(("active_scan", 1.0, "scan"),
                         (at_start.mode, at_start.interval_seconds, at_start.action))

    def test_scanner_near_limit_then_relaxes_at_cap(self):
        now = datetime(2026, 8, 11, 10, 0, tzinfo=engine.ET)
        after_two = live_daemon.choose_mode({"activePositions": [], "dailyTrades": 2}, now)
        after_three = live_daemon.choose_mode({"activePositions": [], "dailyTrades": 3}, now)
        after_five = live_daemon.choose_mode({"activePositions": [], "dailyTrades": 5}, now)
        self.assertEqual(("near_limit_scan", 1.0, "scan"),
                         (after_two.mode, after_two.interval_seconds, after_two.action))
        self.assertEqual(("near_limit_scan", 1.0, "scan"),
                         (after_three.mode, after_three.interval_seconds, after_three.action))
        self.assertEqual("relaxed", after_five.mode)
        self.assertIsNone(after_five.action)

    def test_completed_trade_gets_immediate_resource_cooldown(self):
        now = datetime(2026, 8, 11, 10, 0, tzinfo=engine.ET)
        decision = live_daemon.choose_mode({
            "activePositions": [], "dailyTrades": 1,
            "lastExitAt": datetime(2026, 8, 11, 9, 59, 45, tzinfo=engine.ET).isoformat(),
        }, now)
        self.assertEqual(("post_trade_relaxed", 30.0, None),
                         (decision.mode, decision.interval_seconds, decision.action))

    def test_error_backoff_is_bounded(self):
        self.assertEqual(1.0, live_daemon.failure_interval(1.0, 0))
        self.assertEqual(2.0, live_daemon.failure_interval(1.0, 1))
        self.assertEqual(30.0, live_daemon.failure_interval(1.0, 99))

    def test_only_material_events_are_emitted(self):
        self.assertTrue(live_daemon.should_emit({"action": "PAPER_ENTRY"}, 0))
        self.assertTrue(live_daemon.should_emit({"action": "PAPER_EXIT"}, 0))
        self.assertFalse(live_daemon.should_emit({"action": "HOLD"}, 0))
        self.assertFalse(live_daemon.should_emit({"action": "SKIP"}, 0))

    def test_cold_reset_terminates_quote_worker_and_drops_caches(self):
        runtime = live_daemon.LiveRuntime()
        worker = Mock()
        runtime._quote_worker = worker
        runtime._bar_cache = [{"close": 100.0}]
        runtime.last_equity_quote = {"bid": 100.0}

        runtime.cold_reset()

        worker.close.assert_called_once_with()
        self.assertIsNone(runtime._quote_worker)
        self.assertEqual({}, runtime._bar_cache)
        self.assertEqual({}, runtime.last_equity_quote)


class ReentryPolicyTests(unittest.TestCase):
    """V7.5.3 re-entry risk policy: same-bar dedup + post-loss same-direction delay."""

    @staticmethod
    def _setup(direction="bearish", bar="2026-10-12T09:55:00-04:00"):
        return {"direction": direction, "signalBarEnd": bar}

    @staticmethod
    def _state(**overrides):
        state = engine.default_state("QQQ")
        state.update(overrides)
        return state

    def test_duplicate_signal_bar_entry_is_blocked(self):
        state = self._state(lastEnteredSignalBarEnd="2026-10-12T09:55:00-04:00")
        now = datetime(2026, 10, 12, 13, 5, tzinfo=engine.ET)
        self.assertEqual(
            "duplicate_signal_bar_entry",
            engine.reentry_policy_block(state, self._setup(), now=now),
        )

    def test_entry_on_a_new_signal_bar_is_allowed(self):
        state = self._state(lastEnteredSignalBarEnd="2026-10-12T09:55:00-04:00")
        now = datetime(2026, 10, 12, 13, 5, tzinfo=engine.ET)
        self.assertIsNone(
            engine.reentry_policy_block(
                state, self._setup(bar="2026-10-12T10:00:00-04:00"), now=now,
            )
        )

    def test_same_direction_entry_blocked_within_delay_after_loss(self):
        state = self._state(
            lastLossExitAt="2026-10-12T13:01:00-04:00",
            lastLossDirection="bearish",
        )
        now = datetime(2026, 10, 12, 13, 4, tzinfo=engine.ET)
        self.assertEqual(
            "post_loss_same_direction_delay",
            engine.reentry_policy_block(state, self._setup(), now=now),
        )

    def test_opposite_direction_entry_allowed_within_delay(self):
        state = self._state(
            lastLossExitAt="2026-10-12T13:01:00-04:00",
            lastLossDirection="bearish",
        )
        now = datetime(2026, 10, 12, 13, 4, tzinfo=engine.ET)
        self.assertIsNone(
            engine.reentry_policy_block(
                state, self._setup(direction="bullish"), now=now,
            )
        )

    def test_same_direction_entry_allowed_after_delay_expires(self):
        state = self._state(
            lastLossExitAt="2026-10-12T13:01:00-04:00",
            lastLossDirection="bearish",
        )
        now = datetime(2026, 10, 12, 13, 7, tzinfo=engine.ET)
        self.assertIsNone(engine.reentry_policy_block(state, self._setup(), now=now))

    def test_missing_policy_state_allows_entry(self):
        now = datetime(2026, 10, 12, 13, 5, tzinfo=engine.ET)
        self.assertIsNone(
            engine.reentry_policy_block(self._state(), self._setup(), now=now)
        )

    def test_malformed_loss_timestamp_fails_open(self):
        state = self._state(lastLossExitAt="not-a-timestamp", lastLossDirection="bearish")
        now = datetime(2026, 10, 12, 13, 4, tzinfo=engine.ET)
        self.assertIsNone(engine.reentry_policy_block(state, self._setup(), now=now))

    def test_entry_recording_remembers_signal_bar(self):
        state = self._state()
        position = {"signal": {"signalBarEnd": "2026-10-12T09:55:00-04:00"}}
        engine.record_entry_reentry_state(state, position)
        self.assertEqual("2026-10-12T09:55:00-04:00", state["lastEnteredSignalBarEnd"])

    def test_entry_recording_ignores_positions_without_bar(self):
        state = self._state()
        engine.record_entry_reentry_state(state, {"signal": {}})
        self.assertIsNone(state.get("lastEnteredSignalBarEnd"))

    def test_exit_recording_tracks_loss_and_direction(self):
        state = self._state()
        position = {"direction": "bearish", "signal": {"direction": "bearish"}}
        exit_at = datetime(2026, 10, 12, 13, 1, tzinfo=engine.ET)
        engine.record_exit_reentry_state(state, position, -12.0, exit_at)
        self.assertEqual("bearish", state["lastLossDirection"])
        self.assertEqual("2026-10-12T13:01:00-04:00", state["lastLossExitAt"])

    def test_exit_recording_ignores_wins(self):
        state = self._state()
        position = {"direction": "bearish", "signal": {"direction": "bearish"}}
        exit_at = datetime(2026, 10, 12, 13, 1, tzinfo=engine.ET)
        engine.record_exit_reentry_state(state, position, 4.0, exit_at)
        self.assertIsNone(state.get("lastLossExitAt"))
        self.assertIsNone(state.get("lastLossDirection"))

    def test_default_state_carries_reentry_policy_fields(self):
        state = engine.default_state("QQQ")
        for field in ("lastEnteredSignalBarEnd", "lastLossExitAt", "lastLossDirection"):
            self.assertIn(field, state)
            self.assertIsNone(state[field])


class QuoteFreshnessTests(unittest.TestCase):
    """V7.5.4 near-live pricing: tightened quote/bar staleness windows."""

    def test_v754_entry_option_quote_age_is_15_seconds(self):
        self.assertEqual(15, engine.MAX_ENTRY_QUOTE_AGE_SECONDS)

    def test_v754_exit_option_quote_age_is_15_seconds(self):
        self.assertEqual(15, engine.MAX_EXIT_QUOTE_AGE_SECONDS)

    def test_v754_completed_bar_age_restricted_to_freshest_bar(self):
        self.assertEqual(300, engine.MAX_COMPLETED_BAR_AGE_SECONDS)

    def test_fresh_completed_bar_passes_and_older_bar_is_rejected(self):
        from datetime import timedelta
        now = datetime(2026, 10, 12, 15, 0, tzinfo=engine.ET)
        fresh = [{"dt": now - timedelta(seconds=200 + 300)}]  # bar ended 200s ago
        passed, reason = engine.validate_completed_bar_freshness(fresh, now=now, symbol="SPY")
        self.assertTrue(passed)
        self.assertEqual("ok", reason)
        stale = [{"dt": now - timedelta(seconds=320 + 300)}]  # bar ended 320s ago
        passed2, reason2 = engine.validate_completed_bar_freshness(stale, now=now, symbol="SPY")
        self.assertFalse(passed2)
        self.assertEqual("stale_completed_bar", reason2)

    def test_quote_worker_cold_reset_has_idle_grace(self):
        import v7_scalp_live_daemon as daemon
        self.assertEqual(120.0, daemon.WORKER_COLD_RESET_IDLE_GRACE_SECONDS)
        self.assertFalse(daemon.worker_cold_reset_due(60.0, False))
        self.assertTrue(daemon.worker_cold_reset_due(130.0, False))
        self.assertFalse(daemon.worker_cold_reset_due(300.0, True))


class NormalPathMomentumGateTests(unittest.TestCase):
    """V7.5.4: momentum required on the normal signal path (consistency with V7.5.3)."""

    @staticmethod
    def _setup(momentum=True, direction="bullish", **overrides):
        setup = {
            "direction": direction,
            "bullChecks": {"momentum": momentum},
            "bearChecks": {"momentum": momentum},
            "rejectionReasons": [],
        }
        setup.update(overrides)
        return setup

    def test_momentum_confirmed_normal_setup_passes(self):
        result = engine.apply_normal_path_momentum_gate(self._setup(momentum=True))
        self.assertEqual("bullish", result["direction"])

    def test_momentum_failed_normal_setup_is_rejected(self):
        result = engine.apply_normal_path_momentum_gate(self._setup(momentum=False))
        self.assertIsNone(result["direction"])
        self.assertIn("momentum_not_confirmed", result["rejectionReasons"])

    def test_confluence_setups_are_exempt(self):
        result = engine.apply_normal_path_momentum_gate(
            self._setup(momentum=False, trendConfluence=True)
        )
        self.assertEqual("bullish", result["direction"])

    def test_detector_setups_are_exempt(self):
        result = engine.apply_normal_path_momentum_gate(
            self._setup(momentum=False, detector="crash_detector")
        )
        self.assertEqual("bullish", result["direction"])

    def test_directionless_setup_passes_through(self):
        result = engine.apply_normal_path_momentum_gate(self._setup(direction=None))
        self.assertIsNone(result["direction"])


class BridgeCursorPaginationTests(unittest.TestCase):
    """Robinhood MCP now returns bare cursor tokens in `next`, not URLs."""

    def test_bare_cursor_token_is_returned_verbatim(self):
        self.assertEqual(
            quote_bridge._cursor_from_next("cD02NTUuMDAwMA=="),
            "cD02NTUuMDAwMA==",
        )

    def test_url_style_cursor_still_extracts_query_param(self):
        self.assertEqual(
            quote_bridge._cursor_from_next("https://api.example.com/instruments/?cursor=abc%3D%3D"),
            "abc==",
        )

    def test_empty_and_none_cursors_fail_closed(self):
        self.assertIsNone(quote_bridge._cursor_from_next(None))
        self.assertIsNone(quote_bridge._cursor_from_next(""))
        self.assertIsNone(quote_bridge._cursor_from_next(123))


if __name__ == "__main__":
    unittest.main()
