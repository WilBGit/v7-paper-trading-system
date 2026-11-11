#!/usr/bin/env python3
"""Deterministic daily Alpha Trading Floor channel filler.

Posts concise, channel-specific morning content to Alpha Trading Floor using the
existing OpenClaw Discord bot helper. No LLM/model calls.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

PT_NAME = "America/Los_Angeles"
PREMARKET_MODULE = Path("/path/to/project/.hermes/scripts/v73_morning_premarket_brief.py")
V7_STATE_FILE = Path("/path/to/project/.hermes/state/v7_scalp_state.json")
HERMES_SCRIPTS = Path("/path/to/project/.hermes/scripts")
DISCORD_SEND_HELPER = HERMES_SCRIPTS / "discord-send-message.mjs"
STATE_FILE = Path("/path/to/project/.hermes/state/alpha_floor_daily_fill.json")

# Alpha Trading Floor channels discovered via OpenClaw Discord bot on 2026-06-30.
# #daily-report is handled by v73_morning_premarket_brief.py.
# #trade-log is handled by v63_dryrun_event_watcher.py.
CHANNELS: dict[str, str] = {
    "general-main": "1519232614056333315",
    "trading-signals": "1519239667143282718",
    "alerts": "1519239668808552468",
    "bot-commands": "1519239671224336465",
    "general-lounge": "1519239674051166209",
    "introductions": "1519239674936164392",
    "server-health-checks": "1519239675842396250",
    "bot-status": "1519239677947674746",
    "vip-trade-room": "1519241816107515944",
    "premium-signals": "1519241816963285002",
}


def load_premarket_module():
    spec = importlib.util.spec_from_file_location("v73_morning_premarket_brief", PREMARKET_MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {PREMARKET_MODULE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def split_discord(content: str, max_len: int = 1850) -> list[str]:
    chunks: list[str] = []
    current = ""
    for part in content.split("\n\n"):
        candidate = part if not current else current + "\n\n" + part
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(part) <= max_len:
            current = part
        else:
            for i in range(0, len(part), max_len):
                chunks.append(part[i:i + max_len])
            current = ""
    if current:
        chunks.append(current)
    return chunks or [content[:max_len]]


def discord_post(channel_id: str, content: str, *, dry_run: bool = False) -> tuple[bool, str]:
    chunks = split_discord(content)
    if dry_run:
        return True, f"dry-run {len(chunks)} chunk(s)"
    if not DISCORD_SEND_HELPER.exists():
        return False, f"missing helper {DISCORD_SEND_HELPER}"
    urls: list[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        body = chunk if len(chunks) == 1 else f"{chunk}\n\n_Part {idx}/{len(chunks)}_"
        proc = subprocess.run(
            ["/usr/local/bin/node", str(DISCORD_SEND_HELPER), channel_id, body],
            cwd=str(HERMES_SCRIPTS),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "unknown error").strip().splitlines()[-1]
            return False, err[:250]
        try:
            payload = json.loads(proc.stdout or "{}")
            if payload.get("url"):
                urls.append(payload["url"])
        except Exception:
            pass
    return True, urls[0] if urls else f"posted {len(chunks)} chunk(s)"


def qmap(quotes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {q.get("symbol", ""): q for q in quotes}


def fp(m, value: Any) -> str:
    return m.fmt_pct(value)


def price(m, value: Any) -> str:
    return m.fmt_price(value)


def first_items(items: list[str], n: int = 2) -> str:
    return " | ".join(items[:n]) if items else "No major item detected."


def build_context() -> dict[str, Any]:
    m = load_premarket_module()
    now = m.today_pt()
    prev = m.last_trading_day(now)
    index_quotes = [m.yahoo_quote(s) for s in m.INDEX_QUOTES]
    futures = [m.yahoo_quote(s) for s in m.FUTURES]
    movers = [m.yahoo_quote(s) for s in m.WATCHLIST]
    headlines = {name: m.google_news(query, 3) for name, query in m.RSS_QUERIES.items()}
    fomc = m.fomc_events_for_today(now)
    ledger = m.trade_ledger(prev)
    best = m.best_recent_setup()
    read = m.deterministic_read(index_quotes, headlines)
    v7_state = load_json(V7_STATE_FILE, {})
    return {
        "m": m,
        "now": now,
        "date": now.strftime("%Y-%m-%d"),
        "display_date": now.strftime("%B %-d, %Y") if os.name != "nt" else now.strftime("%B %#d, %Y"),
        "previous_session": prev,
        "index_quotes": index_quotes,
        "futures": futures,
        "movers": movers,
        "headlines": headlines,
        "fomc": fomc,
        "ledger": ledger,
        "best": best,
        "read": read,
        "quote_map": qmap(index_quotes),
        "futures_map": qmap(futures),
        "v7_state": v7_state,
    }


def latest_best_text(ctx: dict[str, Any]) -> str:
    return ctx["m"].short_setup_line(ctx.get("best"))


def top_movers_text(ctx: dict[str, Any]) -> str:
    m = ctx["m"]
    movers = sorted(ctx["movers"], key=lambda x: abs(float(x.get("changePct") or 0)), reverse=True)[:4]
    return ", ".join(f"{x.get('symbol')} {fp(m, x.get('changePct'))}" for x in movers)


def channel_messages(ctx: dict[str, Any]) -> dict[str, str]:
    m = ctx["m"]
    q = ctx["quote_map"]
    f = ctx["futures_map"]
    spy = q.get("SPY", {})
    qqq = q.get("QQQ", {})
    vix = q.get("^VIX", {})
    ledger = ctx["ledger"]
    v7 = ctx.get("v7_state", {})
    v7_positions = v7.get("activePositions", [])
    v7_trades = v7.get("dailyTrades", 0)
    v7_pnl = float(v7.get("dailyPnl", 0.0) or 0.0)
    v7_mode = v7.get("mode", "dry_run")
    v7_version = v7.get("version", "unknown-paper-release")
    v7_symbol = v7.get("symbol", "SPY")
    entry_window = m.entry_window_pt(ctx["now"])
    v7_status = f"{v7_version} {v7_mode}: {v7_trades} paper trade(s), {len(v7_positions)} open, realized P&L ${v7_pnl:.2f}."
    date = ctx["display_date"]
    best = latest_best_text(ctx)
    macro = first_items(ctx["headlines"].get("Fed / macro", []), 2)
    geo = first_items(ctx["headlines"].get("War / geopolitics", []), 2)
    events = "; ".join(ctx["fomc"]) if ctx["fomc"] else "No official FOMC meeting detected today."
    regime = ctx["read"]
    movers = top_movers_text(ctx)
    futures_line = f"ES {fp(m, f.get('ES=F', {}).get('changePct'))}, NQ {fp(m, f.get('NQ=F', {}).get('changePct'))}, RTY {fp(m, f.get('RTY=F', {}).get('changePct'))}"
    market_line = f"SPY {price(m, spy.get('price'))} {fp(m, spy.get('changePct'))}, QQQ {price(m, qqq.get('price'))} {fp(m, qqq.get('changePct'))}, VIX {price(m, vix.get('price'))} {fp(m, vix.get('changePct'))}"

    return {
        "general-main": (
            f"**Good morning Alpha Trading Floor — {date}**\n"
            f"Market pulse: {market_line}. Futures: {futures_line}.\n"
            f"Theme: {regime}\n"
            f"Question for the room: are you leaning trend continuation or fade after the first 15 minutes?"
        ),
        "trading-signals": (
            f"**{v7_version} Signal Board — premarket**\n"
            f"Current focus: {v7_symbol} only, {v7_mode} mode, entry window {entry_window}.\n"
            f"Latest best setup/reject snapshot: {best}\n"
            "No forced entries. Wait for gate alignment, tight spreads, and clean momentum confirmation."
        ),
        "alerts": (
            "**Morning risk alerts**\n"
            f"- Fed/FOMC: {events}\n"
            f"- Macro: {macro}\n"
            f"- Geopolitics: {geo}\n"
            "If headline risk spikes, reduce chase behavior and let the scanner reject weak setups."
        ),
        "bot-commands": (
            "**Daily navigation**\n"
            "- Check #daily-report for the full premarket brief.\n"
            f"- Check #trade-log for {v7_version} {v7_mode} entries/rejects.\n"
            "- Check #trading-signals for the current scanner bias.\n"
            "Jarvis posts are deterministic Python today — no AI-generated alert spam."
        ),
        "general-lounge": (
            "**Watchlist conversation starter**\n"
            f"Big movers to watch: {movers}.\n"
            "What ticker has the cleanest chart this morning: SPY, QQQ, TSLA, NVDA, or something else?"
        ),
        "introductions": (
            "**New here? Start with this morning flow:**\n"
            "1. #daily-report = market map.\n"
            "2. #trading-signals = current scanner bias.\n"
            f"3. #trade-log = {v7_version} {v7_mode} entries/rejects.\n"
            "We track process first — no hype or promise-based calls."
        ),
        "server-health-checks": (
            "**Morning server check**\n"
            "Alpha Trading Floor daily fill is online. Discord bot posting path is active.\n"
            f"{v7_version} {v7_mode} scanner remains armed for market-hour entries/rejects."
        ),
        "bot-status": (
            "**Jarvis status**\n"
            f"{v7_status}\n"
            "Morning posts are script-driven and deterministic."
        ),
        "vip-trade-room": (
            "**VIP room premarket plan**\n"
            f"Regime read: {regime}\n"
            f"Ledger: entries {len(ledger['entries'])}, exits {len(ledger['exits'])}, previous-session P&L ${ledger['pnl']:.2f}.\n"
            "Plan: protect capital, avoid lunch chop, and only respect setups with pricing + momentum confirmation."
        ),
        "premium-signals": (
            "**Premium signal criteria today**\n"
            f"- {v7_symbol} only.\n"
            "- Score gate + clean VWAP/EMA/momentum confirmation.\n"
            "- Option spread must stay controlled; weak/expensive contracts get rejected.\n"
            f"Latest setup snapshot: {best}"
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Fill Alpha Trading Floor channels with deterministic morning content.")
    ap.add_argument("--dry-run", action="store_true", help="validate content/channels without posting")
    ap.add_argument("--force", action="store_true", help="post even if today's fill already ran")
    ap.add_argument("--channels", help="comma-separated channel keys to post; defaults to all managed channels")
    args = ap.parse_args()

    ctx = build_context()
    post_key = ctx["date"]
    state = load_json(STATE_FILE, {})
    if not args.force and state.get("last_post_key") == post_key:
        return 0

    messages = channel_messages(ctx)
    selected = [c.strip() for c in args.channels.split(",")] if args.channels else list(CHANNELS.keys())
    results: list[dict[str, str]] = []
    for key in selected:
        if key not in CHANNELS:
            results.append({"channel": key, "ok": "false", "status": "unknown channel key"})
            continue
        ok, status = discord_post(CHANNELS[key], messages[key], dry_run=args.dry_run)
        results.append({"channel": key, "ok": str(ok).lower(), "status": status})

    if not args.dry_run and all(r["ok"] == "true" for r in results):
        state.update({
            "last_post_key": post_key,
            "last_posted_at": datetime.now().isoformat(),
            "channels": selected,
        })
        save_json(STATE_FILE, state)

    ok_count = sum(1 for r in results if r["ok"] == "true")
    fail = [r for r in results if r["ok"] != "true"]
    mode = "DRY RUN" if args.dry_run else "POSTED"
    lines = [f"Alpha Trading Floor daily fill {mode}: {ok_count}/{len(results)} channels."]
    lines.append("Covered now: " + ", ".join(r["channel"] for r in results if r["ok"] == "true"))
    lines.append("Already covered by other no-agent jobs: #daily-report, #trade-log.")
    if fail:
        lines.append("Failures: " + "; ".join(f"{r['channel']}: {r['status']}" for r in fail))
    lines.append("No LLM/model call used.")
    print("\n".join(lines))
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
