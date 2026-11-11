#!/usr/bin/env python3
"""
V7.1 Daily P&L Report — runs after market close, reports dry-run performance.
Posts to Discord #trade-log and #daily-report.

Schema: scanner now logs event="ENTRY" with dry_run=true (not "DRY_RUN_SIGNAL").
This script reads the updated schema and separates dry-run vs live trades.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
STATE_DIR = Path("/path/to/project/.hermes/state")
TRADES_FILE = STATE_DIR / "v7_trades.jsonl"
STATE_FILE = STATE_DIR / "v7_state.json"
DISCORD_HELPER = Path("/path/to/project/.hermes/scripts/discord-send-message.mjs")
DISCORD_TRADE_LOG = "1519239671224336465"  # #trade-log
DISCORD_DAILY_REPORT = "1519239669924106240"  # #daily-report


def discord_post(channel_id: str, content: str) -> bool:
    if not DISCORD_HELPER.exists():
        return False
    env_file = DISCORD_HELPER.parent / ".env"
    env = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    proc = subprocess.run(
        ["/usr/local/bin/node", str(DISCORD_HELPER), channel_id, content],
        cwd=str(DISCORD_HELPER.parent), text=True, capture_output=True, timeout=30, check=False,
        env={**os.environ, **env},
    )
    return proc.returncode == 0


def main() -> int:
    now = datetime.now(PT)
    today = now.strftime("%Y-%m-%d")

    # Load all trades
    trades = []
    if TRADES_FILE.exists():
        for line in TRADES_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                trades.append(json.loads(line))
            except Exception:
                pass

    # Filter today's trades — updated schema
    # ENTRY with dry_run=true = paper entry
    # EXIT with dry_run=true = paper exit
    # ENTRY without dry_run (or dry_run=false) = live entry
    # EXIT without dry_run (or dry_run=false) = live exit
    today_all = [t for t in trades if t.get("ts", "").startswith(today)]
    today_entries = [t for t in today_all if t.get("event") == "ENTRY"]
    today_exits = [t for t in today_all if t.get("event") == "EXIT"]

    dry_entries = [t for t in today_entries if t.get("dry_run", False)]
    dry_exits = [t for t in today_exits if t.get("dry_run", False)]
    live_entries = [t for t in today_entries if not t.get("dry_run", False)]
    live_exits = [t for t in today_exits if not t.get("dry_run", False)]

    # Calculate P&L from exits
    dry_pnl = sum(t.get("pnl_usd", 0) for t in dry_exits)
    live_pnl = sum(t.get("pnl_usd", 0) for t in live_exits)
    total_pnl = dry_pnl + live_pnl

    # Win/loss breakdown
    all_exits = dry_exits + live_exits
    wins = [t for t in all_exits if t.get("pnl_usd", 0) > 0]
    losses = [t for t in all_exits if t.get("pnl_usd", 0) < 0]
    breakeven = [t for t in all_exits if t.get("pnl_usd", 0) == 0]
    win_rate = len(wins) / len(all_exits) * 100 if all_exits else 0

    # Load state for cumulative tracking
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        state = {}

    mode = state.get("mode", "dry_run").upper()
    cumulative_pnl = state.get("dailyPnl", 0)

    # Build report
    report = (
        f"📊 **V7.1 Daily Report — {now.strftime('%A, %b %d')}**\n"
        f"Mode: {mode}\n\n"
        f"**Signals:** {len(dry_entries)} paper entries, {len(live_entries)} live entries\n"
        f"**Exits:** {len(dry_exits)} paper, {len(live_exits)} live\n"
        f"**Wins:** {len(wins)} | **Losses:** {len(losses)} | **Breakeven:** {len(breakeven)}\n"
        f"**Win Rate:** {win_rate:.0f}%\n"
        f"**P&L:** ${total_pnl:+.2f} (paper: ${dry_pnl:+.2f}, live: ${live_pnl:+.2f})\n"
    )

    if all_exits:
        report += "\n**Exits:**\n"
        for t in all_exits:
            sym = t.get("symbol", "?")
            direction = t.get("direction", "?").upper()
            reason = t.get("exit_reason", "?")
            pnl = t.get("pnl_usd", 0)
            pnl_pct = t.get("pnl_pct", 0)
            tag = "[paper]" if t.get("dry_run") else "[LIVE]"
            report += f"• {tag} {sym} {direction}: ${pnl:+.2f} ({pnl_pct:+.1f}%) — {reason}\n"

    if dry_entries and not all_exits:
        report += "\n**Open Paper Positions (not yet exited):**\n"
        for t in dry_entries:
            sym = t.get("symbol", "?")
            direction = t.get("direction", "?").upper()
            score = t.get("score", 0)
            strike = t.get("strike", "?")
            entry_price = t.get("entry_price", 0)
            report += f"• {sym} {direction} ${strike} score={score} @ ${entry_price} — still open\n"

    report += f"\n**Cumulative P&L:** ${cumulative_pnl:+.2f}"
    report += f"\n**State:** {len(state.get('activePositions', []))} active positions"

    print(report)

    # Post to Discord
    discord_post(DISCORD_TRADE_LOG, report)
    if all_exits or dry_entries or live_entries:
        discord_post(DISCORD_DAILY_REPORT, report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
