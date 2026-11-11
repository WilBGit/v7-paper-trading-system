#!/usr/bin/env python3
"""Regression tests for the deterministic paper macro-event signal module."""
from __future__ import annotations

import math
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest import TestCase, main
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import v7_macro_event_signals as signals


ET = ZoneInfo("America/New_York")


def _cpi_event(event_id: str = "cpi-2026-10-15") -> dict[str, str]:
    return {
        "event_id": event_id,
        "family": "CPI",
        "release_at": "2026-10-15T08:30:00-04:00",
        "source_url": "https://www.bls.gov/news.release/cpi.nr0.htm",
        "verified_at": "2026-10-15T08:00:00-04:00",
    }


def _quote(at: datetime, mid: float) -> dict[str, object]:
    return {
        "timestamp": at.isoformat(),
        "bid": mid - 0.01,
        "ask": mid + 0.01,
    }


def _confirmed_histories(direction: str = "bullish") -> dict[str, list[dict[str, object]]]:
    start = datetime(2026, 10, 15, 9, 30, tzinfo=ET)
    breakout = 100.10 if direction == "bullish" else 99.90
    rows = []
    for offset in (0, 5, 15, 30, 45, 55, 60, 65, 70):
        mid = 100.0 if offset <= 60 else breakout
        rows.append(_quote(start + timedelta(seconds=offset), mid))
    return {"SPY": list(rows), "QQQ": list(rows)}


def _range_only_histories() -> dict[str, list[dict[str, object]]]:
    start = datetime(2026, 10, 15, 9, 30, tzinfo=ET)
    rows = [_quote(start + timedelta(seconds=offset), 100.0) for offset in (0, 5, 15, 30, 45, 55, 60)]
    return {"SPY": list(rows), "QQQ": list(rows)}


def _event_for(
    family: str,
    release_at: str,
    source_url: str,
    event_id: str = "event-1",
) -> dict[str, str]:
    return {
        "event_id": event_id,
        "family": family,
        "release_at": release_at,
        "source_url": source_url,
        "verified_at": release_at,
    }


