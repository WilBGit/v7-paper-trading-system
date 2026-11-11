#!/usr/bin/env python3
"""Isolated macro-paper tests; fixtures are not trading evidence."""
import importlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='macro-paper-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 10, 28, 18, 2, tzinfo=timezone.utc)

    def module(self):
        try:
            return importlib.import_module('v7_macro_event_paper')
        except ModuleNotFoundError:
            self.fail('macro paper journal is not implemented')

    def contract(self):
        return {'symbol': 'QQQ', 'occ_symbol': 'QQQ261029C00600000',
                'instrument_id': 'fixture-instrument', 'option_type': 'call',
                'strike': 600, 'expiry': '2026-10-29', 'bid': .36, 'ask': .38,
                'updated_at': self.now.isoformat(), 'quote_source': 'Robinhood MCP',
                'volume': 1000, 'open_interest': 2000}

    def event(self):
        return {'event_id': 'FOMC_DECISION:2026-10-28', 'family': 'FOMC_DECISION',
                'release_at': '2026-10-28T14:00:00-04:00',
                'verified_at': '2026-10-28T18:00:01+00:00',
                'source_url': 'https://www.federalreserve.gov/newsevents/pressreleases/monetary20261028a.htm'}

    def signal(self):
        return {'action': 'SIGNAL', 'direction': 'bullish', 'symbol': 'QQQ',
                'event_id': self.event()['event_id'], 'evaluated_at': self.now.isoformat(),
                'evidence': {'fixture': True}}

    def test_quote_backed_entry_and_restart_retains_ownership(self):
        m = self.module()
        journal = m.PaperJournal(self.root / 'paper.sqlite')
        position = journal.enter(self.event(), self.contract(), self.signal(), self.now,
                                 release_id='fixture-release')
        self.assertEqual(position['entry_ask'], .38)
        self.assertEqual(position['quantity'], 1)
        self.assertEqual(position['mode'], 'paper_only')
        self.assertFalse(position['liveOrdersEnabled'])
        journal.close()
        restored = m.PaperJournal(self.root / 'paper.sqlite')
        self.addCleanup(restored.close)
        self.assertEqual(restored.position()['position_id'], position['position_id'])
        self.assertEqual(restored.summary()['entries'], 1)

    def test_entry_rejects_unexecutable_or_mismatched_option(self):
        m = self.module()
        mutations = [
            {'ask': float('nan')}, {'ask': float('inf')}, {'bid': 0},
            {'bid': .45}, {'ask': .50}, {'bid': .10}, {'instrument_id': ''},
            {'updated_at': (self.now - timedelta(seconds=6)).isoformat()},
            {'updated_at': (self.now + timedelta(seconds=2)).isoformat()},
            {'updated_at': '2026-10-28T18:02:00'},
            {'symbol': 'SPY'}, {'option_type': 'put'}, {'strike': 601},
            {'expiry': '2026-10-28'}, {'expiry': '2026-11-10'},
            {'quote_source': 'yahoo'}, {'volume': 0}, {'open_interest': float('nan')},
        ]
        for i, change in enumerate(mutations):
            with self.subTest(change=change):
                j = m.PaperJournal(self.root / f'bad-{i}.sqlite')
                self.addCleanup(j.close)
                with self.assertRaises((ValueError, TypeError)):
                    j.enter(self.event(), {**self.contract(), **change}, self.signal(), self.now,
                            release_id='fixture-release')
                self.assertEqual(j.summary()['entries'], 0)

    def test_exit_pairs_real_bid_and_does_not_recycle_daily_budget(self):
        m = self.module()
        j = m.PaperJournal(self.root / 'life.sqlite')
        self.addCleanup(j.close)
        p = j.enter(self.event(), self.contract(), self.signal(), self.now, release_id='test')
        self.assertTrue(callable(getattr(j, 'exit', None)), 'quote-side exit is not implemented')
        exit_time = self.now + timedelta(seconds=10)
        q = {**self.contract(), 'bid': .50, 'ask': .52, 'updated_at': exit_time.isoformat()}
        closed = j.exit(q, exit_time, reason='profit_target')
        self.assertIsNone(j.position())
        self.assertEqual(closed['pnl'], 12)
        self.assertEqual(j.summary()['pnl'], 12)
        with self.assertRaisesRegex(ValueError, 'event_already_used'):
            j.enter(self.event(), self.contract(), self.signal(), self.now, release_id='test')
        second = {**self.event(), 'event_id': 'FOMC_MINUTES:2026-10-28', 'family': 'FOMC_MINUTES'}
        second_signal = {**self.signal(), 'event_id': second['event_id']}
        with self.assertRaisesRegex(ValueError, 'daily_premium_budget'):
            j.enter(second, self.contract(), second_signal, self.now, release_id='test')


if __name__ == '__main__':
    unittest.main()
