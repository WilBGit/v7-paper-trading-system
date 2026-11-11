#!/usr/bin/env python3
"""
Deterministic active V7 morning premarket brief.

No LLM calls. Designed for Hermes no_agent cron: stdout is delivered verbatim,
and failures are surfaced by the scheduler.

Restored from .pyc analysis + alpha_floor_daily_fill.py integration requirements.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import v7_scalp_paper as engine

PT = ZoneInfo("America/Los_Angeles")
ET_ZONE = ZoneInfo("America/New_York")

# Load .env for DISCORD_BOT_TOKEN and the active V7 channel setting. The V63
# environment key remains a compatibility fallback during migration.
_dotenv = Path(__file__).parent / ".env"
if _dotenv.exists():
    for _line in _dotenv.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))

HERMES_STATE = Path("/path/to/project/.hermes/state")
V7_STATE = HERMES_STATE / "v7_scalp_state.json"
TRADES = HERMES_STATE / "v7_scalp_trades.jsonl"
DECISIONS = HERMES_STATE / "v7_scalp_scan_attempts.jsonl"

INDEX_QUOTES = ["SPY", "QQQ", "^VIX", "^TNX"]
FUTURES = ["ES=F", "NQ=F", "RTY=F", "^VXV"]
WATCHLIST = ["SPY", "QQQ", "NVDA", "AAPL", "TSLA", "AMD", "META", "AMZN", "VIXY"]
RSS_QUERIES = {
    "Fed / macro": "Federal Reserve OR FOMC OR CPI OR jobs report OR unemployment OR Treasury yields when:1d",
    "War / geopolitics": "Ukraine Russia OR Iran Israel OR Middle East OR Taiwan China OR oil sanctions when:1d",
    "Market movers": "SPY QQQ NVDA AAPL TSLA stocks premarket when:1d",
}
YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY,QQQ,NVDA,AAPL,TSLA&region=US&lang=en-US"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

OPENCLAW_WORKSPACE = Path("/path/to/project/.openclaw/workspace")
DISCORD_SEND_HELPER = Path("/path/to/project/.hermes/scripts/discord-send-message.mjs")

DISCORD_CHANNEL_ID = (
    os.environ.get("V73_DISCORD_CHANNEL_ID")
    or os.environ.get("V63_DISCORD_CHANNEL_ID", "")
)
DISCORD_POST_STATE = HERMES_STATE / "v73_morning_premarket_brief_discord.json"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def fetch_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_json(url: str, timeout: int = 20) -> Any:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def load_post_state() -> dict[str, Any]:
    try:
        return json.loads(DISCORD_POST_STATE.read_text())
    except Exception:
        return {}


def save_post_state(state: dict[str, Any]) -> None:
    DISCORD_POST_STATE.parent.mkdir(parents=True, exist_ok=True)
    DISCORD_POST_STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


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


def post_discord(content: str, post_key: str) -> tuple[bool, str]:
    state = load_post_state()
    if not os.environ.get("FORCE_POST") and state.get("last_post_key") == post_key:
        return True, "already-posted"
    if not DISCORD_CHANNEL_ID:
        return False, "V73_DISCORD_CHANNEL_ID (or legacy V63_DISCORD_CHANNEL_ID) not set"
    if not DISCORD_SEND_HELPER.exists():
        return False, f"missing {DISCORD_SEND_HELPER}"

    chunks = split_discord(content)
    urls: list[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        body = chunk if len(chunks) == 1 else f"{chunk}\n\n_Part {idx}/{len(chunks)}_"
        proc = subprocess.run(
            ["/usr/local/bin/node", str(DISCORD_SEND_HELPER), DISCORD_CHANNEL_ID, body],
            cwd=str(OPENCLAW_WORKSPACE),
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
    state["last_post_key"] = post_key
    state["last_posted_at"] = datetime.now().isoformat()
    save_post_state(state)
    return True, urls[0] if urls else f"posted {len(chunks)} chunk(s)"


def yahoo_quote(symbol: str) -> dict[str, Any]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1m&includePrePost=true"
    try:
        data = fetch_json(url)
        result = data.get("chart", {}).get("result", [{}])[0]
        meta = result.get("meta", {})
        q = result.get("indicators", {}).get("quote", [{}])[0]
        closes = [c for c in q.get("close", []) if c is not None]
        price = meta.get("regularMarketPrice") or meta.get("previousClose") or (closes[-1] if closes else None)
        prev = meta.get("previousClose") or (closes[-2] if len(closes) >= 2 else None)
        change_pct = ((price - prev) / prev * 100) if price and prev and prev != 0 else None
        return {
            "symbol": symbol,
            "price": price,
            "previousClose": prev,
            "changePct": change_pct,
            "volume": q.get("volume", [None])[-1] if q.get("volume") else None,
            "high": meta.get("regularMarketDayHigh"),
            "low": meta.get("regularMarketDayLow"),
        }
    except Exception:
        return {"symbol": symbol, "price": None, "changePct": None}


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def parse_ts(s: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return parsed
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.min


def today_pt() -> datetime:
    return datetime.now(PT)


def last_trading_day(dt: datetime) -> datetime:
    d = dt - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def fmt_pct(v: Any) -> str:
    if v is None:
        return "N/A"
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "N/A"


def fmt_price(v: Any) -> str:
    if v is None:
        return "N/A"
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return "N/A"


def quote_line(q: dict) -> str:
    sym = q.get("symbol", "?")
    price = fmt_price(q.get("price"))
    pct = fmt_pct(q.get("changePct"))
    return f"{sym} {price} ({pct})"


def rss_items(url: str, limit: int = 5) -> list[str]:
    try:
        text = fetch_text(url)
        root = ET.fromstring(text)
        items = []
        for item in root.findall(".//item")[:limit]:
            title = item.findtext("title", "")
            if title:
                items.append(title)
        return items
    except Exception:
        return []


def google_news(query: str, limit: int = 3) -> list[str]:
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) + "&hl=en-US&gl=US&ceid=US:en"
    return rss_items(url, limit)


def month_name_to_num(name: str) -> int:
    months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
    return months.get(name[:3].lower(), 0)


def fomc_events_for_today(now: datetime) -> list[str]:
    try:
        text = fetch_text(FOMC_URL)
        events = []
        for m in re.finditer(r"(\w+)\s+(\d{4})\s+FOMC\s+Meeting", text):
            month = month_name_to_num(m.group(1))
            year = int(m.group(2))
            if month == now.month and year == now.year:
                events.append(f"FOMC meeting: {m.group(0)}")
        return events
    except Exception:
        return []


def trade_ledger(session_date: datetime) -> dict:
    rows = read_jsonl(TRADES)
    target_date = (
        session_date.astimezone(ET_ZONE).date()
        if session_date.tzinfo else session_date.date()
    )
    session_rows = []
    for row in rows:
        parsed = parse_ts(str(row.get("timestamp", "")))
        if parsed == datetime.min:
            continue
        event_date = parsed.astimezone(ET_ZONE).date() if parsed.tzinfo else parsed.date()
        if event_date == target_date:
            session_rows.append(row)
    entries = [r for r in session_rows if r.get("event") == "PAPER_ENTRY_REAL_ASK"]
    exits = [r for r in session_rows if r.get("event") == "PAPER_EXIT_REAL_BID"]
    exited_ids = {r.get("positionId") for r in exits}
    open_trades = [r for r in entries if r.get("positionId") not in exited_ids]
    total_pnl = sum(float(r.get("pnl", 0) or 0) for r in exits)
    return {"entries": entries, "exits": exits, "open": open_trades, "pnl": total_pnl}


def best_recent_setup() -> dict[str, Any] | None:
    rows = read_jsonl(DECISIONS)
    if not rows:
        return None
    routine_controls = {
        "market_closed", "outside_entry_window", "lunch_chop_window",
        "position_already_open", "pending_entry_exists",
    }
    for row in reversed(rows):
        result = row.get("result") if isinstance(row.get("result"), dict) else row
        diagnostic = row.get("diagnostic") if isinstance(row.get("diagnostic"), dict) else {}
        action = str(result.get("action") or result.get("decision") or "unknown")
        reason = str(
            result.get("reason") or result.get("error")
            or diagnostic.get("primaryBlocker") or ""
        )
        if reason.split(":", 1)[-1] in routine_controls:
            continue
        signal = result.get("signal") if isinstance(result.get("signal"), dict) else {}
        return {
            "symbol": result.get("symbol") or engine.SYMBOL,
            "decision": action.lower(),
            "reason": reason,
            "direction": signal.get("direction") or signal.get("rawDirection"),
            "score": signal.get("score"),
        }
    return None


def short_setup_line(s: dict[str, Any] | None) -> str:
    if not s:
        return "No recent setup data."
    sym = s.get("symbol", "?")
    dec = s.get("decision", "?")
    reason = s.get("reason", s.get("keeper_reason", ""))
    qualifiers = []
    if s.get("direction"):
        qualifiers.append(str(s["direction"]))
    if s.get("score") is not None:
        qualifiers.append(f"score {s['score']}")
    base = f"{sym} {dec}"
    if qualifiers:
        base += " (" + ", ".join(qualifiers) + ")"
    return f"{base}: {reason}" if reason else base


def config_line() -> str:
    cfg = read_json(V7_STATE, {}) or {}
    version = engine.ENGINE_VERSION
    mode = str(cfg.get("mode") or "dry_run").upper()
    symbol = cfg.get("symbol") or engine.SYMBOL
    return (
        f"{version} {mode}, {symbol} {engine.OPTION_DTE_TARGET}DTE, "
        f"score >= {engine.MIN_ENTRY_SCORE}, "
        f"debit ${engine.MIN_DEBIT:g}-${engine.MAX_DEBIT:g}"
    )


def entry_window_pt(now: datetime) -> str:
    """Render the active engine's ET entry window in Pacific time."""
    current = now.astimezone(PT) if now.tzinfo else now.replace(tzinfo=PT)
    start_et = datetime(
        current.year, current.month, current.day,
        engine.ENTRY_START // 100, engine.ENTRY_START % 100,
        tzinfo=ET_ZONE,
    )
    end_et = datetime(
        current.year, current.month, current.day,
        engine.ENTRY_END // 100, engine.ENTRY_END % 100,
        tzinfo=ET_ZONE,
    )
    return f"{start_et.astimezone(PT):%H:%M}-{end_et.astimezone(PT):%H:%M} PT"


