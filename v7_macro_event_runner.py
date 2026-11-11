#!/usr/bin/env python3
"""Macro-event PAPER runner: CPI/PPI releases, FOMC decisions and minutes.

Independent of the frozen V7.5.4 release: no V7 files are written, no broker
orders are possible, and paper quote-side accounting is not fill proof.
Schedule evidence comes only from official BLS/Federal Reserve pages. Any
verification, quote, or validation failure fails closed with no entry.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Mapping
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import v7_macro_event_paper as paper
import v7_macro_event_signals as signals

ET = ZoneInfo('America/New_York')
PT = ZoneInfo('America/Los_Angeles')
UTC = timezone.utc

STATE_DIR = Path.home() / '.hermes/state/v7_macro_paper'
REGISTRY_PATH = STATE_DIR / 'v7_macro_events.json'
JOURNAL_PATH = STATE_DIR / 'v7_macro_paper.sqlite'
RUNTIME_PATH = STATE_DIR / 'v7_macro_runtime.json'
EVENT_LOG_PATH = STATE_DIR / 'v7_macro_paper_events.jsonl'
LOCK_PATH = STATE_DIR / 'v7_macro_runner.lock'
KILL_SWITCH_PATH = Path.home() / '.hermes/state/v7_kill_switch.json'

BLS_ICS_URLS = ('https://blsmon1.bls.gov/schedule/news_release/bls.ics',
                'https://www.bls.gov/schedule/news_release/bls.ics')
FOMC_CALENDAR_URL = 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'
BLS_HOSTS = ('blsmon1.bls.gov', 'www.bls.gov')

HORIZON_DAYS = 45
REGISTRY_MAX_AGE_DAYS = 14
PRE_ARM_SECONDS = 120
SAMPLE_BUDGET_SECONDS = 480  # cap one sampling segment at 8 minutes
PROFIT_TARGET_PCT = 50.0
STOP_LOSS_PCT = 30.0
MAX_HOLD_MINUTES = 10
HARD_CLOSE_ET = dtime(15, 55)
ENTRY_DTE = 1
MIN_DEBIT = 5.0    # per contract, matching the bridge's dollar convention
MAX_DEBIT = 40.0   # per contract: one contract <= $40 premium at risk
MAX_OTM_PCT = 3.0  # $40/contract forces deep OTM on 1DTE; pick nearest affordable

PaperJournal = paper.PaperJournal  # test seam

_MONTHS = {'January': 1, 'February': 2, 'March': 3, 'April': 4, 'May': 5,
           'June': 6, 'July': 7, 'August': 8, 'September': 9, 'October': 10,
           'November': 11, 'December': 12}
_BLS_SUMMARY_FAMILY = {'Consumer Price Index': 'CPI', 'Producer Price Index': 'PPI'}


def _parse_aware(value) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError('required_timestamp_missing')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timezone_required')
    return parsed


def parse_bls_ics(text: str, now: datetime) -> list[dict]:
    events = []
    for block in re.findall(r'BEGIN:VEVENT.*?END:VEVENT', text, re.S):
        summary = re.search(r'SUMMARY:(Consumer Price Index|Producer Price Index)', block)
        start = re.search(r'DTSTART;TZID=US-Eastern:(\d{8}T\d{6})', block)
        if not (summary and start):
            continue
        family = _BLS_SUMMARY_FAMILY[summary.group(1)]
        release = datetime.strptime(start.group(1), '%Y%m%dT%H%M%S').replace(tzinfo=ET)
        if not now <= release <= now + timedelta(days=HORIZON_DAYS):
            continue
        events.append({
            'event_id': f"{family}:{release.date()}",
            'family': family,
            'release_at': release.isoformat(),
            'source_url': f"https://www.bls.gov/news.release/{'cpi' if family == 'CPI' else 'ppi'}.nr0.htm",
        })
    return events


def parse_fomc_calendar(html: str, now: datetime) -> list[dict]:
    events = []
    sections = re.finditer(
        r'<h4[^>]*>\s*(?:<a[^>]*id="\d+"[^>]*>)?(\d{4}) FOMC Meetings(?:</a>)?</h4>(.*?)(?=<h4[^>]*>\s*<a[^>]*id="\d+"|\Z)',
        html, re.S)
    for section in sections:
        year = int(section.group(1))
        body = section.group(2)
        blocks = re.split(r'(?=<div class="[^"]*row fomc-meeting)', body)
        for block in blocks:
            month = re.search(r'fomc-meeting__month[^>]*><strong>([A-Z][a-z]+)</strong>', block)
            days = re.search(r'fomc-meeting__date[^>]*>(\d{1,2})-(\d{1,2})<', block)
            if not (month and days and month.group(1) in _MONTHS):
                continue
            try:
                decision_day = date(year, _MONTHS[month.group(1)], int(days.group(2)))
            except ValueError:
                continue
            decision_at = datetime(decision_day.year, decision_day.month,
                                   decision_day.day, 14, 0, tzinfo=ET)
            if now <= decision_at <= now + timedelta(days=HORIZON_DAYS):
                events.append({
                    'event_id': f'FOMC_DECISION:{decision_day}',
                    'family': 'FOMC_DECISION',
                    'release_at': decision_at.isoformat(),
                    'source_url': f'https://www.federalreserve.gov/newsevents/pressreleases/monetary{decision_day:%Y%m%d}a.htm',
                })
            released = re.search(r'Released ([A-Z][a-z]+) (\d{1,2}), (\d{4})', block)
            if not released or released.group(1) not in _MONTHS:
                continue
            try:
                minutes_day = date(int(released.group(3)), _MONTHS[released.group(1)],
                                   int(released.group(2)))
            except ValueError:
                continue
            minutes_at = datetime(minutes_day.year, minutes_day.month, minutes_day.day,
                                   14, 0, tzinfo=ET)
            if not now <= minutes_at <= now + timedelta(days=HORIZON_DAYS):
                continue
            meeting = re.search(r'fomcminutes(\d{8})', block)
            events.append({
                'event_id': f'FOMC_MINUTES:{minutes_day}',
                'family': 'FOMC_MINUTES',
                'release_at': minutes_at.isoformat(),
                'source_url': (f'https://www.federalreserve.gov/monetarypolicy/fomcminutes{meeting.group(1)}.htm'
                               if meeting else FOMC_CALENDAR_URL),
            })
    return events


def build_registry(ics_text: str, fomc_html: str, now: datetime) -> dict:
    events = parse_bls_ics(ics_text, now) + parse_fomc_calendar(fomc_html, now)
    if not events:
        raise ValueError('schedule_parse_failed:no_official_events')
    events.sort(key=lambda e: e['release_at'])
    return {
        'generatedAt': now.isoformat(),
        'sources': {
            'bls_ics': {'sha256': hashlib.sha256(ics_text.encode()).hexdigest()},
            'fomc_calendar': {'sha256': hashlib.sha256(fomc_html.encode()).hexdigest()},
        },
        'verified': {},
        'events': events,
    }


def registry_ok(registry, now: datetime) -> tuple[bool, str]:
    if not isinstance(registry, Mapping):
        return False, 'registry_missing'
    generated = registry.get('generatedAt')
    events = registry.get('events')
    try:
        generated_at = _parse_aware(generated)
    except ValueError:
        return False, 'registry_invalid'
    if not isinstance(events, list) or not events:
        return False, 'registry_invalid'
    if now - generated_at > timedelta(days=REGISTRY_MAX_AGE_DAYS):
        return False, 'registry_stale'
    return True, 'fresh'


def next_event(registry, now: datetime):
    for event in registry.get('events', []):
        try:
            if phase(event, now) != 'done':
                return event
        except ValueError:
            continue
    return None


def phase(event: Mapping, now: datetime) -> str:
    # Registry events are unverified until the runner confirms the official
    # release; window math only needs release_at, so stamp a placeholder for
    # validation. Entry still requires a real verified_at via _ensure_verified.
    if not event.get('verified_at'):
        event = {**event, 'verified_at': datetime.now(UTC).isoformat()}
    start, end = signals.event_window(event)
    if now >= end:
        return 'done'
    if now >= start:
        return 'active'
    if (start - now).total_seconds() <= PRE_ARM_SECONDS:
        return 'pre_arm'
    return 'idle'


def exit_decision(entry_ask: float, bid: float, entry_at: datetime, now: datetime) -> dict:
    if now.astimezone(ET).time() >= HARD_CLOSE_ET:
        return {'action': 'EXIT', 'reason': 'hard_close'}
    pct = (bid - entry_ask) / entry_ask * 100.0 if entry_ask > 0 else -999.0
    if pct >= PROFIT_TARGET_PCT:
        return {'action': 'EXIT', 'reason': 'profit_target'}
    if pct <= -STOP_LOSS_PCT:
        return {'action': 'EXIT', 'reason': 'stop_loss'}
    if (now - entry_at) >= timedelta(minutes=MAX_HOLD_MINUTES):
        return {'action': 'EXIT', 'reason': 'time_stop'}
    return {'action': 'HOLD', 'reason': 'hold'}


def kill_active(path=KILL_SWITCH_PATH) -> bool:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return False  # paper semantics: missing/malformed does not halt paper
    return isinstance(data, dict) and data.get('active') is True


def sample_until_decision(event, getter, until, *, sleep=time.sleep, now_fn=None):
    now_fn = now_fn or (lambda: datetime.now(UTC))
    histories = {'SPY': [], 'QQQ': []}
    while True:
        now = now_fn()
        for symbol in ('SPY', 'QQQ'):
            try:
                tick = getter(symbol, now)
            except Exception:
                tick = None
            if isinstance(tick, Mapping):
                histories[symbol].append(dict(tick))
        try:
            decision = signals.evaluate_signal(event, histories, now)
        except ValueError as exc:
            return {'action': 'SKIP', 'reason': f'event_invalid:{exc}',
                    'event_id': event.get('event_id')}
        if decision.get('action') != 'WAIT':
            return decision
        if now >= until:
            return {'action': 'SKIP', 'reason': 'window_expired',
                    'event_id': event.get('event_id')}
        sleep(1)


def try_enter(journal, event, signal, candidate_getter, now: datetime):
    try:
        candidate = candidate_getter(signal.get('symbol'), signal.get('direction'))
        if not isinstance(candidate, Mapping):
            return None
        contract = dict(candidate)
        contract['option_type'] = {'bullish': 'call', 'bearish': 'put'}[signal.get('direction')]
        position = journal.enter(event, contract, signal, now, release_id=event['event_id'])
        _log_event('paper_entry', {'event_id': event['event_id'],
                                   'position_id': position['position_id'],
                                   'symbol': contract['symbol'], 'ask': contract['ask']})
        return position
    except (ValueError, TypeError, KeyError) as exc:
        _log_event('entry_skipped', {'event_id': event.get('event_id'), 'error': str(exc)})
        return None


def alert(message: str, *, command=None) -> bool:
    argv = list(command) if command else ['hermes', 'send', '--to', 'telegram', '--quiet', message]
    try:
        result = subprocess.run(argv, input=message, timeout=15, check=False)
        return result.returncode == 0
    except Exception:
        return False


class WorkerClient:
    """Persistent JSON-line client over the read-only quote worker."""

    def __init__(self):
        self.proc = None

    def _ensure(self):
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name('v7_rh_quote_worker.py'))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        return self.proc

    def call(self, request: dict) -> dict:
        proc = self._ensure()
        proc.stdin.write(json.dumps(request) + '\n')
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError('worker_no_response')
        response = json.loads(line)
        if not response.get('ok'):
            raise RuntimeError(response.get('error', 'worker_error'))
        return response['data']

    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()


_WORKER: WorkerClient | None = None


def _worker() -> WorkerClient:
    global _WORKER
    if _WORKER is None:
        _WORKER = WorkerClient()
    return _WORKER


def _finite(value, field):
    number = float(value)
    if number != number or number in (float('inf'), float('-inf')):
        raise ValueError(f'nonfinite:{field}')
    return number


def worker_equity_getter(symbol, now):
    payload = _worker().call({'op': 'equity', 'symbol': symbol})
    quote = payload.get('quote') or {}
    bid = _finite(quote.get('bid_price'), 'bid')
    ask = _finite(quote.get('ask_price'), 'ask')
    if bid <= 0 or ask < bid:
        return None
    return {'timestamp': payload.get('timestamp'), 'bid': bid, 'ask': ask}


def worker_candidate_getter(symbol, direction):
    option_type = {'bullish': 'call', 'bearish': 'put'}[direction]
    equity = _worker().call({'op': 'equity', 'symbol': symbol})
    quote = equity.get('quote') or {}
    underlying = (_finite(quote.get('bid_price'), 'bid') + _finite(quote.get('ask_price'), 'ask')) / 2.0
    request = {'op': 'scalp_candidate', 'symbol': symbol,
               'option_type': option_type, 'underlying': underlying,
               'dte': ENTRY_DTE, 'min_debit': MIN_DEBIT,
               'max_debit': MAX_DEBIT, 'max_otm_pct': MAX_OTM_PCT}
    last_error = None
    for attempt in range(3):  # bounded retry: chain listings flap intermittently
        try:
            payload = _worker().call(request)
            candidate = payload.get(option_type)
            return dict(candidate) if isinstance(candidate, Mapping) else None
        except (RuntimeError, ValueError) as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f'candidate_fetch_failed:{last_error}')


def worker_option_quote(contract: Mapping):
    return _worker().call({'op': 'option', 'occ_symbol': contract['occ_symbol'],
                           'instrument_id': contract['instrument_id']})


def fetch_url(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Macintosh)'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode('utf-8', errors='replace')


def _bls_urls(event: Mapping) -> list[str]:
    path = event['source_url'].split('bls.gov', 1)[1]
    return [f'https://{host}{path}' for host in BLS_HOSTS]


def _confirm_release(event: Mapping, text: str) -> bool:
    family = event.get('family')
    if family in ('CPI', 'PPI'):
        marker = 'Consumer Price Index' if family == 'CPI' else 'Producer Price Index'
        return marker in text and '8:30 a.m.' in text
    if family in ('FOMC_DECISION', 'FOMC_MINUTES'):
        return 'For release at 2:00 p.m.' in text or 'For release at 2:00 p.m' in text
    return False


def _verify_event(event: Mapping, fetch) -> str | None:
    urls = _bls_urls(event) if event.get('family') in ('CPI', 'PPI') else [event['source_url']]
    for url in urls:
        try:
            text = fetch(url)
        except Exception:
            continue
        if text and _confirm_release(event, text):
            return datetime.now(UTC).isoformat()
    return None


def _load_registry(state_dir: Path):
    path = state_dir / REGISTRY_PATH.name
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(text)
    tmp.replace(path)


def _write_runtime(state_dir: Path, payload: dict):
    payload = {'strategy': 'v7-macro-event-paper', 'mode': 'paper_only',
               'liveOrdersEnabled': False, **payload}
    _atomic_write(state_dir / RUNTIME_PATH.name, json.dumps(payload, indent=2))


def _log_event(kind: str, payload: dict):
    try:
        path = EVENT_LOG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as handle:
            handle.write(json.dumps({'kind': kind, **payload,
                                     'timestamp': datetime.now(UTC).isoformat()}) + '\n')
    except OSError:
        pass


def _ensure_verified(state_dir: Path, event: Mapping, fetch, now: datetime):
    registry = _load_registry(state_dir) or {}
    verified = registry.get('verified') or {}
    verified_at = verified.get(event['event_id'])
    if verified_at:
        return {**event, 'verified_at': verified_at}
    stamp = _verify_event(event, fetch)
    if not stamp:
        _log_event('verification_failed', {'event_id': event['event_id']})
        return None
    registry['verified'] = {**verified, event['event_id']: stamp}
    _atomic_write(state_dir / REGISTRY_PATH.name, json.dumps(registry, indent=2))
    _log_event('event_verified', {'event_id': event['event_id'], 'verified_at': stamp})
    return {**event, 'verified_at': stamp}


def _monitor_segment(journal, position, *, sleep=time.sleep, now_fn=None,
                     option_quote=worker_option_quote, budget_seconds=55):
    now_fn = now_fn or (lambda: datetime.now(PT))
    deadline = now_fn() + timedelta(seconds=budget_seconds)
    contract = position['contract']
    entry_ask = float(position['entry_ask'])
    entry_at = _parse_aware(position['entry_at'])
    while now_fn() < deadline:
        now = now_fn()
        try:
            payload = option_quote(contract)
            quote = _option_fields(payload)
            bid = _finite(quote.get('bid'), 'bid')
        except Exception:
            bid = None
        if bid is not None:
            decision = exit_decision(entry_ask, bid, entry_at, now)
            if decision['action'] == 'EXIT':
                closed = journal.exit({**contract, 'bid': bid, 'ask': max(bid, bid + .01),
                                       'updated_at': now.astimezone(UTC).isoformat()},
                                      now, reason=decision['reason'])
                alert(f"MACRO PAPER EXIT {contract['symbol']} {contract['occ_symbol']} "
                      f"P&L=${closed['pnl']:.2f} reason={decision['reason']} (paper only)")
                return {'status': 'exited', 'reason': decision['reason'],
                        'pnl': closed['pnl']}
        sleep(1)
    return {'status': 'holding', 'position_id': position['position_id']}


def _option_fields(payload: Mapping) -> dict:
    if not isinstance(payload, Mapping):
        raise ValueError('option_payload_invalid')
    return {'bid': payload.get('bid'), 'ask': payload.get('ask')}


def run_once(state_dir=STATE_DIR, *, kill_path=KILL_SWITCH_PATH, fetch=fetch_url,
             equity_getter=None, candidate_getter=None, option_quote=None,
             sleep=time.sleep, now_fn=None):
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    now_fn = now_fn or (lambda: datetime.now(PT))
    now = now_fn()
    if kill_active(kill_path):
        _write_runtime(state_dir, {'status': 'kill_switch_active', 'updatedAt': now.isoformat()})
        return {'status': 'kill_switch_active'}
    journal = PaperJournal(state_dir / JOURNAL_PATH.name)
    try:
        position = journal.position()
        if position:
            result = _monitor_segment(journal, position, sleep=sleep, now_fn=now_fn,
                                      option_quote=option_quote or worker_option_quote)
            _write_runtime(state_dir, {**result, 'updatedAt': now_fn().isoformat()})
            return result
        registry = _load_registry(state_dir)
        ok, reason = registry_ok(registry, now)
        if not ok:
            _write_runtime(state_dir, {'status': reason, 'updatedAt': now.isoformat()})
            return {'status': reason}
        event = next_event(registry, now)
        if event is None:
            _write_runtime(state_dir, {'status': 'idle', 'updatedAt': now.isoformat()})
            return {'status': 'idle'}
        current_phase = phase(event, now)
        if current_phase == 'idle':
            result = {'status': 'idle_until_prearm', 'event': event['event_id'],
                      'release_at': event['release_at']}
            _write_runtime(state_dir, {**result, 'updatedAt': now.isoformat()})
            return result
        event = _ensure_verified(state_dir, event, fetch, now)
        if event is None:
            _write_runtime(state_dir, {'status': 'event_unverified',
                                       'event': registry['events'][0]['event_id'],
                                       'updatedAt': now.isoformat()})
            return {'status': 'event_unverified'}
        start, end = signals.event_window(event)
        if current_phase == 'pre_arm':
            while now_fn() < start:
                sleep(min(5, max(0.0, (start - now_fn()).total_seconds())))
        getter = equity_getter or worker_equity_getter
        until = min(end, start + timedelta(seconds=SAMPLE_BUDGET_SECONDS))
        decision = sample_until_decision(event, getter, until, sleep=sleep, now_fn=now_fn)
        if decision.get('action') == 'SIGNAL':
            position = try_enter(journal, event, decision,
                                 candidate_getter or worker_candidate_getter, now_fn())
            if position:
                alert(f"MACRO PAPER ENTRY {position['contract']['symbol']} "
                      f"{position['contract']['occ_symbol']} @{position['entry_ask']} "
                      f"direction={decision['direction']} event={event['event_id']} (paper only)")
                result = {'status': 'entered', 'position_id': position['position_id'],
                          'event': event['event_id']}
            else:
                result = {'status': 'entry_skipped', 'event': event['event_id']}
        else:
            result = {'status': 'no_entry', 'reason': decision.get('reason'),
                      'event': event['event_id']}
        _write_runtime(state_dir, {**result, 'updatedAt': now_fn().isoformat()})
        return result
    finally:
        journal.close()


def cmd_refresh(state_dir=STATE_DIR):
    now = datetime.now(PT)
    ics = None
    errors = []
    for url in BLS_ICS_URLS:
        try:
            ics = fetch_url(url)
            break
        except Exception as exc:
            errors.append(f'{url}:{exc}')
    if ics is None:
        print(json.dumps({'status': 'failed', 'reason': 'bls_ics_unreachable',
                          'errors': errors}))
        return 1
    try:
        html = fetch_url(FOMC_CALENDAR_URL)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'reason': 'fomc_calendar_unreachable',
                          'error': str(exc)}))
        return 1
    registry = build_registry(ics, html, now)
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(state_dir / REGISTRY_PATH.name, json.dumps(registry, indent=2))
    print(json.dumps({'status': 'ok', 'generatedAt': registry['generatedAt'],
                      'events': [e['event_id'] for e in registry['events']]}))
    return 0


def cmd_verify(event_id: str, state_dir=STATE_DIR):
    registry = _load_registry(Path(state_dir))
    ok, reason = registry_ok(registry, datetime.now(PT))
    if not ok:
        print(json.dumps({'status': 'failed', 'reason': reason}))
        return 1
    event = next((e for e in registry['events'] if e['event_id'] == event_id), None)
    if event is None:
        print(json.dumps({'status': 'failed', 'reason': 'event_not_found'}))
        return 1
    stamp = _verify_event(event, fetch_url)
    if stamp:
        registry['verified'] = {**(registry.get('verified') or {}), event_id: stamp}
        _atomic_write(Path(state_dir) / REGISTRY_PATH.name, json.dumps(registry, indent=2))
        print(json.dumps({'status': 'verified', 'event_id': event_id,
                          'verified_at': stamp}))
        return 0
    print(json.dumps({'status': 'failed', 'reason': 'release_not_confirmed'}))
    return 1


def cmd_report(state_dir=STATE_DIR):
    state_dir = Path(state_dir)
    journal = PaperJournal(state_dir / JOURNAL_PATH.name)
    try:
        summary = journal.summary()
    finally:
        journal.close()
    registry = _load_registry(state_dir)
    ok, reason = registry_ok(registry, datetime.now(PT))
    now = datetime.now(PT)
    upcoming = []
    if ok:
        upcoming = [{'event_id': e['event_id'], 'release_at': e['release_at'],
                     'verified': bool((registry.get('verified') or {}).get(e['event_id']))}
                    for e in registry['events'] if phase(e, now) != 'done']
    print(json.dumps({'summary': summary, 'registry': {'ok': ok, 'reason': reason},
                      'upcoming': upcoming,
                      'kill_switch_active': kill_active(),
                      'mode': 'paper_only', 'liveOrdersEnabled': False,
                      'now_pt': now.isoformat()}, indent=2))
    return 0


def cmd_run(state_dir=STATE_DIR):
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = open(state_dir / LOCK_PATH.name, 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # another segment is already running
    try:
        result = run_once(state_dir)
        print(json.dumps(result))
        return 0
    finally:
        _worker().close() if _WORKER is not None else None
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def cmd_smoke(state_dir=STATE_DIR):
    """Read-only quote-path proof: SPY/QQQ equity + one QQQ candidate. No writes."""
    state_dir = Path(state_dir)
    worker = WorkerClient()
    try:
        equity = {}
        for symbol in ('SPY', 'QQQ'):
            payload = worker.call({'op': 'equity', 'symbol': symbol})
            quote = payload.get('quote') or {}
            equity[symbol] = {'timestamp': payload.get('timestamp'),
                              'bid': quote.get('bid_price'), 'ask': quote.get('ask_price')}
        qqq = worker.call({'op': 'scalp_candidate', 'symbol': 'QQQ', 'option_type': 'call',
                           'underlying': (_finite(equity['QQQ']['bid'], 'bid')
                                          + _finite(equity['QQQ']['ask'], 'ask')) / 2.0,
                           'dte': ENTRY_DTE, 'min_debit': MIN_DEBIT, 'max_debit': MAX_DEBIT,
                           'max_otm_pct': MAX_OTM_PCT})
        candidate = qqq.get('call')
        print(json.dumps({'equity': equity,
                          'candidate': ({k: candidate.get(k) for k in
                                         ('symbol', 'occ_symbol', 'strike', 'expiry',
                                          'bid', 'ask', 'volume', 'open_interest')}
                                         if candidate else None),
                          'mode': 'read_only'}, indent=2))
        return 0
    finally:
        worker.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', default=str(STATE_DIR))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('refresh')
    sub.add_parser('run')
    sub.add_parser('report')
    sub.add_parser('smoke')
    verify = sub.add_parser('verify')
    verify.add_argument('event_id')
    args = parser.parse_args()
    state = Path(args.state_dir)
    if args.command == 'refresh':
        return cmd_refresh(state)
    if args.command == 'run':
        return cmd_run(state)
    if args.command == 'report':
        return cmd_report(state)
    if args.command == 'smoke':
        return cmd_smoke(state)
    if args.command == 'verify':
        return cmd_verify(args.event_id, state)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
