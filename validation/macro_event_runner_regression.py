#!/usr/bin/env python3
"""Macro event runner regressions. Fixtures are schedule samples, not evidence."""
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import v7_macro_event_runner as runner

ET = ZoneInfo('America/New_York')
PT = ZoneInfo('America/Los_Angeles')
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=PT)

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;TZID=US-Eastern:20261014T083000
SUMMARY:Consumer Price Index
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=US-Eastern:20261110T083000
SUMMARY:Consumer Price Index
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=US-Eastern:20261113T083000
SUMMARY:Producer Price Index
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=US-Eastern:20261110T100000
SUMMARY:Employment Situation
END:VEVENT
END:VCALENDAR
"""

FOMC = """
<div class="panel panel-default"><div class="panel-heading"><h4><a id="42828">2026 FOMC Meetings</a></h4></div>
<div class="row fomc-meeting">
<div class="fomc-meeting__month col-xs-5"><strong>September</strong></div>
<div class="fomc-meeting__date col-xs-4">15-16</div>
<a href="/newsevents/pressreleases/monetary20260916a.htm">HTML</a>
<a href="/monetarypolicy/fomcminutes20260916.htm">Minutes</a>
<br> (Released October 07, 2026)
</div>
<div class="row fomc-meeting">
<div class="fomc-meeting__month col-xs-5"><strong>October</strong></div>
<div class="fomc-meeting__date col-xs-4">27-28</div>
<a href="/newsevents/pressreleases/monetary20261028a.htm">HTML</a>
<a href="/monetarypolicy/fomcminutes20261028.htm">Minutes</a>
<br> (Released November 18, 2026)
</div>
<div class="fomc-meeting--shaded row fomc-meeting">
<div class="fomc-meeting__month col-xs-5"><strong>December</strong></div>
<div class="fomc-meeting__date col-xs-4">8-9</div>
</div>
"""


class RegistryTests(unittest.TestCase):
    def test_parses_bls_and_fomc_official_schedules(self):
        events = runner.build_registry(ICS, FOMC, NOW)['events']
        by_id = {e['event_id']: e for e in events}
        self.assertIn('CPI:2026-11-10', by_id)
        self.assertIn('PPI:2026-11-13', by_id)
        self.assertIn('FOMC_DECISION:2026-10-28', by_id)
        self.assertIn('FOMC_MINUTES:2026-11-18', by_id)
        self.assertEqual(by_id['CPI:2026-11-10']['release_at'],
                         datetime(2026, 11, 10, 8, 30, tzinfo=ET).isoformat())
        self.assertEqual(by_id['FOMC_DECISION:2026-10-28']['release_at'],
                         datetime(2026, 10, 28, 14, 0, tzinfo=ET).isoformat())
        self.assertTrue(by_id['FOMC_DECISION:2026-10-28']['source_url'].startswith(
            'https://www.federalreserve.gov/newsevents/pressreleases/monetary20261028a.htm'))
        self.assertTrue(by_id['FOMC_MINUTES:2026-11-18']['source_url'].startswith(
            'https://www.federalreserve.gov/monetarypolicy/fomcminutes20261028.htm'))
        self.assertNotIn('CPI:2026-10-14', by_id)  # past release is dropped
        self.assertNotIn('FOMC_MINUTES:2026-10-07', by_id)  # past minutes dropped
        self.assertNotIn('FOMC_DECISION:2026-12-08', by_id)  # beyond 45-day horizon

    def test_registry_fail_closed_on_empty_parse_and_staleness(self):
        with self.assertRaisesRegex(ValueError, 'schedule_parse_failed'):
            runner.build_registry('BEGIN:VCALENDAR\nEND:VCALENDAR', '', NOW)
        registry = runner.build_registry(ICS, FOMC, NOW)
        self.assertTrue(runner.registry_ok(registry, NOW)[0])
        stale_ok, reason = runner.registry_ok(registry, NOW + timedelta(days=20))
        self.assertFalse(stale_ok)
        self.assertIn('registry_stale', reason)


class PhaseTests(unittest.TestCase):
    def test_next_event_and_window_phases(self):
        registry = runner.build_registry(ICS, FOMC, NOW)
        event = runner.next_event(registry, NOW)
        self.assertEqual(event['event_id'], 'FOMC_DECISION:2026-10-28')
        release = datetime.fromisoformat(event['release_at'])
        self.assertEqual(runner.phase(event, release - timedelta(hours=3)), 'idle')
        self.assertEqual(runner.phase(event, release - timedelta(seconds=60)), 'pre_arm')
        self.assertEqual(runner.phase(event, release + timedelta(seconds=60)), 'active')
        self.assertEqual(runner.phase(event, release + timedelta(hours=2)), 'done')


class ExitTests(unittest.TestCase):
    def test_exit_policy_order_and_bounds(self):
        entered = datetime(2026, 10, 28, 11, 1, tzinfo=PT)

        def at(minutes):
            return datetime(2026, 10, 28, 11, 1 + minutes, tzinfo=PT)

        self.assertEqual(runner.exit_decision(.40, .62, entered, at(2))['action'], 'EXIT')
        self.assertEqual(runner.exit_decision(.40, .62, entered, at(2))['reason'], 'profit_target')
        self.assertEqual(runner.exit_decision(.40, .27, entered, at(2))['reason'], 'stop_loss')
        self.assertEqual(runner.exit_decision(.40, .39, entered, at(11))['reason'], 'time_stop')
        self.assertEqual(runner.exit_decision(.40, .45, entered, at(4))['action'], 'HOLD')
        hard = datetime(2026, 10, 28, 15, 56, tzinfo=ET)
        self.assertEqual(runner.exit_decision(.40, .45, entered, hard)['reason'], 'hard_close')


class KillSwitchTests(unittest.TestCase):
    def test_paper_semantics_for_kill_switch(self):
        import json
        with self.subTest(active=True):
            path = Path(self.id() + '.json')
            path.write_text(json.dumps({'active': True}))
            self.addCleanup(path.unlink)
            self.assertTrue(runner.kill_active(path))
        with self.subTest(missing=False):
            self.assertFalse(runner.kill_active(Path('/nonexistent/kill.json')))
        with self.subTest(case='explicit_false'):
            path = Path(self.id() + '-false.json')
            path.write_text(json.dumps({'active': False}))
            self.addCleanup(path.unlink)
            self.assertFalse(runner.kill_active(path))


class EntryPathTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix='macro-runner-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime.now(ZoneInfo('UTC'))

    def contract(self, **over):
        c = {'symbol': 'QQQ', 'occ_symbol': 'QQQ261029C00600000',
             'instrument_id': 'fixture-instrument', 'option_type': 'call',
             'strike': 600, 'expiry': '2026-10-29', 'bid': .36, 'ask': .38,
             'updated_at': self.now.isoformat(),
             'quote_source': 'Robinhood MCP', 'volume': 400, 'open_interest': 900}
        c.update(over)
        return c

    def test_enter_on_signal_skips_on_bad_contract_without_raising(self):
        registry = runner.build_registry(ICS, FOMC, NOW)
        event = runner.next_event(registry, NOW)
        signal = {'action': 'SIGNAL', 'direction': 'bullish', 'symbol': 'QQQ',
                  'event_id': event['event_id']}
        entry_now = datetime(2026, 10, 28, 18, 2, tzinfo=ZoneInfo('UTC'))
        journal = runner.PaperJournal(self.root / 'j.sqlite')
        entered = runner.try_enter(journal, event, signal,
                                   lambda s, d: self.contract(updated_at=entry_now.isoformat()),
                                   entry_now)
        self.assertIsNotNone(entered)
        self.assertEqual(journal.summary()['entries'], 1)
        journal2 = runner.PaperJournal(self.root / 'j2.sqlite')
        bad = runner.try_enter(journal2, event, signal,
                               lambda s, d: self.contract(ask=float('nan')),
                               datetime(2026, 10, 28, 18, 2, tzinfo=ZoneInfo('UTC')))
        self.assertIsNone(bad)
        self.assertEqual(journal2.summary()['entries'], 0)
        none = runner.try_enter(journal2, event, signal, lambda s, d: None,
                                datetime(2026, 10, 28, 18, 2, tzinfo=ZoneInfo('UTC')))
        self.assertIsNone(none)

    def test_sample_loop_delegates_to_signals_and_stops_on_signal(self):
        event = {'event_id': 'TEST:2026-10-15', 'family': 'CPI',
                 'release_at': '2026-10-15T08:30:00-04:00',
                 'verified_at': '2026-10-15T12:31:00+00:00',
                 'source_url': 'https://www.bls.gov/news.release/cpi.nr0.htm'}
        import v7_macro_event_signals as signals
        start, deadline = signals.event_window(event)
        quotes = []
        clock = {'tick': 0}

        def now_fn():
            now = start + timedelta(seconds=clock['tick'])
            clock['tick'] += 1
            return now

        def getter(symbol, now):
            step = int((now - start).total_seconds())
            base = 100.0
            if step <= 60:
                tick = {'timestamp': now.isoformat(), 'bid': base, 'ask': base}
            else:
                tick = {'timestamp': now.isoformat(), 'bid': base + .05,
                        'ask': base + .06}
            quotes.append((symbol, now))
            return tick

        decision = runner.sample_until_decision(event, getter, start + timedelta(seconds=150), sleep=lambda s: None, now_fn=now_fn)
        self.assertEqual(decision['action'], 'SIGNAL')
        self.assertEqual(decision['symbol'], 'QQQ')
        self.assertEqual({s for s, _ in quotes}, {'SPY', 'QQQ'})

    def test_alert_failure_is_not_fatal(self):
        self.assertFalse(runner.alert(['x'], command=['definitely-not-a-command-xyz']))

    def test_run_once_respects_kill_switch(self):
        import json
        path = self.root / 'kill.json'
        path.write_text(json.dumps({'active': True}))
        result = runner.run_once(self.root, kill_path=path,
                                 fetch=lambda name: (_ for _ in ()).throw(AssertionError()))
        self.assertEqual(result['status'], 'kill_switch_active')


if __name__ == '__main__':
    unittest.main()
