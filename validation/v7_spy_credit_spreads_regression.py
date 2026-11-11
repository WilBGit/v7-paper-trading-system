#!/usr/bin/env python3
"""Paper-only regression tests for the SPY V7 credit-spread module."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import v7_spy_credit_spreads_paper as spreads  # noqa: E402
import v7_rh_quote_bridge as quote_bridge  # noqa: E402


ET = spreads.ET


def leg(option_type: str, strike: float, bid: float, ask: float, *, delta: float | None = None) -> dict:
    return {
        "symbol": "SPY",
        "optionType": option_type,
        "strike": strike,
        "bid": bid,
        "ask": ask,
        "mark": (bid + ask) / 2,
        "instrumentId": f"{option_type}-{strike}",
        "occSymbol": f"SPY260909{'C' if option_type == 'call' else 'P'}{int(strike * 1000):08d}",
        "expiry": "2026-09-09",
        "updatedAt": "2026-09-09T14:00:00+00:00",
        "quoteSource": "Robinhood MCP",
        "delta": delta,
    }


def neutral_setup() -> dict:
    return {
        "direction": None,
        "rawDirection": None,
        "bullScore": 5,
        "bearScore": 5,
        "qualificationBullScore": 5,
        "qualificationBearScore": 5,
        "close": 650.0,
        "ema9": 650.02,
        "ema21": 649.98,
        "vwap": 650.0,
        "vwapUpper": 651.0,
        "vwapLower": 649.0,
        "rsi": 50.0,
        "macdHist": 0.01,
        "momentumPct": 0.01,
        "relativeVolume": 1.05,
        "supertrend": "neutral",
        "premarketBreakout": False,
        "openingContinuation": False,
        "turtle": {"longBreakout": False, "shortBreakout": False, "n": 1.0},
        "bullChecks": {},
        "bearChecks": {},
    }


class SpreadRiskTests(unittest.TestCase):
    def test_requested_pacific_entry_window_and_hard_close(self):
        # September is PDT: 09:30 ET = 06:30 PDT and 16:00 ET = 13:00 PDT.
        before = datetime(2026, 9, 10, 9, 29, 59, tzinfo=ET)
        start = datetime(2026, 9, 10, 9, 30, tzinfo=ET)
        last_entry = datetime(2026, 9, 10, 15, 59, 59, tzinfo=ET)
        hard_close = datetime(2026, 9, 10, 16, 0, tzinfo=ET)
        self.assertFalse(spreads.entry_window_open(before))
        self.assertTrue(spreads.entry_window_open(start))
        self.assertTrue(spreads.entry_window_open(last_entry))
        self.assertFalse(spreads.entry_window_open(hard_close))
        self.assertFalse(spreads.hard_close_due(last_entry))
        self.assertTrue(spreads.hard_close_due(hard_close))

    def test_quote_bridge_target_expiration_can_explicitly_select_same_day(self):
        now = datetime(2026, 9, 9, 14, 0, tzinfo=spreads.UTC)
        chains = [{
            "symbol": "SPY",
            "id": "chain-1",
            "expiration_dates": ["2026-09-09", "2026-09-10"],
        }]

        with self.assertRaises(quote_bridge.BridgeError):
            quote_bridge._target_expiration(chains, now, 0, "SPY")
        self.assertEqual(
            ("chain-1", "2026-09-09"),
            quote_bridge._target_expiration(chains, now, 0, "SPY", allow_same_day=True),
        )

    def test_quote_bridge_spread_snapshot_has_four_leg_executable_quotes(self):
        now = datetime(2026, 9, 9, 14, 0, tzinfo=spreads.UTC)
        contracts = [
            ("put", 648, "put-648", 0.02, 0.05, -0.10),
            ("put", 649, "put-649", 0.35, 0.39, -0.20),
            ("call", 651, "call-651", 0.35, 0.39, 0.20),
            ("call", 652, "call-652", 0.02, 0.05, 0.10),
        ]
        instruments = [
            {
                "id": instrument_id,
                "chain_id": "chain-1",
                "chain_symbol": "SPY",
                "expiration_date": "2026-09-09",
                "type": option_type,
                "state": "active",
                "tradability": "tradable",
                "strike_price": strike,
            }
            for option_type, strike, instrument_id, *_ in contracts
        ]
        quote_by_id = {
            instrument_id: {
                "instrument_id": instrument_id,
                "bid_price": bid,
                "ask_price": ask,
                "mark_price": (bid + ask) / 2,
                "updated_at": "2026-09-09T14:00:00+00:00",
                "delta": delta,
                "volume": 100,
                "open_interest": 1000,
            }
            for _, _, instrument_id, bid, ask, delta in contracts
        }

        def caller(method, arguments):
            if method == "get_equity_quotes":
                return {"data": {"results": [{"quote": {
                    "symbol": "SPY", "bid_price": 650.0, "ask_price": 650.02,
                    "venue_bid_time": "2026-09-09T14:00:00+00:00",
                    "venue_ask_time": "2026-09-09T14:00:00+00:00",
                }}]}}
            if method == "get_option_chains":
                return {"data": {"chains": [{
                    "symbol": "SPY", "id": "chain-1", "expiration_dates": ["2026-09-09"],
                }]}}
            if method == "get_option_instruments":
                option_type = arguments.get("type")
                selected_instruments = instruments if option_type is None else [
                    item for item in instruments if item["type"] == option_type
                ]
                return {"data": {"instruments": selected_instruments}}
            if method == "get_option_quotes":
                return {"data": {"results": [{"quote": quote_by_id[item]} for item in arguments["instrument_ids"]]}}
            raise AssertionError(method)

        payload = quote_bridge.build_spread_candidates(
            symbol="SPY", dte=0, max_strike_distance=5.0,
            caller=caller, observed_at=now,
        )
        self.assertEqual("spread_candidates", payload["kind"])
        self.assertEqual("2026-09-09", payload["expirationDate"])
        self.assertTrue(payload["contracts"])
        self.assertTrue(all(item["bid"] > 0 and item["ask"] >= item["bid"] for item in payload["contracts"]))

    def test_candidate_selection_returns_risk_capped_condor_and_butterfly(self):
        contracts = [
            leg("put", 646, 0.02, 0.05, delta=-0.04),
            leg("put", 647, 0.04, 0.07, delta=-0.08),
            leg("put", 648, 0.02, 0.05, delta=-0.14),
            leg("put", 649, 0.35, 0.39, delta=-0.20),
            leg("put", 650, 0.40, 0.44, delta=-0.50),
            leg("call", 650, 0.42, 0.46, delta=0.50),
            leg("call", 651, 0.02, 0.05, delta=0.20),
            leg("call", 652, 0.35, 0.39, delta=0.14),
            leg("call", 653, 0.02, 0.05, delta=0.08),
            leg("call", 654, 0.02, 0.05, delta=0.04),
        ]
        payload = {"symbol": "SPY", "underlying": 650.0, "expectedMove": 0.5, "contracts": contracts}

        candidates = spreads.select_candidates(payload, neutral_setup(), underlying=650.0)
        self.assertIn("iron_condor", {item["structure"] for item in candidates})
        self.assertTrue(all(item["maxLoss"] <= spreads.MAX_RISK_PER_TRADE for item in candidates))

        butterfly_payload = {
            "symbol": "SPY",
            "underlying": 650.0,
            "expectedMove": 0.5,
            "contracts": [
                leg("put", 648, 0.02, 0.05, delta=-0.04),
                leg("put", 649, 0.02, 0.05, delta=-0.10),
                leg("put", 650, 0.40, 0.44, delta=-0.50),
                leg("call", 650, 0.42, 0.46, delta=0.50),
                leg("call", 651, 0.02, 0.05, delta=0.10),
                leg("call", 652, 0.02, 0.05, delta=0.04),
            ],
        }
        butterfly_candidates = spreads.select_candidates(
            butterfly_payload, neutral_setup(), underlying=650.0,
        )
        self.assertIn("iron_butterfly", {item["structure"] for item in butterfly_candidates})

    def test_candidate_selection_rejects_range_gate_failure(self):
        payload = {"symbol": "SPY", "underlying": 650.0, "expectedMove": 1.0, "contracts": []}
        setup = neutral_setup()
        setup["direction"] = "bearish"
        setup["rawDirection"] = "bearish"
        setup["bearScore"] = 8
        setup["bullScore"] = 4

        self.assertEqual([], spreads.select_candidates(payload, setup, underlying=650.0))

    def test_paper_runner_writes_isolated_entry_and_hard_close_exit(self):
        contracts = [
            leg("put", 648, 0.02, 0.05, delta=-0.10),
            leg("put", 649, 0.35, 0.39, delta=-0.20),
            leg("call", 651, 0.35, 0.39, delta=0.20),
            leg("call", 652, 0.02, 0.05, delta=0.10),
        ]
        chain_payload = {
            "kind": "spread_candidates",
            "symbol": "SPY",
            "underlying": 650.0,
            "underlyingQuote": {"symbol": "SPY", "bid_price": 650.0, "ask_price": 650.02},
            "expectedMove": 0.5,
            "timestamp": "2026-09-09T14:00:00+00:00",
            "contracts": contracts,
        }
        close_legs = {
            "long_put": leg("put", 648, 0.02, 0.04),
            "short_put": leg("put", 649, 0.10, 0.12),
            "short_call": leg("call", 651, 0.12, 0.14),
            "long_call": leg("call", 652, 0.02, 0.04),
        }

        def fake_bridge(*args):
            if args[0] == "--spread-candidates":
                return chain_payload
            if args[0] == "--spread-position":
                return {"kind": "spread_position_quotes", "timestamp": "2026-09-09T20:01:00+00:00", "legs": close_legs}
            if args[0] == "--equity":
                return {"kind": "equity_quote", "quote": {"bid_price": 650.0, "ask_price": 650.02}}
            raise AssertionError(args)

        setup_result = {"ok": True, "setup": neutral_setup(), "regime": spreads.range_regime(neutral_setup())}
        with tempfile.TemporaryDirectory() as directory, patch.object(spreads, "entry_block_reason", return_value=None), patch.object(spreads, "build_v7423_setup", return_value=setup_result):
            state_file = Path(directory) / "state.json"
            ledger_file = Path(directory) / "ledger.jsonl"
            entered = spreads.scan_once(
                now=datetime(2026, 9, 9, 10, 0, tzinfo=ET),
                state_file=state_file, ledger_file=ledger_file, bridge_fn=fake_bridge,
            )
            self.assertEqual("paper_entry", entered["action"])
            self.assertEqual("iron_condor", entered["position"]["structure"])

            exited = spreads.monitor_once(
                now=datetime(2026, 9, 9, 16, 1, tzinfo=ET),
                state_file=state_file, ledger_file=ledger_file, bridge_fn=fake_bridge,
            )
            self.assertEqual("paper_exit", exited["action"])
            self.assertEqual("hard_close", exited["reason"])
            events = spreads.read_events(ledger_file)
            self.assertEqual(["SPREAD_PAPER_ENTRY", "SPREAD_PAPER_EXIT"], [event["event"] for event in events])
            self.assertEqual(1, spreads.promotion_summary(events)["closedTrades"])

    def test_iron_condor_uses_executable_bid_ask_and_caps_risk(self):
        legs = {
            "long_put": leg("put", 648, 0.02, 0.05, delta=-0.05),
            "short_put": leg("put", 649, 0.35, 0.39, delta=-0.20),
            "short_call": leg("call", 651, 0.35, 0.39, delta=0.20),
            "long_call": leg("call", 652, 0.02, 0.05, delta=0.05),
        }

        position = spreads.build_position(
            "iron_condor", legs, underlying=650.0, setup=neutral_setup(),
            entered_at=datetime(2026, 9, 9, 10, 0, tzinfo=ET),
        )

        self.assertIsNotNone(position)
        self.assertAlmostEqual(0.60, position["entryCredit"], places=6)
        self.assertAlmostEqual(40.0, position["maxLoss"], places=6)
        self.assertAlmostEqual(60.0, position["maxProfit"], places=6)
        self.assertLessEqual(position["maxLoss"], spreads.MAX_RISK_PER_TRADE)

    def test_iron_butterfly_requires_symmetric_wings_and_records_body(self):
        legs = {
            "long_put": leg("put", 649, 0.02, 0.05, delta=-0.10),
            "short_put": leg("put", 650, 0.40, 0.44, delta=-0.50),
            "short_call": leg("call", 650, 0.42, 0.46, delta=0.50),
            "long_call": leg("call", 651, 0.02, 0.05, delta=0.10),
        }

        position = spreads.build_position(
            "iron_butterfly", legs, underlying=650.0, setup=neutral_setup(),
            entered_at=datetime(2026, 9, 9, 10, 0, tzinfo=ET),
        )

        self.assertIsNotNone(position)
        self.assertEqual(650.0, position["bodyStrike"])
        self.assertEqual(1.0, position["putWingWidth"])
        self.assertEqual(1.0, position["callWingWidth"])

    def test_range_regime_uses_v7_indicators_and_rejects_directional_alignment(self):
        self.assertTrue(spreads.range_regime(neutral_setup())["eligible"])

        directional = neutral_setup()
        directional.update({
            "bullScore": 8,
            "bearScore": 4,
            "direction": "bullish",
            "rawDirection": "bullish",
            "ema9": 651.0,
            "ema21": 649.0,
            "supertrend": "up",
            "macdHist": 0.80,
            "momentumPct": 0.35,
            "relativeVolume": 2.2,
            "rsi": 68.0,
            "turtle": {"longBreakout": True, "shortBreakout": False, "n": 1.0},
        })
        decision = spreads.range_regime(directional)
        self.assertFalse(decision["eligible"])
        self.assertTrue(decision["vetoes"])

    def test_profit_target_uses_executable_close_debit(self):
        entry_legs = {
            "long_put": leg("put", 648, 0.02, 0.05),
            "short_put": leg("put", 649, 0.35, 0.39),
            "short_call": leg("call", 651, 0.35, 0.39),
            "long_call": leg("call", 652, 0.02, 0.05),
        }
        position = spreads.build_position(
            "iron_condor", entry_legs, underlying=650.0, setup=neutral_setup(),
            entered_at=datetime(2026, 9, 9, 10, 0, tzinfo=ET),
        )
        self.assertIsNotNone(position)

        close_legs = {
            "long_put": leg("put", 648, 0.06, 0.08),
            "short_put": leg("put", 649, 0.10, 0.12),
            "short_call": leg("call", 651, 0.12, 0.14),
            "long_call": leg("call", 652, 0.06, 0.08),
        }
        reason = spreads.exit_reason(
            position, close_legs, underlying=650.0,
            now=datetime(2026, 9, 9, 10, 30, tzinfo=ET),
            v7_setup=neutral_setup(),
        )
        self.assertEqual("profit_target", reason)

    def test_directional_v7_reversal_exits_before_expiration(self):
        entry_legs = {
            "long_put": leg("put", 648, 0.02, 0.05),
            "short_put": leg("put", 649, 0.35, 0.39),
            "short_call": leg("call", 651, 0.35, 0.39),
            "long_call": leg("call", 652, 0.02, 0.05),
        }
        position = spreads.build_position(
            "iron_condor", entry_legs, underlying=650.0, setup=neutral_setup(),
            entered_at=datetime(2026, 9, 9, 10, 0, tzinfo=ET),
        )
        directional = neutral_setup()
        directional.update({
            "direction": "bullish", "rawDirection": "bullish",
            "bullScore": 8, "bearScore": 4,
        })
        reason = spreads.exit_reason(
            position, entry_legs, underlying=650.2,
            now=datetime(2026, 9, 9, 10, 20, tzinfo=ET),
            v7_setup=directional,
        )
        self.assertEqual("v7_directional_reversal", reason)

    def test_hard_close_always_exits(self):
        legs = {
            "long_put": leg("put", 648, 0.02, 0.05),
            "short_put": leg("put", 649, 0.35, 0.39),
            "short_call": leg("call", 651, 0.35, 0.39),
            "long_call": leg("call", 652, 0.02, 0.05),
        }
        position = spreads.build_position(
            "iron_condor", legs, underlying=650.0, setup=neutral_setup(),
            entered_at=datetime(2026, 9, 9, 10, 0, tzinfo=ET),
        )
        reason = spreads.exit_reason(
            position, legs, underlying=650.0,
            now=datetime(2026, 9, 9, 16, 1, tzinfo=ET),
            v7_setup=neutral_setup(),
        )
        self.assertEqual("hard_close", reason)

    def test_iron_butterfly_exit_uses_breakeven_not_short_strike(self):
        """A butterfly's short_put == short_call, so the condor breach check
        would always fire.  Verify the engine uses break-even bounds instead
        and never returns the old buggy underlying_short_strike_breach."""
        entry_legs = {
            "long_put": leg("put", 649, 0.02, 0.05, delta=-0.10),
            "short_put": leg("put", 650, 0.40, 0.44, delta=-0.50),
            "short_call": leg("call", 650, 0.42, 0.46, delta=0.50),
            "long_call": leg("call", 651, 0.02, 0.05, delta=0.10),
        }
        position = spreads.build_position(
            "iron_butterfly", entry_legs, underlying=650.0, setup=neutral_setup(),
            entered_at=datetime(2026, 9, 9, 10, 0, tzinfo=ET),
        )
        self.assertIsNotNone(position)
        # Use moderate close legs so P&L is between stop_loss and profit_target.
        close_legs = {
            "long_put": leg("put", 649, 0.01, 0.03),
            "short_put": leg("put", 650, 0.22, 0.26),
            "short_call": leg("call", 650, 0.22, 0.26),
            "long_call": leg("call", 651, 0.01, 0.03),
        }
        # At body strike: must NOT return the old buggy short-strike breach.
        # (Other exits like profit_target may fire, but the bug-specific
        # underlying_short_strike_breach must never appear for a butterfly.)
        reason_at_body = spreads.exit_reason(
            position, close_legs, underlying=650.0,
            now=datetime(2026, 9, 9, 10, 15, tzinfo=ET),
            v7_setup=neutral_setup(),
        )
        self.assertNotEqual("underlying_short_strike_breach", reason_at_body)

        # Far beyond break-even: must return the new breakeven breach, not
        # the old short-strike breach.
        credit = float(position["entryCredit"])
        break_lower = 650.0 - credit
        reason_breach = spreads.exit_reason(
            position, close_legs, underlying=break_lower - 2.0,
            now=datetime(2026, 9, 9, 10, 15, tzinfo=ET),
            v7_setup=neutral_setup(),
        )
        self.assertEqual("underlying_breakeven_breach", reason_breach)

    def test_scan_rejects_entry_too_close_to_hard_close(self):
        """Entries within MIN_TIME_TO_CLOSE_MINUTES of hard close are rejected."""
        contracts = [
            leg("put", 648, 0.02, 0.05, delta=-0.10),
            leg("put", 649, 0.35, 0.39, delta=-0.20),
            leg("call", 651, 0.35, 0.39, delta=0.20),
            leg("call", 652, 0.02, 0.05, delta=0.10),
        ]
        chain_payload = {
            "kind": "spread_candidates", "symbol": "SPY",
            "underlying": 650.0, "expectedMove": 0.5,
            "underlyingQuote": {"symbol": "SPY", "bid_price": 650.0, "ask_price": 650.02},
            "timestamp": "2026-09-09T19:45:00+00:00",
            "contracts": contracts,
        }

        def fake_bridge(*args):
            if args[0] == "--spread-candidates":
                return chain_payload
            if args[0] == "--equity":
                return {"kind": "equity_quote", "quote": {"bid_price": 650.0, "ask_price": 650.02}}
            raise AssertionError(args)

        setup_result = {"ok": True, "setup": neutral_setup(),
                        "regime": spreads.range_regime(neutral_setup())}
        # 15:45 ET — 15 minutes before 16:00 hard close — should skip.
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(spreads, "entry_block_reason", return_value=None), \
             patch.object(spreads, "build_v7423_setup", return_value=setup_result):
            state_file = Path(directory) / "state.json"
            ledger_file = Path(directory) / "ledger.jsonl"
            late = spreads.scan_once(
                now=datetime(2026, 9, 9, 15, 45, tzinfo=ET),
                state_file=state_file, ledger_file=ledger_file, bridge_fn=fake_bridge,
            )
            self.assertEqual("skip", late["action"])
            self.assertEqual("insufficient_time_to_close", late["reason"])
            # 10:00 ET — well before hard close — should NOT skip for this reason.
            state_file2 = Path(directory) / "state2.json"
            ledger_file2 = Path(directory) / "ledger2.jsonl"
            midday = spreads.scan_once(
                now=datetime(2026, 9, 9, 10, 0, tzinfo=ET),
                state_file=state_file2, ledger_file=ledger_file2, bridge_fn=fake_bridge,
            )
            self.assertNotEqual("insufficient_time_to_close", midday.get("reason"))

    def test_promotion_requires_ten_exact_closed_trades_and_positive_pnl(self):
        events = []
        for index in range(10):
            pid = f"spread-{index}"
            events.append({"event": "SPREAD_PAPER_ENTRY", "positionId": pid, "strategyId": spreads.STRATEGY_ID})
            events.append({
                "event": "SPREAD_PAPER_EXIT", "positionId": pid,
                "strategyId": spreads.STRATEGY_ID, "pnl": 1.0,
            })
        summary = spreads.promotion_summary(events)
        self.assertEqual(10, summary["closedTrades"])
        self.assertTrue(summary["gateMet"])
        self.assertEqual(0, summary["remainingTrades"])

    def test_promotion_does_not_count_mixed_strategy_events(self):
        events = []
        for index in range(10):
            pid = f"mixed-{index}"
            events.extend([
                {"event": "SPREAD_PAPER_ENTRY", "positionId": pid, "strategyId": "v7423-evidence-gate-early-reversal"},
                {"event": "SPREAD_PAPER_EXIT", "positionId": pid, "strategyId": "v7423-evidence-gate-early-reversal", "pnl": 100.0},
            ])
        summary = spreads.promotion_summary(events)
        self.assertEqual(0, summary["closedTrades"])
        self.assertFalse(summary["gateMet"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
