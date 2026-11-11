#!/usr/bin/env python3
"""Durable regressions for V7 premarket/reporting data lineage.

The filename avoids Hermes' ephemeral ``test_*`` cleanup rule. Run with:
    python3 tests/premarket_regression_suite.py -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import alpha_floor_daily_fill as alpha_fill

MODULE_PATH = SCRIPTS / "v73_morning_premarket_brief.py"
if not MODULE_PATH.exists():
    raise RuntimeError(f"Missing active premarket module: {MODULE_PATH}")

import importlib.util

spec = importlib.util.spec_from_file_location("premarket_under_test", MODULE_PATH)
assert spec and spec.loader
premarket = importlib.util.module_from_spec(spec)
spec.loader.exec_module(premarket)


class PremarketDataLineageRegressions(unittest.TestCase):
    def test_config_line_uses_active_state_not_retired_v62(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "v7_state.json"
            state_path.write_text(json.dumps({
                "version": "7.3.2-paper-scalp",
                "mode": "dry_run",
                "symbol": "SPY",
            }))
            with (
                patch.object(premarket, "V7_STATE", state_path, create=True),
                patch.object(premarket, "DATA", root, create=True),
            ):
                line = premarket.config_line()
        self.assertIn(premarket.engine.ENGINE_VERSION, line)
        self.assertNotIn("7.3.2-paper-scalp", line)
        self.assertIn("SPY", line)
        self.assertNotIn("V6.2", line)

    def test_trade_ledger_reads_current_events_and_filters_session_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "trades.jsonl"
            events = [
                {"event": "PAPER_ENTRY_REAL_ASK", "positionId": "p1", "timestamp": "2026-08-07T10:00:00-04:00"},
                {"event": "PAPER_EXIT_REAL_BID", "positionId": "p1", "pnl": 12.0, "timestamp": "2026-08-07T10:10:00-04:00"},
                {"event": "PAPER_ENTRY_REAL_ASK", "positionId": "other", "timestamp": "2026-08-06T10:00:00-04:00"},
            ]
            ledger.write_text("".join(json.dumps(event) + "\n" for event in events))
            with patch.object(premarket, "TRADES", ledger):
                summary = premarket.trade_ledger(
                    datetime(2026, 8, 7, 6, 0, tzinfo=premarket.PT)
                )
        self.assertEqual(1, len(summary["entries"]))
        self.assertEqual(1, len(summary["exits"]))
        self.assertEqual(12.0, summary["pnl"])

    def test_recent_setup_reads_scan_attempt_result_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            attempts = Path(tmp) / "attempts.jsonl"
            attempts.write_text(json.dumps({
                "timestamp": "2026-08-07T11:55:00-07:00",
                "result": {
                    "action": "SKIP",
                    "reason": "no_qualified_signal",
                    "signal": {"rawDirection": "bullish", "score": 6},
                },
                "diagnostic": {"stage": "signal"},
            }) + "\n")
            with patch.object(premarket, "DECISIONS", attempts):
                setup = premarket.best_recent_setup()
                text = premarket.short_setup_line(setup)
        self.assertIsNotNone(setup)
        self.assertIn("no_qualified_signal", text)

    def test_alpha_fill_imports_v73_module(self):
        self.assertEqual("v73_morning_premarket_brief.py", alpha_fill.PREMARKET_MODULE.name)

    def test_premarket_wrapper_forwards_dry_run_arguments(self):
        wrapper = (SCRIPTS / "v73_morning_premarket_brief.sh").read_text()
        self.assertIn('"$@"', wrapper)

    def test_alpha_signal_text_is_derived_from_active_release(self):
        fake_module = SimpleNamespace(
            fmt_pct=lambda value: "N/A" if value is None else f"{value:+.2f}%",
            fmt_price=lambda value: "N/A" if value is None else f"${value:.2f}",
            short_setup_line=lambda value: "fixture setup",
            entry_window_pt=lambda now: "07:00–11:00 PT",
        )
        context = {
            "m": fake_module,
            "display_date": "August 10, 2026",
            "quote_map": {"SPY": {}, "QQQ": {}, "^VIX": {}},
            "futures_map": {"ES=F": {}, "NQ=F": {}, "RTY=F": {}},
            "ledger": {"entries": [], "exits": [], "pnl": 0.0},
            "v7_state": {
                "version": "9.9-paper",
                "symbol": "DIA",
                "mode": "dry_run",
                "dailyTrades": 0,
                "dailyPnl": 0.0,
                "activePositions": [],
            },
            "now": datetime(2026, 8, 10, 6, 0),
            "best": None,
            "headlines": {"Fed / macro": [], "War / geopolitics": []},
            "fomc": [],
            "read": "fixture regime",
            "movers": [],
        }
        messages = alpha_fill.channel_messages(context)
        signal_text = messages["trading-signals"]
        self.assertIn("9.9-paper", signal_text)
        self.assertIn("DIA only", signal_text)
        self.assertIn("07:00–11:00 PT", signal_text)
        self.assertNotIn("SPY/QQQ", signal_text)


if __name__ == "__main__":
    unittest.main()