class MacroEventSignalRegression(TestCase):
    def test_cpi_premarket_release_starts_at_regular_open(self):
        event = {
            "event_id": "cpi-2026-10-15",
            "family": "CPI",
            "release_at": "2026-10-15T08:30:00-04:00",
            "source_url": "https://www.bls.gov/news.release/cpi.nr0.htm",
            "verified_at": "2026-10-15T08:00:00-04:00",
        }

        start, end = signals.event_window(event)

        self.assertEqual(start, datetime(2026, 10, 15, 9, 30, tzinfo=ET))
        self.assertEqual(end, datetime(2026, 10, 15, 10, 0, tzinfo=ET))

    def test_confirmed_agreement_emits_deterministic_qqq_bullish_signal(self):
        now = datetime(2026, 10, 15, 9, 31, 11, tzinfo=ET)

        result = signals.evaluate_signal(
            _cpi_event(),
            _confirmed_histories("bullish"),
            now,
        )

        self.assertEqual(result["action"], "SIGNAL")
        self.assertEqual(result["reason"], "confirmed_breakout")
        self.assertEqual(result["event_id"], "cpi-2026-10-15")
        self.assertEqual(result["direction"], "bullish")
        self.assertEqual(result["symbol"], "QQQ")
        self.assertIsInstance(result["evidence"], dict)
        self.assertEqual(
            result["evidence"]["latest_venue_timestamps"]["SPY"],
            "2026-10-15T09:31:10-04:00",
        )
        self.assertEqual(
            result["evidence"]["latest_venue_timestamps"]["QQQ"],
            "2026-10-15T09:31:10-04:00",
        )
        self.assertEqual(
            result["evidence"]["reference_prices"]["SPY"]["range_low"],
            100.0,
        )
        self.assertEqual(
            result["evidence"]["reference_prices"]["SPY"]["range_high"],
            100.0,
        )

    def test_confirmed_agreement_emits_bearish_direction(self):
        result = signals.evaluate_signal(
            _cpi_event("cpi-bear-2026-10-15"),
            _confirmed_histories("bearish"),
            datetime(2026, 10, 15, 9, 31, 11, tzinfo=ET),
        )

        self.assertEqual(result["action"], "SIGNAL")
        self.assertEqual(result["direction"], "bearish")
        self.assertEqual(result["symbol"], "QQQ")

    def test_fomc_deadlines_are_family_specific(self):
        decision = _event_for(
            "FOMC_DECISION",
            "2026-10-15T14:00:00-04:00",
            "https://www.federalreserve.gov/newsevents/pressreleases/monetary20261015a.htm",
        )
        minutes = _event_for(
            "FOMC_MINUTES",
            "2026-10-15T14:00:00-04:00",
            "https://www.federalreserve.gov/monetarypolicy/fomcminutes20261015.htm",
        )

        self.assertEqual(
            signals.event_window(decision),
            (
                datetime(2026, 10, 15, 14, 0, tzinfo=ET),
                datetime(2026, 10, 15, 14, 25, tzinfo=ET),
            ),
        )
        self.assertEqual(
            signals.event_window(minutes)[1],
            datetime(2026, 10, 15, 14, 30, tzinfo=ET),
        )

    def test_fomc_deadlines_are_family_specific(self):
        decision = _event_for(
            "FOMC_DECISION",
            "2026-10-15T14:00:00-04:00",
            "https://www.federalreserve.gov/newsevents/pressreleases/monetary20261015a.htm",
        )
        minutes = _event_for(
            "FOMC_MINUTES",
            "2026-10-15T14:00:00-04:00",
            "https://www.federalreserve.gov/monetarypolicy/fomcminutes20261015.htm",
        )
        late_release = _event_for(
            "PPI",
            "2026-10-15T15:20:00-04:00",
            "https://www.bls.gov/news.release/ppi.nr0.htm",
        )

        self.assertEqual(
            signals.event_window(decision),
            (
                datetime(2026, 10, 15, 14, 0, tzinfo=ET),
                datetime(2026, 10, 15, 14, 25, tzinfo=ET),
            ),
        )
        self.assertEqual(
            signals.event_window(minutes)[1],
            datetime(2026, 10, 15, 14, 30, tzinfo=ET),
        )
        self.assertEqual(
            signals.event_window(late_release)[1],
            datetime(2026, 10, 15, 15, 30, tzinfo=ET),
        )

    def test_malformed_event_and_now_inputs_fail_closed(self):
        naive_release = _cpi_event()
        naive_release["release_at"] = "2026-10-15T08:30:00"
        with self.assertRaises(ValueError):
            signals.event_window(naive_release)

        naive_verified = _cpi_event()
        naive_verified["verified_at"] = "2026-10-15T08:00:00"
        with self.assertRaises(ValueError):
            signals.event_window(naive_verified)

        result = signals.evaluate_signal(
            _cpi_event(), {}, datetime(2026, 10, 15, 9, 31, 11)
        )
        self.assertEqual(result["action"], "SKIP")
        self.assertEqual(result["reason"], "now_invalid")

    def test_premarket_and_pre_release_are_waits(self):
        self.assertEqual(
            signals.evaluate_signal(
                _cpi_event(),
                {},
                datetime(2026, 10, 15, 9, 0, tzinfo=ET),
            )["action"],
            "WAIT",
        )
        future_event = _event_for(
            "CPI",
            "2026-10-15T09:31:00-04:00",
            "https://www.bls.gov/news.release/cpi.nr0.htm",
        )
        result = signals.evaluate_signal(
            future_event,
            {},
            datetime(2026, 10, 15, 9, 30, 30, tzinfo=ET),
        )
        self.assertEqual(result["action"], "WAIT")
        self.assertEqual(result["reason"], "pre_release")

    def test_event_unknown_unofficial_and_late_are_skips(self):
        unknown = dict(_cpi_event())
        unknown["family"] = "GDP"
        self.assertEqual(
            signals.evaluate_signal(
                unknown,
                {},
                datetime(2026, 10, 15, 9, 31, 11, tzinfo=ET),
            )["reason"],
            "family_unknown",
        )
        unofficial = dict(_cpi_event())
        unofficial["source_url"] = "https://example.com/cpi"
        self.assertEqual(
            signals.evaluate_signal(
                unofficial,
                {},
                datetime(2026, 10, 15, 9, 31, 11, tzinfo=ET),
            )["reason"],
            "source_url_unofficial",
        )
        late = signals.evaluate_signal(
            _cpi_event(),
            {},
            datetime(2026, 10, 15, 10, 1, tzinfo=ET),
        )
        self.assertEqual(late["action"], "SKIP")
        self.assertEqual(late["reason"], "entry_window_closed")

    def test_weekend_and_full_day_holiday_fail_closed(self):
        weekend = _event_for(
            "CPI",
            "2026-10-17T08:30:00-04:00",
            "https://www.bls.gov/news.release/cpi.nr0.htm",
        )
        holiday = _event_for(
            "CPI",
            "2026-09-07T08:30:00-04:00",
            "https://www.bls.gov/news.release/cpi.nr0.htm",
        )
        with self.assertRaises(ValueError):
            signals.event_window(weekend)
        with self.assertRaises(ValueError):
            signals.event_window(holiday)
        self.assertEqual(
            signals.evaluate_signal(
                holiday,
                {},
                datetime(2026, 9, 7, 9, 31, tzinfo=ET),
            )["reason"],
            "market_closed",
        )

    def test_calendar_lookup_failure_fails_closed(self):
        original = signals.is_market_holiday

        def unavailable(_day):
            raise RuntimeError("calendar unavailable")

        signals.is_market_holiday = unavailable
        try:
            with self.assertRaisesRegex(ValueError, "market_calendar_unavailable"):
                signals.event_window(_cpi_event())
        finally:
            signals.is_market_holiday = original

    def test_initial_range_requires_coverage_and_small_gaps(self):
        histories = _confirmed_histories()
        histories["SPY"].pop(2)  # leaves a 25-second gap from +5 to +30
        result = signals.evaluate_signal(
            _cpi_event(),
            histories,
            datetime(2026, 10, 15, 9, 31, 11, tzinfo=ET),
        )
        self.assertEqual(result["action"], "SKIP")
        self.assertEqual(result["reason"], "initial_range_gap")

        missing_early = _confirmed_histories()
        missing_early["SPY"].pop(1)
        missing_early["SPY"].pop(0)
        missing_early["QQQ"].pop(1)
        missing_early["QQQ"].pop(0)
        result = signals.evaluate_signal(
            _cpi_event(),
            missing_early,
            datetime(2026, 10, 15, 9, 31, 11, tzinfo=ET),
        )
        self.assertEqual(result["action"], "SKIP")
        self.assertEqual(result["reason"], "initial_range_incomplete")

    def test_provider_and_book_validation_fails_closed(self):
        cases: list[tuple[str, object]] = []

        stale = _range_only_histories()
        cases.append(("quote_stale", stale))

        future = _confirmed_histories()
        future["SPY"][-1]["timestamp"] = "2026-10-15T09:31:12-04:00"
        future["QQQ"][-1]["timestamp"] = "2026-10-15T09:31:12-04:00"
        cases.append(("quote_future", future))

        nonfinite = _confirmed_histories()
        nonfinite["SPY"][-1]["bid"] = math.nan
        cases.append(("bid_invalid", nonfinite))

        crossed = _confirmed_histories()
        crossed["SPY"][-1]["bid"] = 100.11
        crossed["SPY"][-1]["ask"] = 100.09
        cases.append(("quote_crossed", crossed))

        wide = _confirmed_histories()
        wide["SPY"][-1]["bid"] = 100.0
        wide["SPY"][-1]["ask"] = 100.2
        cases.append(("quote_spread_wide", wide))

        now = datetime(2026, 10, 15, 9, 31, 10, tzinfo=ET)
        for expected_reason, histories in cases:
            with self.subTest(expected_reason=expected_reason):
                result = signals.evaluate_signal(_cpi_event(), histories, now)
                self.assertEqual(result["action"], "SKIP")
                self.assertEqual(result["reason"], expected_reason)

    def test_duplicate_out_of_order_and_unsupported_symbols_are_rejected(self):
        duplicate = _confirmed_histories()
        duplicate["SPY"].insert(2, deepcopy(duplicate["SPY"][2]))
        duplicate_result = signals.evaluate_signal(
            _cpi_event(), duplicate, datetime(2026, 10, 15, 9, 31, 11, tzinfo=ET)
        )
        self.assertEqual(duplicate_result["reason"], "history_not_strictly_increasing")

        out_of_order = _confirmed_histories()
        out_of_order["SPY"][2], out_of_order["SPY"][3] = (
            out_of_order["SPY"][3],
            out_of_order["SPY"][2],
        )
        out_of_order_result = signals.evaluate_signal(
            _cpi_event(), out_of_order, datetime(2026, 10, 15, 9, 31, 11, tzinfo=ET)
        )
        self.assertEqual(out_of_order_result["reason"], "history_not_strictly_increasing")

        unsupported = _confirmed_histories()
        unsupported["IWM"] = list(unsupported["SPY"])
        unsupported_result = signals.evaluate_signal(
            _cpi_event(), unsupported, datetime(2026, 10, 15, 9, 31, 11, tzinfo=ET)
        )
        self.assertEqual(unsupported_result["reason"], "symbols_invalid")

    def test_index_option_symbols_are_rejected_too(self):
        unsupported = _confirmed_histories()
        unsupported["XSP"] = list(unsupported["SPY"])
        unsupported_result = signals.evaluate_signal(
            _cpi_event(), unsupported, datetime(2026, 10, 15, 9, 31, 11, tzinfo=ET)
        )
        self.assertEqual(unsupported_result["reason"], "symbols_invalid")

    def test_evaluation_waits_until_range_plus_delay_elapses(self):
        start = datetime(2026, 10, 15, 9, 30, tzinfo=ET)
        histories = _confirmed_histories()
        result = signals.evaluate_signal(_cpi_event(), histories, start + timedelta(seconds=64))
        self.assertEqual(result["action"], "WAIT")
        self.assertEqual(result["reason"], "range_not_ready")

        result = signals.evaluate_signal(
            _cpi_event(), histories, start + timedelta(seconds=65)
        )
        self.assertNotEqual(result["reason"], "range_not_ready")

    def test_first_spike_waits_for_confirmation(self):
        start = datetime(2026, 10, 15, 9, 30, tzinfo=ET)
        rows = [
            _quote(start + timedelta(seconds=offset), 100.0 if offset <= 60 else 100.10)
            for offset in (0, 5, 15, 30, 45, 55, 60, 65)
        ]
        result = signals.evaluate_signal(
            _cpi_event(), {"SPY": list(rows), "QQQ": list(rows)},
            start + timedelta(seconds=66),
        )
        self.assertEqual(result["action"], "WAIT")
        self.assertEqual(result["reason"], "breakout_not_confirmed")
        self.assertNotIn("direction", result)

    def test_cross_symbol_disagreement_and_chasing_skip(self):
        opposite = {
            "SPY": _confirmed_histories("bullish")["SPY"],
            "QQQ": _confirmed_histories("bearish")["QQQ"],
        }
        result = signals.evaluate_signal(
            _cpi_event(), opposite, datetime(2026, 10, 15, 9, 31, 11, tzinfo=ET)
        )
        self.assertEqual(result["action"], "SKIP")
        self.assertEqual(result["reason"], "cross_symbol_disagreement")

        chasing = _confirmed_histories("bullish")
        for symbol in signals.SYMBOLS:
            chasing[symbol][-1]["bid"] = 100.19
            chasing[symbol][-1]["ask"] = 100.21
        result = signals.evaluate_signal(
            _cpi_event(), chasing, datetime(2026, 10, 15, 9, 31, 11, tzinfo=ET)
        )
        self.assertEqual(result["action"], "SKIP")
        self.assertEqual(result["reason"], "chasing")

    def test_dense_post_range_confirmation_is_allowed(self):
        start = datetime(2026, 10, 15, 9, 30, tzinfo=ET)
        rows = [
            _quote(start + timedelta(seconds=offset), 100.0 if offset <= 60 else 100.10)
            for offset in (0, 5, 15, 30, 45, 55, 60, 65, 66, 67, 68, 69, 70)
        ]
        result = signals.evaluate_signal(
            _cpi_event(), {"SPY": list(rows), "QQQ": list(rows)},
            start + timedelta(seconds=71),
        )
        self.assertEqual(result["action"], "SIGNAL")
        self.assertEqual(result["direction"], "bullish")

    def test_contrary_quote_breaks_continuous_confirmation(self):
        histories = _confirmed_histories("bullish")
        start = datetime(2026, 10, 15, 9, 30, tzinfo=ET)
        histories["SPY"].insert(8, _quote(start + timedelta(seconds=67), 99.90))
        result = signals.evaluate_signal(
            _cpi_event(), histories, datetime(2026, 10, 15, 9, 31, 11, tzinfo=ET)
        )
        self.assertEqual(result["action"], "WAIT")
        self.assertEqual(result["reason"], "breakout_not_confirmed")

    def test_config_is_optional_but_validated(self):
        result = signals.evaluate_signal(
            _cpi_event(), _confirmed_histories(),
            datetime(2026, 10, 15, 9, 31, 11, tzinfo=ET),
            {"unknown": 1},
        )
        self.assertEqual(result["action"], "SKIP")
        self.assertEqual(result["reason"], "config_unknown")

        result = signals.evaluate_signal(
            _cpi_event(), _confirmed_histories(),
            datetime(2026, 10, 15, 9, 31, 11, tzinfo=ET),
            {"chase_bps": 1},
        )
        self.assertEqual(result["action"], "SKIP")
        self.assertEqual(result["reason"], "config_invalid")

        result = signals.evaluate_signal(
            _cpi_event(), _confirmed_histories(),
            datetime(2026, 10, 15, 9, 31, 11, tzinfo=ET),
            {"entry_cutoff_hour": 24},
        )
        self.assertEqual(result["action"], "SKIP")
        self.assertEqual(result["reason"], "config_invalid")

    def test_extended_hours_are_not_entry_hours(self):
        result = signals.evaluate_signal(
            _cpi_event(), {}, datetime(2026, 10, 15, 16, 0, tzinfo=ET)
        )
        self.assertEqual(result["action"], "SKIP")
        self.assertEqual(result["reason"], "entry_window_closed")


if __name__ == "__main__":
    main(verbosity=2)