def deterministic_read(index_quotes: list[dict], headlines: dict[str, list[str]]) -> str:
    spy_q = next((q for q in index_quotes if q.get("symbol") == "SPY"), {})
    spy_pct = spy_q.get("changePct")
    vix_q = next((q for q in index_quotes if q.get("symbol") == "^VIX"), {})
    vix_pct = vix_q.get("changePct")
    vix_price = vix_q.get("price")

    if spy_pct is None or vix_price is None:
        return "Market data unavailable — check data freshness"

    spy_abs = abs(spy_pct)

    if spy_pct > 1.0:
        return f"STRONG BULLISH: SPY {fmt_pct(spy_pct)}, VIX {fmt_price(vix_price)}"
    elif spy_pct > 0.3:
        return f"BULLISH BIAS: SPY {fmt_pct(spy_pct)}, VIX {fmt_price(vix_price)}"
    elif spy_pct < -1.0:
        return f"STRONG BEARISH: SPY {fmt_pct(spy_pct)}, VIX {fmt_price(vix_price)}"
    elif spy_pct < -0.3:
        return f"BEARISH BIAS: SPY {fmt_pct(spy_pct)}, VIX {fmt_price(vix_price)}"
    elif vix_price and float(vix_price) > 25:
        return f"HIGH VOL NEUTRAL: SPY {fmt_pct(spy_pct)}, VIX {fmt_price(vix_price)}"
    else:
        return f"RANGEBOUND: SPY {fmt_pct(spy_pct)}, VIX {fmt_price(vix_price)}"


