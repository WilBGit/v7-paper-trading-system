#!/usr/bin/env python3
"""Regression tests for the V7 system health gate."""
from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

import v7_system_health_check as health


ET = ZoneInfo("America/New_York")


class HealthGateTests(unittest.TestCase):
    def test_open_position_is_not_reported_as_orphaned(self):
        state = {"activePositions": [{"positionId": "paper-open"}]}
        events = "\n".join(
            [
                json.dumps({"event": "PAPER_ENTRY_REAL_ASK", "positionId": "paper-open"}),
            ]
        )

        result = health.inspect_ledger("SPY", state, events)

        self.assertTrue(result["healthy"])
        self.assertEqual([], result["orphaned"])

    def test_orphaned_entry_is_unhealthy(self):
        state = {"activePositions": []}
        events = "\n".join(
            [
                json.dumps({"event": "PAPER_ENTRY_REAL_ASK", "positionId": "paper-orphan"}),
            ]
        )

        result = health.inspect_ledger("SPY", state, events)

        self.assertFalse(result["healthy"])
        self.assertEqual(["paper-orphan"], result["orphaned"])

    def test_stale_runtime_is_critical(self):
        now = datetime(2026, 8, 28, 12, 0, tzinfo=ET)
        runtime = {
            "updatedAt": "2026-08-28T11:56:00-04:00",
            "consecutiveFailures": 0,
            "liveOrdersEnabled": False,
            "processId": 123}

        result = health.inspect_runtime(runtime, now=now, max_age_seconds=120)

        self.assertFalse(result["healthy"])
        self.assertIn("heartbeat_stale", result["failures"])

    def test_runtime_release_identity_checks_version_and_strategy_id(self):
        import v7_scalp_paper as engine

        runtime = {
            "engineVersion": "old-engine",
            "strategyId": "old-strategy",
            "engineSourceSha256": hashlib.sha256(
                (SCRIPTS_DIR / "v7_scalp_paper.py").read_bytes()
            ).hexdigest(),
            "daemonSourceSha256": hashlib.sha256(
                (SCRIPTS_DIR / "v7_scalp_live_daemon.py").read_bytes()
            ).hexdigest(),
            "configSha256": engine.config_sha256(),
            "releaseManifestSha256": engine.RELEASE_MANIFEST_SHA256}

        result = health.check_release_identity(runtime)

        self.assertFalse(result["healthy"], result)
        self.assertEqual(
            {"engineVersion_mismatch", "strategyId_mismatch"},
            set(result["failures"]),
        )

    def test_off_hours_does_not_require_static_state_file_freshness(self):
        now = datetime(2026, 8, 28, 12, 0, tzinfo=ET)
        runtime = {"mode": "off_hours", "interval_seconds": 30.0}
        import v7_scalp_paper as engine
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            for symbol in engine.SYMBOLS:
                state = {
                    "symbol": symbol,
                    "activePositions": [],
                    "liveOrdersEnabled": False,
                    "lastResetDate": engine.trading_session_key(symbol, now),
                    "updatedAt": "2026-08-27T12:00:00-04:00"}
                (state_dir / f"v7_scalp_state_{symbol}.json").write_text(json.dumps(state))
                (state_dir / f"v7_scalp_trades_{symbol}.jsonl").write_text("")
            with patch.object(health, "STATE_DIR", state_dir):
                result, _children = health.check_states_and_ledgers(runtime, now=now)

        self.assertTrue(result["healthy"], result)

    def test_active_scan_rejects_stale_equity_quotes(self):
        now = datetime(2026, 8, 28, 12, 0, tzinfo=ET)
        old = "2026-08-28T11:58:00-04:00"
        runtime = {
            "mode": "active_scan",
            "liveEquity": {
                **{
                    symbol: {"fetchedAt": old}
                    for symbol in ("SPY", "QQQ")
                }}}
        result = health.check_live_equity_quotes(runtime, now=now)
        self.assertFalse(result["healthy"], result)
        self.assertEqual(
            {"equity_quote_stale:SPY", "equity_quote_stale:QQQ"},
            set(result["failures"]),
        )

    def test_active_position_does_not_require_equity_quote(self):
        now = datetime(2026, 8, 28, 12, 0, tzinfo=ET)
        runtime = {
            "mode": "active_scan",
            "symbols": {
                "SPY": {"mode": "active_scan", "action": "scan", "activePositions": 0},
                "QQQ": {"mode": "position", "action": "monitor", "activePositions": 1}},
            "liveEquity": {
                "SPY": {"fetchedAt": "2026-08-28T12:00:00-04:00"},
                "QQQ": {"fetchedAt": "2026-08-28T11:50:00-04:00"}}}

        result = health.check_live_equity_quotes(runtime, now=now)

        self.assertTrue(result["healthy"], result)
        self.assertEqual([], result["failures"])
        self.assertIn("required=SPY", result["detail"])

    def test_post_trade_relaxed_symbol_does_not_require_equity_quote(self):
        now = datetime(2026, 8, 28, 12, 0, tzinfo=ET)
        runtime = {
            "mode": "active_scan",
            "symbols": {
                "SPY": {"mode": "active_scan", "action": "scan", "activePositions": 0},
                "QQQ": {"mode": "post_trade_relaxed", "action": None, "activePositions": 0}},
            "liveEquity": {
                "SPY": {"fetchedAt": "2026-08-28T12:00:00-04:00"},
                "QQQ": {"fetchedAt": "2026-08-28T11:50:00-04:00"}}}

        result = health.check_live_equity_quotes(runtime, now=now)

        self.assertTrue(result["healthy"], result)
        self.assertEqual([], result["failures"])
        self.assertIn("required=SPY", result["detail"])

    def test_reconcile_preserves_runtime_position_quote_fields(self):
        import v7_scalp_paper as engine

        now = datetime(2026, 8, 28, 12, 0, tzinfo=ET)
        last_quote = {
            "bid": 1.25,
            "ask": 1.28,
            "at": "2026-08-28T11:59:59-04:00",
            "quoteTimestamp": "2026-08-28T11:59:58-04:00"}
        state = {
            "activePositions": [{
                "positionId": "paper-open",
                "lastQuote": last_quote,
                "peakBid": 1.31,
                "pendingExitReason": "signal_reversal",
                "exitConfirmCount": 1}]}
        events = json.dumps({
            "event": "PAPER_ENTRY_REAL_ASK",
            "positionId": "paper-open",
            "timestamp": "2026-08-28T11:55:00-04:00",
            "entryAsk": 1.20,
            "occSymbol": "SPY260829C00600000"})

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "trades.jsonl"
            ledger_path.write_text(events + "\n")
            with patch.object(engine, "log_path", return_value=ledger_path):
                reconciled = engine.reconcile_state_from_ledger(state, now=now, symbol="SPY")
        position = reconciled["activePositions"][0]

        self.assertEqual(last_quote, position["lastQuote"])
        self.assertEqual(1.31, position["peakBid"])
        self.assertEqual("signal_reversal", position["pendingExitReason"])
        self.assertEqual(1, position["exitConfirmCount"])

    def test_provider_event_timestamp_beats_fetch_time(self):
        now = datetime(2026, 8, 28, 12, 0, tzinfo=ET)
        runtime = {
            "mode": "active_scan",
            "liveEquity": {
                **{
                    symbol: {
                        "fetchedAt": "2026-08-28T12:00:00-04:00",
                        "venueTimestamp": "2026-08-28T11:58:00-04:00"}
                    for symbol in ("SPY", "QQQ")
                }}}
        result = health.check_live_equity_quotes(runtime, now=now)
        self.assertFalse(result["healthy"], result)
        self.assertEqual(
            {"equity_quote_stale:SPY", "equity_quote_stale:QQQ"},
            set(result["failures"]),
        )

    def test_active_scan_uses_each_symbols_session_window(self):
        now = datetime(2026, 8, 28, 15, 50, tzinfo=ET)
        runtime = {
            "mode": "active_scan",
            "liveEquity": {
                "SPY": {"venueTimestamp": "2026-08-28T15:30:00-04:00"},
                "QQQ": {"venueTimestamp": "2026-08-28T15:30:00-04:00"}}}
        result = health.check_live_equity_quotes(runtime, now=now)
        self.assertTrue(result["healthy"], result)
        self.assertEqual([], result["failures"])

    def test_sunday_session_does_not_require_equity_premarket_targets(self):
        now = datetime(2026, 8, 30, 20, 33, tzinfo=ET)
        result = health.check_premarket_targets({"mode": "active_scan"}, now=now)
        self.assertTrue(result["healthy"], result)
        self.assertEqual([], result["failures"])
        self.assertIn("symbol session windows", result["detail"])

    def test_sunday_provider_gap_is_off_hours(self):
        """Do not require a fresh equity quote when Robinhood has no Sunday feed."""
        now = datetime(2026, 8, 30, 20, 33, tzinfo=ET)
        import v7_scalp_paper as engine

        self.assertFalse(engine.xsp_market_open(now))
        result = health.check_live_equity_quotes({"mode": "active_scan"}, now=now)
        self.assertTrue(result["healthy"], result)
        self.assertEqual([], result["failures"])
        self.assertIn("no symbol quote window open", result["detail"])

    def test_labor_day_market_holiday_does_not_require_live_quotes(self):
        """A full-day exchange holiday must not turn prior-close quotes into failures."""
        now = datetime(2026, 9, 7, 12, 0, tzinfo=ET)
        import v7_scalp_paper as engine

        self.assertTrue(engine.is_market_holiday(now.date()))
        self.assertFalse(engine.xsp_market_open(now))
        self.assertFalse(engine.entry_window_open(now, "SPY"))
        runtime = {
            "mode": "active_scan",
            "liveEquity": {
                symbol: {"venueTimestamp": "2026-09-04T15:59:59-04:00"}
                for symbol in engine.SYMBOLS
            }}
        result = health.check_live_equity_quotes(runtime, now=now)
        self.assertTrue(result["healthy"], result)
        self.assertEqual([], result["failures"])
        self.assertIn("no symbol quote window open", result["detail"])

    def test_labor_day_daemon_stays_off_hours(self):
        """The paper daemon must not enter active scan mode on a market holiday."""
        now = datetime(2026, 9, 7, 12, 0, tzinfo=ET)
        import v7_scalp_live_daemon as live_daemon
        import v7_scalp_paper as engine

        decision = live_daemon.choose_mode(engine.default_state("SPY"), now, symbol="SPY")
        self.assertEqual("off_hours", decision.mode)
        self.assertEqual("market_holiday", decision.reason)

    def test_active_scan_rejects_stale_spy_quote(self):
        now = datetime(2026, 8, 31, 12, 0, tzinfo=ET)
        runtime = {
            "mode": "active_scan",
            "liveEquity": {
                "SPY": {"venueTimestamp": "2026-08-28T20:14:23-04:00"},
                "QQQ": {"venueTimestamp": "2026-08-31T11:59:45-04:00"}}}
        result = health.check_live_equity_quotes(runtime, now=now)
        self.assertFalse(result["healthy"], result)
        self.assertEqual(["equity_quote_stale:SPY"], result["failures"])

    def test_weekday_active_scan_still_requires_equity_premarket_targets(self):
        now = datetime(2026, 8, 28, 10, 0, tzinfo=ET)
        import v7_preflight_check as preflight
        with patch.object(
            preflight,
            "check_premarket_snapshot",
            return_value={"status": preflight.WARN, "detail": "test stale"},
        ):
            result = health.check_premarket_targets({"mode": "active_scan"}, now=now)
        self.assertFalse(result["healthy"], result)
        self.assertEqual(
            {
                "premarket_target_unhealthy:SPY",
                "premarket_target_unhealthy:QQQ"},
            set(result["failures"]),
        )

    def test_pause_writes_fail_closed_kill_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            kill_path = Path(tmp) / "v7_kill_switch.json"
            with patch.object(health, "KILL_SWITCH_PATH", kill_path):
                health.pause_trading(["heartbeat_stale"], now=datetime(2026, 8, 28, 12, 0, tzinfo=ET))

            payload = json.loads(kill_path.read_text())
            self.assertTrue(payload["active"])
            self.assertIn("heartbeat_stale", payload["reason"])

    def test_resume_requires_healthy_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            kill_path = Path(tmp) / "v7_kill_switch.json"
            kill_path.write_text(json.dumps({"active": True, "reason": "health_gate:test"}))
            with patch.object(health, "KILL_SWITCH_PATH", kill_path):
                self.assertFalse(health.resume_trading({"healthy": False, "failures": ["bad_data"]}))
                self.assertTrue(kill_path.exists())
                self.assertTrue(health.resume_trading({"healthy": True, "failures": []}))
            self.assertFalse(kill_path.exists())

    def test_inactive_manual_control_is_not_pause_or_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            kill_path = Path(tmp) / "v7_kill_switch.json"
            kill_path.write_text(json.dumps({
                "active": False,
                "reason": "explicit control installed; paper evidence collection enabled"}))
            report = {
                "checkedAt": "2026-09-01T13:54:14-04:00",
                "healthy": True,
                "failures": [],
                "checks": [],
                "runtime": {"liveOrdersEnabled": False}}
            with patch.object(health, "KILL_SWITCH_PATH", kill_path), patch.object(
                health, "evaluate_health", return_value=report
            ), patch.object(health, "_record_report") as record:
                result = health.main([])
                self.assertTrue(health.resume_trading(report))

            self.assertEqual(0, result)
            self.assertTrue(kill_path.exists())
            record.assert_called_once()


    def test_health_owned_pause_does_not_turn_clean_gate_into_cron_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            kill_path = Path(tmp) / "v7_kill_switch.json"
            kill_path.write_text(
                json.dumps(
                    {
                        "active": True,
                        "reason": "health_gate:equity_quote_stale:SPY"}
                )
            )
            report = {
                "checkedAt": "2026-09-01T13:54:14-04:00",
                "healthy": True,
                "failures": [],
                "checks": [],
                "runtime": {"liveOrdersEnabled": False}}
            with patch.object(health, "KILL_SWITCH_PATH", kill_path), patch.object(
                health, "evaluate_health", return_value=report
            ), patch.object(health, "_record_report") as record:
                result = health.main([])

            self.assertEqual(0, result)
            self.assertTrue(kill_path.exists())
            record.assert_called_once()
            recorded = record.call_args.args[0]
            self.assertTrue(recorded["healthy"])
            self.assertEqual([], recorded["failures"])


if __name__ == "__main__":
    unittest.main()
