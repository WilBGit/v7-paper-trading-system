#!/usr/bin/env python3
"""Regression coverage for V7.4.23 evidence-gate early-reversal policy.

V7.4.23 keeps V7.4.20's release-owned combined evidence progress and
V7.4.21's two-bar signal-reversal exits, while removing V7.4.22's
pre-minimum-hold early-loss exit policy.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_release  # noqa: E402
import v7_live_execution_daemon as live_daemon  # noqa: E402
import v7_live_order_bridge as live_bridge  # noqa: E402
import v7_scalp_live_daemon as paper_daemon  # noqa: E402
import v7_scalp_paper as engine  # noqa: E402


class ReleaseIdentityTests(unittest.TestCase):
    def test_manifest_declares_the_v7423_combined_release(self):
        self.assertEqual("7.4.23-evidence-gate-early-reversal", engine.ENGINE_VERSION)
        self.assertEqual("v7423-evidence-gate-early-reversal", engine.STRATEGY_ID)
        self.assertEqual(
            "7.4.22-immediate-profit-early-loss",
            v7_release.PREVIOUS_ENGINE_VERSION,
        )
        self.assertEqual(
            "v7422-immediate-profit-early-loss",
            v7_release.PREVIOUS_STRATEGY_ID,
        )
        self.assertEqual(
            {
                "closed_trades": 24,
                "wins": 11,
                "losses_or_breakeven": 13,
                "net_pnl": 49.0,
            },
            {
                key: v7_release.EVIDENCE_CARRY_FORWARD[key]
                for key in (
                    "closed_trades",
                    "wins",
                    "losses_or_breakeven",
                    "net_pnl",
                )
            },
        )


class ExitPolicyTests(unittest.TestCase):
    def test_fast_loss_does_not_bypass_v7421_minimum_hold_policy(self):
        entry_time = datetime(2026, 9, 13, 10, 0, tzinfo=engine.ET)
        position = {
            "symbol": "SPY",
            "direction": "bearish",
            "optionType": "put",
            "entryAsk": 1.59,
            "entryTime": entry_time.isoformat(),
            "peakBid": 1.59,
        }

        reason = engine.price_exit_reason(
            position,
            1.47,
            now=entry_time + timedelta(minutes=1),
            peak_bid=1.59,
            entry_price=1.59,
            symbol="SPY",
        )

        self.assertIsNone(reason)

    def test_small_positive_bid_remains_target_or_reversal_driven(self):
        entry_time = datetime(2026, 9, 13, 10, 0, tzinfo=engine.ET)
        position = {
            "symbol": "SPY",
            "direction": "bearish",
            "optionType": "put",
            "entryAsk": 1.59,
            "entryTime": entry_time.isoformat(),
            "peakBid": 1.59,
        }

        reason = engine.price_exit_reason(
            position,
            1.60,
            now=entry_time + timedelta(minutes=1),
            peak_bid=1.60,
            entry_price=1.59,
            symbol="SPY",
        )

        self.assertIsNone(reason)

    def test_strategy_config_retains_v7421_reversal_and_excludes_v7422_early_loss(self):
        config = engine.strategy_config()

        self.assertEqual(
            {
                "confirmationBars": 2,
                "vwapBufferPct": 0.12,
                "momentumLookbackBars": 4,
                "basis": "completed_5m_bars",
            },
            config["signalReversal"],
        )
        self.assertNotIn("earlyLossProtection", config)


class PaperDaemonFollowupTests(unittest.TestCase):
    def test_entry_is_followed_by_same_cycle_monitor_call(self):
        with patch.object(
            paper_daemon,
            "_capture_engine_call",
            return_value=(0, {"action": "HOLD"}, "hold"),
        ) as capture:
            result = paper_daemon.monitor_after_entry(
                {"action": "PAPER_ENTRY", "position": {"positionId": "paper-1"}},
                "SPY",
            )

        self.assertEqual((0, {"action": "HOLD"}, "hold"), result)
        capture.assert_called_once_with("monitor", "SPY")


class LiveExitPolicyTests(unittest.TestCase):
    def test_live_monitor_defers_fast_loss_until_v7421_minimum_hold(self):
        now = datetime(2026, 9, 13, 10, 1, tzinfo=live_daemon.ET)
        position_id = "live-fast-loss"
        state = {
            "livePositions": {
                position_id: {
                    "positionId": position_id,
                    "symbol": "SPY",
                    "occSymbol": "SPY260914P00766000",
                    "instrumentId": "live-instrument-1",
                    "entryAsk": 1.59,
                    "fillPrice": 1.59,
                    "quantity": 1,
                    "filledQuantity": 1,
                    "entryTime": (now - timedelta(minutes=1)).isoformat(),
                    "engineVersion": "test-release",
                }
            },
            "processedExits": [],
            "retryBackoff": {},
        }
        quote = {
            "kind": "option_quote",
            "source": "Robinhood MCP",
            "timestamp": now.isoformat(),
            "symbol": "SPY",
            "occ_symbol": "SPY260914P00766000",
            "bid": 1.47,
            "ask": 1.48,
        }

        with patch.object(live_bridge, "get_option_quote_by_instrument", return_value=quote):
            result = live_daemon.monitor_live_position(
                state,
                state["livePositions"][position_id],
                {"engineVersion": "test-release"},
                now=now,
            )

        self.assertIsNone(result)
        self.assertIn(position_id, state["livePositions"])
        self.assertEqual([], state["processedExits"])


if __name__ == "__main__":
    raise SystemExit(unittest.main(verbosity=2))
