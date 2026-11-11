#!/usr/bin/env python3
"""Independent quote-backed macro PAPER journal. No order endpoints or V7 writes.

Paper quote-side accounting is not broker fills and does not prove execution,
slippage, fees or profitability. Never count this strategy in the V7 cohort.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

PT = ZoneInfo('America/Los_Angeles')
MAX_DAILY_PREMIUM = Decimal('40.00')


def aware(value):
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError('timezone_required')
    return dt


def number(value):
    if isinstance(value, bool):
        raise ValueError('boolean_price')
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('nonfinite_value')
    return result


def validate_option(c: dict, now: datetime, *, entry: bool, expected: dict | None = None):
    now = aware(now)
    if c.get('quote_source') != 'Robinhood MCP':
        raise ValueError('option_source')
    if not isinstance(c.get('instrument_id'), str) or not c['instrument_id'].strip():
        raise ValueError('missing_instrument')
    match = re.fullmatch(r'(SPY|QQQ)(\d{6})([CP])(\d{8})', str(c.get('occ_symbol', '')))
    if not match:
        raise ValueError('occ_format')
    symbol, exp, opt, strike = match.groups()
    expiry = datetime.strptime(exp, '%y%m%d').date()
    if (c.get('symbol') != symbol or c.get('option_type') != {'C': 'call', 'P': 'put'}[opt]
            or c.get('expiry') != expiry.isoformat() or number(c.get('strike')) != Decimal(strike) / 1000):
        raise ValueError('contract_identity')
    if expected:
        for k in ('symbol', 'occ_symbol', 'instrument_id', 'option_type', 'strike', 'expiry'):
            if c.get(k) != expected.get(k):
                raise ValueError('exit_identity:' + k)
    age = (now - aware(c.get('updated_at'))).total_seconds()
    if not -1 <= age <= (5 if entry else 15):
        raise ValueError('stale_or_future_option')
    bid, ask = number(c.get('bid')), number(c.get('ask'))
    if bid <= 0 or ask <= 0 or bid > ask:
        raise ValueError('unexecutable_book')
    if entry:
        if not 1 <= (expiry - now.astimezone(PT).date()).days <= 7:
            raise ValueError('entry_dte')
        if ask * 100 > MAX_DAILY_PREMIUM:
            raise ValueError('premium_cap')
        if (ask - bid) / ask > Decimal('.10'):
            raise ValueError('entry_spread')
        if number(c.get('volume')) < 100 or number(c.get('open_interest')) < 100:
            raise ValueError('entry_liquidity')
    return bid, ask


class PaperJournal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS trades (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0)')
        self.db.commit()

    def close(self):
        self.db.close()

    def position(self):
        rows = self.db.execute('SELECT payload FROM trades WHERE closed=0').fetchall()
        if len(rows) > 1:
            raise ValueError('multiple_open_positions')
        return json.loads(rows[0][0]) if rows else None

    def used(self, event_id):
        return self.db.execute('SELECT 1 FROM trades WHERE event_id=?', (event_id,)).fetchone() is not None

    def summary(self):
        trades = [(json.loads(p), closed) for p, closed in self.db.execute('SELECT payload,closed FROM trades')]
        return {'entries': len(trades), 'open': sum(not closed for _, closed in trades),
                'closed': sum(closed for _, closed in trades),
                'pnl': float(sum((number(p['pnl']) for p, closed in trades if closed), Decimal(0))),
                'mode': 'paper_only', 'liveOrdersEnabled': False}

    def exit(self, contract: dict, now: datetime, *, reason: str):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            p = self.position()
            if p is None:
                raise ValueError('no_owned_position')
            bid, _ = validate_option(contract, now, entry=False, expected=p['contract'])
            if aware(contract['updated_at']) < aware(p['entry_at']) or aware(now) < aware(p['entry_at']):
                raise ValueError('exit_predates_entry')
            p.update({'exit_at': aware(now).isoformat(), 'exit_bid': float(bid),
                      'exit_quote': contract, 'exit_reason': reason,
                      'pnl': float((bid - number(p['entry_ask'])) * 100)})
            self.db.execute('UPDATE trades SET payload=?, closed=1 WHERE event_id=? AND closed=0',
                            (json.dumps(p, allow_nan=False), p['event_id']))
        return p

    def enter(self, event: dict, contract: dict, signal: dict, now: datetime, *, release_id: str):
        validate_option(contract, now, entry=True)
        if contract['symbol'] != signal.get('symbol') or contract['option_type'] != {'bullish': 'call', 'bearish': 'put'}.get(signal.get('direction')):
            raise ValueError('signal_contract_mismatch')
        p = {'position_id': 'macro-paper-' + uuid4().hex,
             'event_id': event['event_id'], 'event': event, 'contract': contract,
             'signal': signal, 'entry_at': now.isoformat(), 'entry_ask': contract['ask'],
             'quantity': 1, 'release_id': release_id, 'mode': 'paper_only',
             'liveOrdersEnabled': False}
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.used(event['event_id']):
                raise ValueError('event_already_used')
            if self.position() is not None:
                raise ValueError('position_already_open')
            day = aware(now).astimezone(PT).date()
            committed = Decimal(0)
            for (raw,) in self.db.execute('SELECT payload FROM trades'):
                previous = json.loads(raw)
                if aware(previous['entry_at']).astimezone(PT).date() == day:
                    committed += number(previous['entry_ask']) * 100
            if committed + number(contract['ask']) * 100 > MAX_DAILY_PREMIUM:
                raise ValueError('daily_premium_budget')
            self.db.execute('INSERT INTO trades(event_id,payload) VALUES (?,?)',
                            (event['event_id'], json.dumps(p, allow_nan=False)))
        return p
