#!/usr/bin/env python3
"""Deterministic weekly report for the active quote-validated V7 paper scalp.

Historical totals are retained for context. Readiness is calculated only from
exact current-release entry/exit pairs carrying the full release identity.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path

import v7_scalp_paper as engine

STATE_DIR = Path('/path/to/project/.hermes/state')
SYMBOLS = tuple(engine.SYMBOLS)
EVIDENCE_GATE = engine.EVIDENCE_GATE
ACTIVE_RELEASE = {
    'engineVersion': engine.ENGINE_VERSION,
    'strategyId': engine.STRATEGY_ID,
    'releaseManifestSha256': engine.RELEASE_MANIFEST_SHA256,
    'sourceCodeSha256': engine.source_code_sha256(),
    'configSha256': engine.config_sha256(),
}


def _exact_current_release(events: list[dict]) -> tuple[list[dict], list[dict]]:
    entries: dict[str, dict] = {}
    for event in events:
        if event.get('event') != 'PAPER_ENTRY_REAL_ASK':
            continue
        position_id = event.get('positionId')
        if position_id and all(event.get(field) == value for field, value in ACTIVE_RELEASE.items()):
            entries[position_id] = event

    exits: list[dict] = []
    for event in events:
        if event.get('event') != 'PAPER_EXIT_REAL_BID':
            continue
        position_id = event.get('positionId')
        if (
            position_id in entries
            and all(event.get(field) == value for field, value in ACTIVE_RELEASE.items())
        ):
            exits.append(event)
    return list(entries.values()), exits


def _evidence_status(closed_trades: int, wins: int, net_pnl: float) -> str:
    minimum = EVIDENCE_GATE['min_closed_trades']
    minimum_wr = EVIDENCE_GATE['min_win_rate_pct']
    if closed_trades < minimum:
        return f'NOT ELIGIBLE — exact current-release sample {closed_trades}/{minimum} trades'
    win_rate = wins / closed_trades * 100 if closed_trades else 0.0
    if win_rate < minimum_wr:
        return f'NOT ELIGIBLE — exact current-release WR {win_rate:.1f}% below {minimum_wr:.1f}%'
    if EVIDENCE_GATE['require_positive_pnl'] and net_pnl <= 0:
        return 'NOT ELIGIBLE — exact current-release P&L is not positive'
    return 'REVIEW REQUIRED — exact gate met; manual Project Contributor approval required'


def report_for_symbol(symbol: str) -> dict | None:
    log_path = STATE_DIR / f'v7_scalp_trades_{symbol}.jsonl'
    if symbol == 'SPY' and not log_path.exists():
        legacy = STATE_DIR / 'v7_scalp_trades.jsonl'
        if legacy.exists():
            log_path = legacy
    if not log_path.exists():
        return None

    events = []
    for line in log_path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    entries = [e for e in events if e.get('event') == 'PAPER_ENTRY_REAL_ASK']
    exits = [e for e in events if e.get('event') == 'PAPER_EXIT_REAL_BID']
    pnls = [float(e['pnl']) for e in exits if isinstance(e.get('pnl'), (int, float))]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    reasons = Counter(e.get('reason', 'unknown') for e in exits)
    historical_versions = sorted({
        str(e.get('engineVersion')) for e in events if e.get('engineVersion')
    })
    current_entries, current_exits = _exact_current_release(events)
    current_pnls = [
        float(e['pnl']) for e in current_exits if isinstance(e.get('pnl'), (int, float))
    ]
    current_wins = [p for p in current_pnls if p > 0]
    current_net_pnl = round(sum(current_pnls), 2)

    return {
        'symbol': symbol,
        'strategy': f'V{engine.ENGINE_VERSION} active engine / {symbol} 1DTE naked paper scalp',
        'ledgerScope': 'historical context; readiness uses exact current-release pairs',
        'historicalEngineVersions': historical_versions,
        'mode': 'dry_run',
        'entries': len(entries),
        'closedTrades': len(exits),
        'openPaperTrades': len(entries) - len(exits),
        'netPnl': round(sum(pnls), 2),
        'winRatePct': round(len(wins) / len(pnls) * 100, 1) if pnls else None,
        'avgWin': round(sum(wins) / len(wins), 2) if wins else None,
        'avgLoss': round(sum(losses) / len(losses), 2) if losses else None,
        'exitReasons': dict(reasons),
        'currentRelease': {
            'engineVersion': engine.ENGINE_VERSION,
            'strategyId': engine.STRATEGY_ID,
            'entries': len(current_entries),
            'closedTrades': len(current_exits),
            'openPaperTrades': len(current_entries) - len(current_exits),
            'wins': len(current_wins),
            'winRatePct': round(len(current_wins) / len(current_pnls) * 100, 1) if current_pnls else None,
            'netPnl': current_net_pnl,
        },
        'fundingDecision': 'See combined exact-current-release evidence gate',
        'liveOrdersEnabled': False,
    }


def main():
    results = []
    for symbol in SYMBOLS:
        result = report_for_symbol(symbol)
        if result is not None:
            results.append(result)

    if not results:
        print(json.dumps({'error': 'no_trade_data_found', 'symbols_checked': SYMBOLS}, indent=2))
        return

    exact_closed = sum(r['currentRelease']['closedTrades'] for r in results)
    exact_wins = sum(r['currentRelease']['wins'] for r in results)
    exact_pnl = round(sum(r['currentRelease']['netPnl'] for r in results), 2)
    exact_wr = round(exact_wins / exact_closed * 100, 1) if exact_closed else 0.0
    combined = {
        'version': engine.ENGINE_VERSION,
        'activeEngineVersion': engine.ENGINE_VERSION,
        'ledgerScope': 'historical totals are context; evidence gate is exact current release only',
        'evidenceGate': {
            'minClosedTrades': EVIDENCE_GATE['min_closed_trades'],
            'minWinRatePct': EVIDENCE_GATE['min_win_rate_pct'],
            'requirePositivePnl': EVIDENCE_GATE['require_positive_pnl'],
            'scope': 'exact current-release entry/exit pairs with matching release, source, and config identity',
            'closedTrades': exact_closed,
            'wins': exact_wins,
            'winRatePct': exact_wr,
            'netPnl': exact_pnl,
            'status': _evidence_status(exact_closed, exact_wins, exact_pnl),
        },
        'symbols': results,
        'combinedHistoricalNetPnl': round(sum(r['netPnl'] for r in results), 2),
        'combinedHistoricalClosedTrades': sum(r['closedTrades'] for r in results),
        'liveOrdersEnabled': False,
    }
    print(json.dumps(combined, indent=2))


if __name__ == '__main__':
    main()
