#!/usr/bin/env python3
"""Deterministic, read-only Robinhood MCP quote adapter for V7 paper trading.

No LLM or agent loop is involved. Four market-data tools are hard-allowlisted;
order, account, portfolio, and position tools are unreachable from this CLI.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import inspect
import json
import math
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

OUT_DIR = Path("/path/to/project/.hermes/state/quotes")
HERMES_ROOT = Path.home() / ".hermes" / "hermes-agent"
# Re-exec in the interpreter that launched this bridge. The previous uv venv
# was removed; the parent daemon and worker already provide the active runtime.
HERMES_PYTHON = Path(sys.executable)
MCP_PREFIX = "mcp__robinhood_trading__"
ET = ZoneInfo("America/New_York")
READ_ONLY_METHODS = frozenset({
    "get_equity_quotes",
    "get_option_chains",
    "get_option_instruments",
    "get_option_quotes",
})
ENGINE_SOURCE = "Robinhood MCP"
ALLOWED_UNDERLYINGS = frozenset({"SPY", "QQQ"})
# A synthetic XSP spot is accepted only when executable parity produces a tight range.
MAX_INDEX_PARITY_SPREAD_PCT = 0.25
MAX_INDEX_REFERENCE_DEVIATION_PCT = 1.00
INDEX_PARITY_MAX_PAIRS = 8


class BridgeError(RuntimeError):
    pass


_MCP_DISCOVERED = False


def _run_discovery(discover):
    """Support installed synchronous discovery and future async variants."""
    result = discover()
    return asyncio.run(result) if inspect.isawaitable(result) else result


def _direct_dispatch(tool_name: str, arguments: dict[str, Any]) -> Any:
    """Dispatch through Hermes' authenticated MCP registry with no model call."""
    global _MCP_DISCOVERED
    allowed_names = {MCP_PREFIX + method for method in READ_ONLY_METHODS}
    if tool_name not in allowed_names:
        raise BridgeError(f"mcp_tool_not_allowlisted:{tool_name}")
    root = str(HERMES_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from model_tools import handle_function_call
        try:
            from tools.mcp_tool_discovery import discover_mcp_tools
        except ImportError:
            from tools.mcp_tool import discover_mcp_tools
    except Exception as exc:
        raise BridgeError(f"hermes_mcp_import_failed:{type(exc).__name__}") from exc

    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            if not _MCP_DISCOVERED:
                discovered = _run_discovery(discover_mcp_tools)
                if tool_name not in set(discovered or []):
                    raise BridgeError(f"required_mcp_tool_not_discovered:{tool_name}")
                _MCP_DISCOVERED = True
            return handle_function_call(
                tool_name,
                arguments,
                task_id="v7423-paper-readonly-quotes",
                user_task="Deterministic multi-symbol paper quote retrieval",
            )
    except Exception as exc:
        detail = captured.getvalue()[-500:].replace("\n", " ")
        raise BridgeError(
            f"mcp_dispatch_failed:{type(exc).__name__}:{exc!s}:{detail}"
        ) from exc


def _normalize_dispatch_result(raw: Any) -> dict[str, Any]:
    try:
        outer = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise BridgeError("mcp_dispatch_invalid_json") from exc
    if not isinstance(outer, dict):
        raise BridgeError("mcp_dispatch_not_object")
    if outer.get("error"):
        raise BridgeError(f"mcp_dispatch_error:{outer['error']}")
    structured = outer.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    result = outer.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError as exc:
            raise BridgeError("mcp_result_invalid_json") from exc
    if isinstance(result, dict):
        if result.get("error"):
            raise BridgeError(f"mcp_result_error:{result['error']}")
        return result
    if "data" in outer:
        return outer
    raise BridgeError("mcp_dispatch_missing_structured_result")


def mcp_call(method: str, arguments: dict[str, Any], dispatcher=None) -> dict[str, Any]:
    """Invoke one explicitly allowed read-only Robinhood MCP method."""
    if method not in READ_ONLY_METHODS:
        raise BridgeError(f"mcp_method_not_allowlisted:{method}")
    if dispatcher is None:
        dispatcher = _direct_dispatch
    return _normalize_dispatch_result(dispatcher(MCP_PREFIX + method, arguments))


def _finite_number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise BridgeError(f"invalid_number:{field}") from exc
    if not math.isfinite(number):
        raise BridgeError(f"non_finite_number:{field}")
    return number


def _live_underlying_price(quote: dict[str, Any], fallback: float) -> float:
    """Prefer the current executable market midpoint over a completed-bar close."""
    try:
        bid = _finite_number(quote.get("bid_price"), "equity_bid_price")
        ask = _finite_number(quote.get("ask_price"), "equity_ask_price")
        if bid > 0 and ask >= bid:
            return (bid + ask) / 2.0
    except BridgeError:
        pass
    for field in ("last_non_reg_trade_price", "last_trade_price"):
        try:
            value = _finite_number(quote.get(field), field)
            if value > 0:
                return value
        except BridgeError:
            continue
    return fallback


def _equity_quote_timestamp(quote: dict[str, Any], fallback: str) -> str:
    """Timestamp an executable midpoint by its older bid/ask side."""
    bid_time = quote.get("venue_bid_time")
    ask_time = quote.get("venue_ask_time")
    if isinstance(bid_time, str) and bid_time and isinstance(ask_time, str) and ask_time:
        return min(bid_time, ask_time)
    for field in ("venue_last_non_reg_trade_time", "venue_last_trade_time"):
        value = quote.get(field)
        if isinstance(value, str) and value:
            return value
    return fallback


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Return Gregorian Easter using the Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed_fixed_holiday(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _market_holidays(year: int) -> set[date]:
    """Return standard full-day U.S. equity-market holidays for one year."""
    holidays = {
        _nth_weekday(year, 1, 0, 3),   # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),   # Washington's Birthday
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),     # Memorial Day
        _observed_fixed_holiday(date(year, 6, 19)),  # Juneteenth
        _observed_fixed_holiday(date(year, 7, 4)),   # Independence Day
        _nth_weekday(year, 9, 0, 1),   # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving Day
        _observed_fixed_holiday(date(year, 12, 25)), # Christmas Day
    }
    new_year = date(year, 1, 1)
    # NYSE observes a Sunday New Year's Day on Monday. When it falls on
    # Saturday, the preceding Friday remains an open session.
    holidays.add(new_year + timedelta(days=1) if new_year.weekday() == 6 else new_year)
    return holidays


def is_market_holiday(day: date) -> bool:
    """Return whether the U.S. equity/index market is closed all day."""
    return day in _market_holidays(day.year)


def _business_day_dte(now: datetime, expiration: date) -> int:
    cursor = now.astimezone(ET).date()
    holidays: set[date] = set()
    for year in range(cursor.year, expiration.year + 1):
        holidays.update(_market_holidays(year))
    count = 0
    while cursor < expiration:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5 and cursor not in holidays:
            count += 1
    return count


def _target_expiration(
    chains: list[dict[str, Any]], now: datetime, dte: int, symbol: str = "SPY",
    *, allow_same_day: bool = False,
) -> tuple[str, str]:
    symbol = symbol.upper()
    if symbol not in ALLOWED_UNDERLYINGS:
        raise BridgeError(f"unsupported_underlying:{symbol}")
    session_date = now.astimezone(ET).date()
    for chain in chains:
        if str(chain.get("symbol", "")).upper() != symbol:
            continue
        chain_id = str(chain.get("id", ""))
        for value in chain.get("expiration_dates", []):
            try:
                expiration = date.fromisoformat(str(value))
            except ValueError:
                continue
            same_day = allow_same_day and dte == 0 and expiration == session_date
            future_day = expiration > session_date and _business_day_dte(now, expiration) == dte
            if same_day or future_day:
                if not chain_id:
                    raise BridgeError("chain_missing_id")
                return chain_id, expiration.isoformat()
    raise BridgeError(f"no_{symbol.lower()}_expiration_for_{dte}dte")


def _cursor_from_next(next_url: Any) -> str | None:
    if not isinstance(next_url, str) or not next_url:
        return None
    values = parse_qs(urlparse(next_url).query).get("cursor", [])
    if values:
        return values[0]
    if next_url.startswith(("http://", "https://", "/")):
        return None  # a URL without a cursor query param means no next page
    return next_url  # bare cursor token returned by the MCP verbatim


def _collect_instruments(
    chain_id: str,
    expiration: str,
    option_type: str | None,
    caller,
    max_pages: int = 6,
) -> list[dict[str, Any]]:
    instruments: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(max_pages):
        arguments: dict[str, Any] = {
            "chain_id": chain_id,
            "expiration_dates": expiration,
            "state": "active",
        }
        if option_type is not None:
            arguments["type"] = option_type
        if cursor:
            arguments["cursor"] = cursor
        payload = caller("get_option_instruments", arguments)
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        page = data.get("instruments", [])
        if not isinstance(page, list):
            raise BridgeError("option_instruments_not_list")
        instruments.extend(item for item in page if isinstance(item, dict))
        cursor = _cursor_from_next(data.get("next"))
        if not cursor:
            break
    else:
        raise BridgeError("option_instrument_pagination_limit")
    return instruments


def _occ_symbol(expiration: str, option_type: str, strike: float, symbol: str = "SPY") -> str:
    symbol = symbol.upper()
    if symbol not in ALLOWED_UNDERLYINGS:
        raise BridgeError(f"unsupported_underlying:{symbol}")
    expiry = date.fromisoformat(expiration)
    code = "C" if option_type == "call" else "P"
    strike_code = int(round(strike * 1000))
    if strike_code < 0 or strike_code > 99_999_999:
        raise BridgeError("strike_out_of_occ_range")
    return f"{symbol}{expiry:%y%m%d}{code}{strike_code:08d}"


def _index_leg_quote(instrument_id: str, caller) -> dict[str, Any]:
    """Fetch one XSP parity leg and reject missing, crossed, or timeless books."""
    payload = caller("get_option_quotes", {"instrument_ids": [instrument_id]})
    results = payload.get("data", {}).get("results", [])
    if len(results) != 1 or not isinstance(results[0], dict):
        raise BridgeError("index_leg_quote_result_count_mismatch")
    raw = results[0].get("quote", {})
    if str(raw.get("instrument_id", "")) != instrument_id:
        raise BridgeError("index_leg_quote_identity_mismatch")
    bid = _finite_number(raw.get("bid_price"), "index_leg_bid_price")
    ask = _finite_number(raw.get("ask_price"), "index_leg_ask_price")
    if bid <= 0 or ask < bid:
        raise BridgeError("index_leg_non_executable_book")
    timestamp = raw.get("updated_at")
    if not isinstance(timestamp, str) or not timestamp:
        raise BridgeError("index_leg_missing_updated_at")
    try:
        observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise BridgeError("index_leg_invalid_updated_at") from exc
    return {
        "instrument_id": instrument_id,
        "bid": bid,
        "ask": ask,
        "mark": _finite_number(raw.get("mark_price"), "index_leg_mark_price"),
        "timestamp": timestamp,
        "observed": observed,
    }


def _matching_index_pairs(
    calls: list[dict[str, Any]],
    puts: list[dict[str, Any]],
    *,
    chain_id: str,
    expiration: str,
    symbol: str,
) -> list[tuple[float, dict[str, Any], dict[str, Any]]]:
    """Return exact active/tradable call-put pairs, keyed by strike."""
    per_type: dict[str, dict[float, list[dict[str, Any]]]] = {"call": {}, "put": {}}
    for option_type, instruments in (("call", calls), ("put", puts)):
        for instrument in instruments:
            if (
                instrument.get("chain_id") != chain_id
                or str(instrument.get("chain_symbol", "")).upper() != symbol
                or instrument.get("expiration_date") != expiration
                or instrument.get("type") != option_type
                or instrument.get("state") != "active"
                or instrument.get("tradability") != "tradable"
            ):
                continue
            instrument_id = str(instrument.get("id", ""))
            if not instrument_id:
                continue
            strike = _finite_number(instrument.get("strike_price"), "index_pair_strike")
            per_type[option_type].setdefault(round(strike, 3), []).append({
                "instrument_id": instrument_id,
                "strike": strike,
            })
    pairs: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for key in set(per_type["call"]).intersection(per_type["put"]):
        call_matches = per_type["call"][key]
        put_matches = per_type["put"][key]
        # Ambiguous contract identity is not safe enough to synthesize a spot.
        if len(call_matches) == 1 and len(put_matches) == 1:
            pairs.append((call_matches[0]["strike"], call_matches[0], put_matches[0]))
    return pairs


def build_index_quote(
    symbol: str,
    *,
    reference_underlying: float,
    dte: int = 1,
    caller=mcp_call,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a fail-closed XSP spot range from matched executable call/put parity.

    Robinhood does not expose XSP through the equity quote endpoint.  This keeps
    the price source broker-native without treating ES futures or an ETF as XSP:
    ``bid = K + call_bid - put_ask`` and ``ask = K + call_ask - put_bid``.
    """
    symbol = symbol.upper()
    if symbol != "XSP":
        raise BridgeError(f"unsupported_index_symbol:{symbol}")
    now = observed_at or datetime.now(timezone.utc)
    reference = _finite_number(reference_underlying, "index_reference_underlying")
    if reference <= 0 or dte < 1:
        raise BridgeError("invalid_index_quote_bounds")
    chain_payload = caller("get_option_chains", {"underlying_symbol": symbol})
    chains = chain_payload.get("data", {}).get("chains", [])
    if not isinstance(chains, list):
        raise BridgeError("index_option_chains_not_list")
    chain_id, expiration = _target_expiration(chains, now, dte, symbol)
    calls = _collect_instruments(chain_id, expiration, "call", caller)
    puts = _collect_instruments(chain_id, expiration, "put", caller)
    pairs = _matching_index_pairs(
        calls, puts, chain_id=chain_id, expiration=expiration, symbol=symbol,
    )
    if not pairs:
        raise BridgeError("index_parity_no_matched_instruments")
    pairs.sort(key=lambda item: abs(item[0] - reference))

    for strike, call_instrument, put_instrument in pairs[:INDEX_PARITY_MAX_PAIRS]:
        try:
            call_quote = _index_leg_quote(call_instrument["instrument_id"], caller)
            put_quote = _index_leg_quote(put_instrument["instrument_id"], caller)
        except BridgeError:
            continue
        bid = strike + call_quote["bid"] - put_quote["ask"]
        ask = strike + call_quote["ask"] - put_quote["bid"]
        if bid <= 0 or ask < bid:
            continue
        mark = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mark * 100.0
        reference_deviation_pct = abs(mark - reference) / reference * 100.0
        if (
            spread_pct > MAX_INDEX_PARITY_SPREAD_PCT
            or reference_deviation_pct > MAX_INDEX_REFERENCE_DEVIATION_PCT
        ):
            continue
        older_leg = min((call_quote, put_quote), key=lambda item: item["observed"])
        return {
            "kind": "index_quote",
            "source": ENGINE_SOURCE,
            "timestamp": older_leg["timestamp"],
            "fetched_at": now.isoformat(),
            "symbol": symbol,
            "quote": {
                "symbol": symbol,
                "bid_price": round(bid, 6),
                "ask_price": round(ask, 6),
                "mark_price": round(mark, 6),
                "updated_at": older_leg["timestamp"],
            },
            "pair": {
                "chainId": chain_id,
                "expirationDate": expiration,
                "strike": str(strike),
                "callInstrumentId": call_instrument["instrument_id"],
                "putInstrumentId": put_instrument["instrument_id"],
                "callUpdatedAt": call_quote["timestamp"],
                "putUpdatedAt": put_quote["timestamp"],
                "paritySpreadPct": round(spread_pct, 6),
                "referenceDeviationPct": round(reference_deviation_pct, 6),
            },
            "reference_underlying": reference,
        }
    raise BridgeError("index_parity_no_valid_executable_pair")


def build_scalp_candidates(
    *,
    symbol: str = "SPY",
    option_type: str,
    underlying: float,
    dte: int,
    min_debit: float,
    max_debit: float,
    max_otm_pct: float,
    caller=mcp_call,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Build normalized paper candidates with no LLM and no order capability."""
    symbol = symbol.upper()
    if symbol not in ALLOWED_UNDERLYINGS:
        raise BridgeError(f"unsupported_underlying:{symbol}")
    if option_type not in {"call", "put"}:
        raise BridgeError("option_type_must_be_call_or_put")
    now = observed_at or datetime.now(timezone.utc)
    underlying = _finite_number(underlying, "underlying")
    min_debit = _finite_number(min_debit, "min_debit")
    max_debit = _finite_number(max_debit, "max_debit")
    max_otm_pct = _finite_number(max_otm_pct, "max_otm_pct")
    if underlying <= 0 or dte < 1 or min_debit < 0 or max_debit < min_debit:
        raise BridgeError("invalid_scalp_candidate_bounds")

    if symbol == "XSP":
        underlying_quote = build_index_quote(
            symbol,
            reference_underlying=underlying,
            dte=dte,
            caller=caller,
            observed_at=now,
        )
        equity_quote = underlying_quote["quote"]
        underlying = round(_live_underlying_price(equity_quote, underlying), 6)
    else:
        equity = caller("get_equity_quotes", {"symbols": [symbol]})
        equity_results = equity.get("data", {}).get("results", [])
        if not equity_results or equity_results[0].get("quote", {}).get("symbol") != symbol:
            raise BridgeError(f"equity_identity_mismatch:{symbol}")
        equity_quote = equity_results[0].get("quote", {})
        underlying = _live_underlying_price(equity_quote, underlying)
        underlying_quote = {
            "kind": "equity_quote",
            "source": ENGINE_SOURCE,
            "symbol": symbol,
            "timestamp": _equity_quote_timestamp(equity_quote, now.isoformat()),
            "quote": equity_quote,
        }

    chain_payload = caller("get_option_chains", {"underlying_symbol": symbol})
    chains = chain_payload.get("data", {}).get("chains", [])
    if not isinstance(chains, list):
        raise BridgeError("option_chains_not_list")
    chain_id, expiration = _target_expiration(chains, now, dte, symbol)
    instruments = _collect_instruments(chain_id, expiration, option_type, caller)

    selected: dict[str, dict[str, Any]] = {}
    for instrument in instruments:
        if (
            instrument.get("chain_id") != chain_id
            or str(instrument.get("chain_symbol", "")).upper() != symbol
            or instrument.get("expiration_date") != expiration
            or instrument.get("type") != option_type
            or instrument.get("state") != "active"
            or instrument.get("tradability") != "tradable"
        ):
            continue
        instrument_id = str(instrument.get("id", ""))
        if not instrument_id:
            continue
        strike = _finite_number(instrument.get("strike_price"), "strike_price")
        if option_type == "call":
            otm_pct = max(0.0, (strike - underlying) / underlying * 100.0)
            intrinsic = max(0.0, underlying - strike)
        else:
            otm_pct = max(0.0, (underlying - strike) / underlying * 100.0)
            intrinsic = max(0.0, strike - underlying)
        if otm_pct > max_otm_pct or intrinsic * 100.0 > max_debit:
            continue
        selected[instrument_id] = {**instrument, "_strike": strike, "_otmPct": otm_pct}

    instrument_ids = sorted(selected, key=lambda key: abs(selected[key]["_strike"] - underlying))[:20]
    if not instrument_ids:
        return {
            "source": "robinhood_mcp",
            "timestamp": now.isoformat(),
            "underlying": underlying,
            "equityQuote": equity_quote,
            "underlyingQuote": underlying_quote,
            "candidates": [],
        }

    quotes_payload = caller("get_option_quotes", {"instrument_ids": instrument_ids})
    quote_results = quotes_payload.get("data", {}).get("results", [])
    if not isinstance(quote_results, list):
        raise BridgeError("option_quotes_not_list")
    candidates: list[dict[str, Any]] = []
    for result in quote_results:
        quote = result.get("quote", {}) if isinstance(result, dict) else {}
        instrument_id = str(quote.get("instrument_id", ""))
        instrument = selected.get(instrument_id)
        if not instrument:
            raise BridgeError("quote_instrument_not_requested")
        ask = _finite_number(quote.get("ask_price"), "ask_price")
        bid = _finite_number(quote.get("bid_price"), "bid_price")
        cost = ask * 100.0
        if cost < min_debit or cost > max_debit:
            continue
        strike = instrument["_strike"]
        candidates.append({
            "symbol": symbol,
            "occSymbol": _occ_symbol(expiration, option_type, strike, symbol),
            "instrumentId": instrument_id,
            "type": option_type,
            "strike": strike,
            "expirationDate": expiration,
            "bid": bid,
            "ask": ask,
            "mark": _finite_number(quote.get("mark_price"), "mark_price"),
            "updatedAt": quote.get("updated_at"),
            "quoteSource": "robinhood_mcp",
            "source": "robinhood_mcp",
            "underlying": underlying,
            "otmPct": instrument["_otmPct"],
            "delta": quote.get("delta"),
            "volume": quote.get("volume"),
            "openInterest": quote.get("open_interest"),
        })
    candidates.sort(key=lambda item: (item["otmPct"], item["ask"]))
    return {
        "source": "robinhood_mcp",
        "timestamp": now.isoformat(),
        "underlying": underlying,
        "equityQuote": equity_quote,
        "underlyingQuote": underlying_quote,
        "chainId": chain_id,
        "expirationDate": expiration,
        "candidates": candidates,
    }


def _parse_occ_symbol(occ_symbol: str) -> tuple[str, str, str, float]:
    match = re.fullmatch(r"([A-Z]{1,6})(\d{6})([CP])(\d{8})", occ_symbol.upper())
    if not match:
        raise BridgeError("invalid_occ_symbol")
    root, expiry_code, cp_code, strike_code = match.groups()
    if root not in ALLOWED_UNDERLYINGS:
        raise BridgeError(f"unsupported_occ_root:{root}")
    expiration = datetime.strptime(expiry_code, "%y%m%d").date().isoformat()
    option_type = "call" if cp_code == "C" else "put"
    return root, expiration, option_type, int(strike_code) / 1000.0


def _tradable_instrument_rows(
    instruments: list[dict[str, Any]],
    *,
    chain_id: str,
    expiration: str,
    symbol: str,
    underlying: float,
    max_strike_distance: float,
) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for instrument in instruments:
        if (
            instrument.get("chain_id") != chain_id
            or str(instrument.get("chain_symbol", "")).upper() != symbol
            or instrument.get("expiration_date") != expiration
            or instrument.get("type") not in {"call", "put"}
            or instrument.get("state") != "active"
            or instrument.get("tradability") != "tradable"
        ):
            continue
        instrument_id = str(instrument.get("id", ""))
        if not instrument_id:
            continue
        strike = _finite_number(instrument.get("strike_price"), "spread_strike")
        if abs(strike - underlying) > max_strike_distance:
            continue
        selected[instrument_id] = {**instrument, "_strike": strike}
    return selected


def _spread_contract_from_quote(
    instrument: Mapping[str, Any], raw_quote: Mapping[str, Any], *, symbol: str, expiration: str,
) -> dict[str, Any] | None:
    instrument_id = str(instrument.get("id", ""))
    if str(raw_quote.get("instrument_id", "")) != instrument_id:
        raise BridgeError("spread_quote_instrument_identity_mismatch")
    bid = _finite_number(raw_quote.get("bid_price"), "spread_bid_price")
    ask = _finite_number(raw_quote.get("ask_price"), "spread_ask_price")
    if bid <= 0 or ask < bid:
        return None
    updated_at = raw_quote.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at:
        return None
    option_type = str(instrument.get("type", "")).lower()
    strike = _finite_number(instrument.get("_strike", instrument.get("strike_price")), "spread_strike")
    return {
        "symbol": symbol,
        "optionType": option_type,
        "type": option_type,
        "strike": strike,
        "expiry": expiration,
        "expirationDate": expiration,
        "bid": bid,
        "ask": ask,
        "mark": _finite_number(raw_quote.get("mark_price"), "spread_mark_price"),
        "instrumentId": instrument_id,
        "occSymbol": _occ_symbol(expiration, option_type, strike, symbol),
        "updatedAt": updated_at,
        "quoteSource": ENGINE_SOURCE,
        "source": ENGINE_SOURCE,
        "delta": raw_quote.get("delta"),
        "volume": raw_quote.get("volume"),
        "openInterest": raw_quote.get("open_interest"),
    }


def build_spread_candidates(
    *,
    symbol: str = "SPY",
    dte: int = 0,
    max_strike_distance: float = 5.0,
    caller=mcp_call,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Fetch one atomic, quote-backed SPY option-chain snapshot for spreads."""
    symbol = symbol.upper()
    if symbol != "SPY":
        raise BridgeError(f"unsupported_spread_symbol:{symbol}")
    if dte < 0 or dte > 45:
        raise BridgeError("invalid_spread_dte")
    max_strike_distance = _finite_number(max_strike_distance, "max_strike_distance")
    if max_strike_distance <= 0:
        raise BridgeError("invalid_spread_strike_distance")
    now = observed_at or datetime.now(timezone.utc)
    equity = caller("get_equity_quotes", {"symbols": [symbol]})
    equity_results = equity.get("data", {}).get("results", [])
    if len(equity_results) != 1 or equity_results[0].get("quote", {}).get("symbol") != symbol:
        raise BridgeError(f"spread_equity_identity_mismatch:{symbol}")
    equity_quote = equity_results[0].get("quote", {})
    underlying = _live_underlying_price(equity_quote, 0.0)
    if underlying <= 0:
        raise BridgeError("spread_underlying_not_executable")
    chain_payload = caller("get_option_chains", {"underlying_symbol": symbol})
    chains = chain_payload.get("data", {}).get("chains", [])
    if not isinstance(chains, list):
        raise BridgeError("spread_option_chains_not_list")
    chain_id, expiration = _target_expiration(
        chains, now, dte, symbol, allow_same_day=(dte == 0),
    )
    instruments = _collect_instruments(chain_id, expiration, None, caller)
    selected = _tradable_instrument_rows(
        instruments,
        chain_id=chain_id,
        expiration=expiration,
        symbol=symbol,
        underlying=underlying,
        max_strike_distance=max_strike_distance,
    )
    if not selected:
        raise BridgeError("spread_no_nearby_tradable_instruments")
    instrument_ids = sorted(selected, key=lambda key: abs(selected[key]["_strike"] - underlying))[:160]
    quotes_payload = caller("get_option_quotes", {"instrument_ids": instrument_ids})
    results = quotes_payload.get("data", {}).get("results", [])
    if not isinstance(results, list):
        raise BridgeError("spread_option_quotes_not_list")
    contracts: list[dict[str, Any]] = []
    observed_quotes: list[datetime] = []
    for result in results:
        raw_quote = result.get("quote", {}) if isinstance(result, dict) else {}
        instrument_id = str(raw_quote.get("instrument_id", ""))
        instrument = selected.get(instrument_id)
        if instrument is None:
            raise BridgeError("spread_quote_not_requested")
        contract = _spread_contract_from_quote(
            instrument, raw_quote, symbol=symbol, expiration=expiration,
        )
        if contract is None:
            continue
        try:
            observed = datetime.fromisoformat(contract["updatedAt"].replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            continue
        age = (now.astimezone(timezone.utc) - observed).total_seconds()
        if age < -2 or age > 15:
            continue
        observed_quotes.append(observed)
        contracts.append(contract)
    if len(contracts) < 4:
        raise BridgeError("spread_insufficient_fresh_executable_quotes")
    puts_by_strike = {item["strike"]: item for item in contracts if item["optionType"] == "put"}
    calls_by_strike = {item["strike"]: item for item in contracts if item["optionType"] == "call"}
    common_strikes = set(puts_by_strike).intersection(calls_by_strike)
    if common_strikes:
        atm_strike = min(common_strikes, key=lambda strike: abs(strike - underlying))
        expected_move = puts_by_strike[atm_strike]["ask"] + calls_by_strike[atm_strike]["ask"]
    else:
        nearest_put = min(puts_by_strike.values(), key=lambda item: abs(item["strike"] - underlying), default=None)
        nearest_call = min(calls_by_strike.values(), key=lambda item: abs(item["strike"] - underlying), default=None)
        if nearest_put is None or nearest_call is None:
            raise BridgeError("spread_expected_move_missing")
        expected_move = nearest_put["ask"] + nearest_call["ask"]
    if expected_move <= 0:
        raise BridgeError("spread_expected_move_invalid")
    timestamp = min(observed_quotes).isoformat() if observed_quotes else now.isoformat()
    return {
        "kind": "spread_candidates",
        "source": ENGINE_SOURCE,
        "timestamp": timestamp,
        "fetchedAt": now.isoformat(),
        "symbol": symbol,
        "dteTarget": dte,
        "underlying": underlying,
        "underlyingQuote": equity_quote,
        "chainId": chain_id,
        "expirationDate": expiration,
        "expectedMove": round(expected_move, 6),
        "expectedMoveSource": "atm_executable_ask_straddle",
        "contracts": contracts,
    }


def build_spread_position_quotes(
    legs: Mapping[str, Mapping[str, Any]],
    *,
    caller=mcp_call,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Fetch all four current leg books in one read-only quote request."""
    now = observed_at or datetime.now(timezone.utc)
    selected: dict[str, tuple[str, str, str, float]] = {}
    instrument_ids: list[str] = []
    for key in ("long_put", "short_put", "short_call", "long_call"):
        item = legs.get(key)
        if not isinstance(item, Mapping):
            raise BridgeError(f"spread_position_missing_leg:{key}")
        instrument_id = str(item.get("instrumentId", item.get("instrument_id", ""))).strip()
        occ_symbol = str(item.get("occSymbol", item.get("occ_symbol", ""))).strip().upper()
        if not instrument_id or not occ_symbol:
            raise BridgeError(f"spread_position_missing_identity:{key}")
        root, expiration, option_type, strike = _parse_occ_symbol(occ_symbol)
        if root != "SPY":
            raise BridgeError("spread_position_symbol_mismatch")
        selected[key] = (instrument_id, expiration, option_type, strike)
        instrument_ids.append(instrument_id)
    if len(set(instrument_ids)) != 4:
        raise BridgeError("spread_position_duplicate_instrument")
    payload = caller("get_option_quotes", {"instrument_ids": instrument_ids})
    results = payload.get("data", {}).get("results", [])
    if len(results) != 4:
        raise BridgeError("spread_position_quote_count_mismatch")
    by_id: dict[str, Mapping[str, Any]] = {}
    for result in results:
        raw = result.get("quote", {}) if isinstance(result, dict) else {}
        instrument_id = str(raw.get("instrument_id", ""))
        if instrument_id not in instrument_ids or instrument_id in by_id:
            raise BridgeError("spread_position_quote_identity_mismatch")
        by_id[instrument_id] = raw
    normalized: dict[str, dict[str, Any]] = {}
    timestamps: list[datetime] = []
    for key, (instrument_id, expiration, option_type, strike) in selected.items():
        raw = by_id.get(instrument_id)
        if raw is None:
            raise BridgeError("spread_position_missing_quote")
        instrument = {"id": instrument_id, "type": option_type, "_strike": strike}
        contract = _spread_contract_from_quote(
            instrument, raw, symbol="SPY", expiration=expiration,
        )
        if contract is None:
            raise BridgeError(f"spread_position_non_executable:{key}")
        try:
            observed = datetime.fromisoformat(contract["updatedAt"].replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError as exc:
            raise BridgeError(f"spread_position_invalid_timestamp:{key}") from exc
        age = (now.astimezone(timezone.utc) - observed).total_seconds()
        if age < -2 or age > 15:
            raise BridgeError(f"spread_position_stale_quote:{key}")
        timestamps.append(observed)
        normalized[key] = contract
    return {
        "kind": "spread_position_quotes",
        "source": ENGINE_SOURCE,
        "timestamp": min(timestamps).isoformat(),
        "fetchedAt": now.isoformat(),
        "symbol": "SPY",
        "legs": normalized,
    }


def build_option_quote(
    occ_symbol: str,
    *,
    caller=mcp_call,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Fetch one exact supported option quote after verifying instrument identity."""
    root, expiration, option_type, strike = _parse_occ_symbol(occ_symbol)
    now = observed_at or datetime.now(timezone.utc)
    chain_payload = caller("get_option_chains", {"underlying_symbol": root})
    chains = chain_payload.get("data", {}).get("chains", [])
    chain = next(
        (
            item for item in chains
            if isinstance(item, dict)
            and str(item.get("symbol", "")).upper() == root
            and expiration in item.get("expiration_dates", [])
        ),
        None,
    )
    if not chain or not chain.get("id"):
        raise BridgeError("occ_chain_or_expiration_not_found")
    chain_id = str(chain["id"])
    instruments = _collect_instruments(chain_id, expiration, option_type, caller)
    matches = [
        item for item in instruments
        if item.get("chain_id") == chain_id
        and str(item.get("chain_symbol", "")).upper() == root
        and item.get("expiration_date") == expiration
        and item.get("type") == option_type
        and item.get("state") == "active"
        and item.get("tradability") == "tradable"
        and abs(_finite_number(item.get("strike_price"), "strike_price") - strike) < 0.0005
    ]
    if len(matches) != 1:
        raise BridgeError(f"occ_instrument_match_count:{len(matches)}")
    instrument = matches[0]
    instrument_id = str(instrument.get("id", ""))
    if not instrument_id:
        raise BridgeError("occ_instrument_missing_id")
    quotes_payload = caller("get_option_quotes", {"instrument_ids": [instrument_id]})
    results = quotes_payload.get("data", {}).get("results", [])
    if len(results) != 1 or not isinstance(results[0], dict):
        raise BridgeError("occ_quote_result_count_mismatch")
    raw_quote = results[0].get("quote", {})
    if raw_quote.get("instrument_id") != instrument_id:
        raise BridgeError("occ_quote_instrument_identity_mismatch")
    quote = {
        "symbol": root,
        "occSymbol": occ_symbol.upper(),
        "instrumentId": instrument_id,
        "type": option_type,
        "strike": strike,
        "expirationDate": expiration,
        "bid": _finite_number(raw_quote.get("bid_price"), "bid_price"),
        "ask": _finite_number(raw_quote.get("ask_price"), "ask_price"),
        "mark": _finite_number(raw_quote.get("mark_price"), "mark_price"),
        "updatedAt": raw_quote.get("updated_at"),
        "quoteSource": "robinhood_mcp",
        "source": "robinhood_mcp",
        "delta": raw_quote.get("delta"),
        "volume": raw_quote.get("volume"),
        "openInterest": raw_quote.get("open_interest"),
    }
    return {
        "source": "robinhood_mcp",
        "timestamp": now.isoformat(),
        "quote": quote,
    }


def _normalized_contract(candidate: dict[str, Any]) -> dict[str, Any]:
    """Translate Robinhood bridge fields into the engine's fail-closed schema."""
    bid = _finite_number(candidate.get("bid"), "bid")
    ask = _finite_number(candidate.get("ask"), "ask")
    mark_raw = candidate.get("mark")
    mark = (bid + ask) / 2.0 if mark_raw is None else _finite_number(mark_raw, "mark")
    return {
        "symbol": str(candidate.get("symbol", "")).upper(),
        "occ_symbol": str(candidate.get("occSymbol", "")).upper(),
        "instrument_id": str(candidate.get("instrumentId", "")),
        "option_type": candidate.get("type"),
        "strike": _finite_number(candidate.get("strike"), "strike"),
        "expiry": candidate.get("expirationDate"),
        "bid": bid,
        "ask": ask,
        "mark": mark,
        "updated_at": candidate.get("updatedAt"),
        "quote_source": ENGINE_SOURCE,
        "delta": candidate.get("delta"),
        "volume": candidate.get("volume"),
        "open_interest": candidate.get("openInterest"),
    }


def build_option_quote_by_instrument(
    instrument_id: str,
    occ_symbol: str,
    *,
    caller=mcp_call,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Fetch one verified position quote without rebuilding its option chain."""
    instrument_id = str(instrument_id).strip()
    if not instrument_id:
        raise BridgeError("missing_option_instrument_id")
    root, expiration, option_type, strike = _parse_occ_symbol(occ_symbol)
    fetched_at = observed_at or datetime.now(timezone.utc)
    payload = caller("get_option_quotes", {"instrument_ids": [instrument_id]})
    results = payload.get("data", {}).get("results", [])
    if len(results) != 1 or not isinstance(results[0], dict):
        raise BridgeError("instrument_quote_result_count_mismatch")
    raw = results[0].get("quote", {})
    if str(raw.get("instrument_id", "")) != instrument_id:
        raise BridgeError("instrument_quote_identity_mismatch")
    timestamp = raw.get("updated_at")
    if not isinstance(timestamp, str) or not timestamp:
        raise BridgeError("instrument_quote_missing_updated_at")
    return {
        "kind": "option_quote",
        "source": ENGINE_SOURCE,
        "timestamp": timestamp,
        "fetched_at": fetched_at.isoformat(),
        "symbol": root,
        "occ_symbol": occ_symbol.upper(),
        "instrument_id": instrument_id,
        "option_type": option_type,
        "strike": strike,
        "expiry": expiration,
        "bid": _finite_number(raw.get("bid_price"), "bid_price"),
        "ask": _finite_number(raw.get("ask_price"), "ask_price"),
        "mark": _finite_number(raw.get("mark_price"), "mark_price"),
        "updated_at": timestamp,
        "quote_source": ENGINE_SOURCE,
        "delta": raw.get("delta"),
        "volume": raw.get("volume"),
        "open_interest": raw.get("open_interest"),
    }


def quote_equity(symbol: str) -> dict[str, Any]:
    symbol = symbol.upper()
    if symbol not in ALLOWED_UNDERLYINGS:
        raise BridgeError(f"unsupported_equity_symbol:{symbol}")
    payload = mcp_call("get_equity_quotes", {"symbols": [symbol]})
    results = payload.get("data", {}).get("results", [])
    if len(results) != 1 or results[0].get("quote", {}).get("symbol") != symbol:
        raise BridgeError(f"equity_quote_identity_mismatch:{symbol}")
    fetched_at = datetime.now(timezone.utc).isoformat()
    quote = results[0]["quote"]
    timestamp = _equity_quote_timestamp(quote, fetched_at)
    return {
        "kind": "equity_quote",
        "source": ENGINE_SOURCE,
        "timestamp": timestamp,
        "fetched_at": fetched_at,
        "symbol": symbol,
        "quote": quote,
    }


def quote_index(symbol: str, *, reference_underlying: float, dte: int = 1) -> dict[str, Any]:
    """Fetch a verified XSP index range through matched read-only option quotes."""
    return build_index_quote(
        symbol,
        reference_underlying=reference_underlying,
        dte=dte,
    )


def quote_option(occ_symbol: str, instrument_id: str | None = None) -> dict[str, Any]:
    if instrument_id:
        return build_option_quote_by_instrument(instrument_id, occ_symbol)
    raw = build_option_quote(occ_symbol)
    normalized = _normalized_contract(raw["quote"])
    timestamp = normalized.get("updated_at")
    if not isinstance(timestamp, str) or not timestamp:
        raise BridgeError("option_quote_missing_updated_at")
    return {
        "kind": "option_quote",
        "source": ENGINE_SOURCE,
        "timestamp": timestamp,
        "fetched_at": raw.get("timestamp"),
        **normalized,
    }


def quote_scalp_candidate(
    symbol: str,
    option_type: str,
    underlying: float,
    dte: int,
    min_debit: float,
    max_debit: float,
    max_otm_pct: float,
) -> dict[str, Any]:
    symbol = symbol.upper()
    if symbol not in ALLOWED_UNDERLYINGS:
        raise BridgeError(f"unsupported_scalp_symbol:{symbol}")
    raw = build_scalp_candidates(
        symbol=symbol,
        option_type=option_type,
        underlying=underlying,
        dte=dte,
        min_debit=min_debit,
        max_debit=max_debit,
        max_otm_pct=max_otm_pct,
    )
    candidates = raw.get("candidates", [])
    selected = _normalized_contract(candidates[0]) if candidates else None
    timestamp = selected.get("updated_at") if selected else raw.get("timestamp")
    underlying_quote = raw.get("underlyingQuote")
    underlying_quote_kind = (
        underlying_quote.get("kind")
        if isinstance(underlying_quote, dict)
        else "equity_quote"
    )
    payload = {
        "kind": "option_candidates",
        "source": ENGINE_SOURCE,
        "timestamp": timestamp,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "dte_target": dte,
        "underlying": raw.get("underlying"),
        "underlying_quote": underlying_quote,
        "underlying_quote_kind": underlying_quote_kind,
        "equity_quote": raw.get("equityQuote"),
        "equity_quote_timestamp": _equity_quote_timestamp(
            raw.get("equityQuote") or {}, str(raw.get("timestamp")),
        ),
        "chain_id": raw.get("chainId"),
        "expiration_date": raw.get("expirationDate"),
        "option_type": option_type,
        "candidate_count": len(candidates),
    }
    if selected is not None:
        payload[option_type] = selected
    return payload


def quote_spread_candidates(
    symbol: str = "SPY", *, dte: int = 0, max_strike_distance: float = 5.0,
) -> dict[str, Any]:
    return build_spread_candidates(
        symbol=symbol,
        dte=dte,
        max_strike_distance=max_strike_distance,
    )


def quote_spread_position(legs_json: str) -> dict[str, Any]:
    try:
        legs = json.loads(legs_json)
    except json.JSONDecodeError as exc:
        raise BridgeError("spread_position_invalid_json") from exc
    if not isinstance(legs, dict):
        raise BridgeError("spread_position_json_must_be_object")
    return build_spread_position_quotes(legs)


def save_latest_snapshot(payload: dict[str, Any]) -> Path:
    """Atomically replace one live quote file instead of creating one file per tick."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    identifier = (
        payload.get("occ_symbol")
        or payload.get("quote", {}).get("occSymbol")
        or payload.get("symbol")
        or "UNKNOWN"
    )
    safe_identifier = re.sub(r"[^A-Za-z0-9_.-]", "_", str(identifier))
    path = OUT_DIR / f"latest_{payload.get('kind', 'quote')}_{safe_identifier}.json"
    temp_path = path.with_suffix(path.suffix + ".tmp")
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    with temp_path.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)
    return path


def save_snapshot(payload: dict[str, Any]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    identifier = (
        payload.get("occ_symbol")
        or payload.get("quote", {}).get("occSymbol")
        or payload.get("symbol")
        or "UNKNOWN"
    )
    safe_identifier = re.sub(r"[^A-Za-z0-9_.-]", "_", str(identifier))
    path = OUT_DIR / f"{payload.get('kind', 'quote')}_{safe_identifier}_{stamp}_{os.getpid()}.json"
    temp_path = OUT_DIR / f".{path.name}.{os.getpid()}.tmp"
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(OUT_DIR, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp_path.unlink(missing_ok=True)
    return path


def _reexec_in_hermes_runtime() -> None:
    if os.environ.get("V7_RH_DIRECT_RUNTIME") == "1":
        return
    if not HERMES_PYTHON.is_file() or not os.access(HERMES_PYTHON, os.X_OK):
        raise BridgeError(f"hermes_python_unavailable:{HERMES_PYTHON}")
    environment = os.environ.copy()
    environment["V7_RH_DIRECT_RUNTIME"] = "1"
    os.execve(
        str(HERMES_PYTHON),
        [str(HERMES_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="V7 quote-only Robinhood MCP bridge")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--equity", metavar="SYMBOL", help="Fetch one equity quote")
    group.add_argument(
        "--option", "--option-quote", dest="option", metavar="OCC_SYMBOL",
        help="Fetch one specific option quote",
    )
    group.add_argument("--scalp-candidates", metavar="SYMBOL", help="Fetch 1DTE naked-option quote candidates")
    group.add_argument("--spread-candidates", metavar="SYMBOL", help="Fetch SPY iron-condor/butterfly chain quotes")
    group.add_argument("--spread-position", metavar="LEGS_JSON", help="Fetch all four current spread leg quotes")
    ap.add_argument("--instrument-id", help="Verified option instrument ID for direct position quotes")
    ap.add_argument("--option-type", choices=("call", "put"))
    ap.add_argument("--underlying", type=float)
    ap.add_argument("--dte", type=int, default=1)
    ap.add_argument("--min-debit", type=float, default=75.0)
    ap.add_argument("--max-debit", type=float, default=175.0)
    ap.add_argument("--max-otm-pct", type=float, default=0.60)
    ap.add_argument("--max-strike-distance", type=float, default=5.0)
    return ap


def main() -> int:
    _reexec_in_hermes_runtime()
    args = build_parser().parse_args()

    try:
        if args.equity:
            payload = quote_equity(args.equity)
        elif args.option:
            payload = quote_option(args.option, args.instrument_id)
        elif args.spread_candidates:
            if args.dte < 0:
                raise ValueError("spread candidate dte cannot be negative")
            payload = quote_spread_candidates(
                args.spread_candidates,
                dte=args.dte,
                max_strike_distance=args.max_strike_distance,
            )
        elif args.spread_position:
            payload = quote_spread_position(args.spread_position)
        else:
            if args.dte < 1:
                raise ValueError("Scalp candidate bridge only supports 1DTE+; 0DTE is disabled for $50 account")
            if args.option_type is None or args.underlying is None:
                raise BridgeError("scalp_candidates_require_option_type_and_underlying")
            payload = quote_scalp_candidate(
                args.scalp_candidates,
                args.option_type,
                args.underlying,
                args.dte,
                args.min_debit,
                args.max_debit,
                args.max_otm_pct,
            )
        payload["bridge_timestamp"] = datetime.now().astimezone().isoformat()
        path = save_snapshot(payload)
        print(json.dumps({"ok": True, "snapshot": str(path), "data": payload}, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