def main() -> int:
    ap = argparse.ArgumentParser(description=f"Deterministic V{engine.ENGINE_VERSION} morning premarket brief.")
    ap.add_argument("--dry-run", action="store_true", help="print brief without posting")
    ap.add_argument("--force", action="store_true", help="post even if already posted today")
    args = ap.parse_args()

    now = today_pt()
    prev = last_trading_day(now)
    date_str = now.strftime("%Y-%m-%d")
    display_date = now.strftime("%B %-d, %Y") if os.name != "nt" else now.strftime("%B %#d, %Y")

    # Fetch data
    index_quotes = [yahoo_quote(s) for s in INDEX_QUOTES]
    futures = [yahoo_quote(s) for s in FUTURES]
    movers = [yahoo_quote(s) for s in WATCHLIST]
    headlines = {name: google_news(query, 3) for name, query in RSS_QUERIES.items()}
    fomc = fomc_events_for_today(now)
    ledger = trade_ledger(prev)
    best = best_recent_setup()
    read = deterministic_read(index_quotes, headlines)

    # Build brief
    spy = next((q for q in index_quotes if q.get("symbol") == "SPY"), {})
    qqq = next((q for q in index_quotes if q.get("symbol") == "QQQ"), {})
    vix = next((q for q in index_quotes if q.get("symbol") == "^VIX"), {})
    tnx = next((q for q in index_quotes if q.get("symbol") == "^TNX"), {})

    es = next((q for q in futures if q.get("symbol") == "ES=F"), {})
    nq = next((q for q in futures if q.get("symbol") == "NQ=F"), {})
    rty = next((q for q in futures if q.get("symbol") == "RTY=F"), {})

    movers_sorted = sorted(movers, key=lambda x: abs(float(x.get("changePct") or 0)), reverse=True)[:5]
    movers_line = ", ".join(f"{m.get('symbol')} {fmt_pct(m.get('changePct'))}" for m in movers_sorted)

    # Build premarket levels from daemon snapshots
    def _premarket_levels_section() -> str:
        lines = []
        for sym in engine.SYMBOLS:
            try:
                p = engine.premarket_path(sym)
                if not p.exists():
                    continue
                data = json.loads(p.read_text())
                if not data.get("ready"):
                    continue
                lines.append(
                    f"{sym}: PM H {data['premarketHigh']:.2f} / L {data['premarketLow']:.2f} "
                    f"| Bull tgt {data['bullishBreakoutTarget']:.2f} / Bear tgt {data['bearishBreakoutTarget']:.2f} "
                    f"| Active: {data['activeDirection']} → {data['activePriceTarget']:.2f} "
                    f"({data['targetState']})"
                )
            except Exception:
                pass
        return "**Premarket Levels:** " + "\n".join(lines) + "\n\n" if lines else ""

    brief = (
        f"**📊 V{engine.ENGINE_VERSION} Premarket Brief — {display_date}**\n\n"
        f"**Indices:** SPY {fmt_price(spy.get('price'))} ({fmt_pct(spy.get('changePct'))}), "
        f"QQQ {fmt_price(qqq.get('price'))} ({fmt_pct(qqq.get('changePct'))}), "
        f"VIX {fmt_price(vix.get('price'))} ({fmt_pct(vix.get('changePct'))}), "
        f"10Y {fmt_price(tnx.get('price'))}%\n\n"
        f"**Futures:** ES {fmt_pct(es.get('changePct'))}, NQ {fmt_pct(nq.get('changePct'))}, "
        f"RTY {fmt_pct(rty.get('changePct'))}\n\n"
        f"**Regime:** {read}\n\n"
        f"**Top Movers:** {movers_line}\n\n"
        f"{_premarket_levels_section()}"
    )

    if fomc:
        brief += f"**FOMC:** {'; '.join(fomc)}\n\n"

    for cat, items in headlines.items():
        if items:
            brief += f"**{cat}:** " + " | ".join(items[:2]) + "\n"

    brief += (
        f"\n**Scanner:** {config_line()}\n"
        f"**Last setup:** {short_setup_line(best)}\n"
        f"**Ledger:** {len(ledger['entries'])} entries, {len(ledger['exits'])} exits, "
        f"P&L ${ledger['pnl']:.2f}\n\n"
        f"_{config_line()}. No forced entries. Data as of {now.strftime('%H:%M')} PT._"
    )

    if args.dry_run:
        print(brief)
        return 0

    if args.force:
        os.environ["FORCE_POST"] = "1"

    ok, status = post_discord(brief, date_str)
    if ok:
        print(f"Posted premarket brief for {date_str}: {status}")
        return 0
    else:
        print(f"Failed to post premarket brief: {status}")
        print(brief)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
