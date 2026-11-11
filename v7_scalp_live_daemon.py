#!/usr/bin/env python3
"""Adaptive near-real-time runner for the V7.5 multi-symbol paper-only scalp engine.

One process owns scanning and position monitoring for all symbols in engine.SYMBOLS.
It runs at one-second cadence only during the configured entry window or while a
paper position is open, then relaxes automatically. Each symbol has its own state
file, trade log, and lock. This process has no order capability.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import v7_rh_quote_bridge as quote_bridge
import v7_scalp_paper as engine
import v7_scalp_photon_alert as photon
import v7_spy_credit_spreads_paper as spread_paper

ET = engine.ET
MAX_TRADES = engine.MAX_TRADES
ACTIVE_INTERVAL_SECONDS = 1.0
POSITION_INTERVAL_SECONDS = 1.0
PREMARKET_INTERVAL_SECONDS = 60.0
OPENING_WATCH_INTERVAL_SECONDS = 1.0
RELAXED_INTERVAL_SECONDS = 300.0
OFF_HOURS_INTERVAL_SECONDS = 300.0
POST_TRADE_RELAX_SECONDS = 30.0
POST_TRADE_COOLDOWN_SECONDS = 60.0
RELAX_AFTER_TRADES = 2
SPREAD_PAPER_ENABLED = True
SPREAD_PAPER_IDLE_INTERVAL_SECONDS = 60.0
SPREAD_PAPER_POSITION_INTERVAL_SECONDS = 5.0
# Keep the near-limit paper scan at the same one-second cadence as active scans.
NEAR_LIMIT_SCAN_INTERVAL_SECONDS = 1.0
MAX_FAILURE_INTERVAL_SECONDS = 30.0
BAR_REFRESH_SECONDS = 15.0
RUNTIME_STATUS_PATH = Path.home() / ".hermes/state/v7_scalp_live_runtime.json"
PID_PATH = Path.home() / ".hermes/state/v7_scalp_live_daemon.pid"
LOCK_PATH = Path.home() / ".hermes/state/v7_scalp_live_daemon.lock"
EVENT_LOG_PATH = Path.home() / ".hermes/state/v7_scalp_live_daemon.jsonl"
HERMES_ROOT = Path.home() / ".hermes/hermes-agent"
# The old uv-managed venv was removed, but the daemon must continue to use the
# interpreter that launchd actually started. The quote worker inherits this
# interpreter and the alert path adds HERMES_ROOT to PYTHONPATH below.
HERMES_PYTHON = Path(sys.executable)
QUOTE_WORKER_SCRIPT = Path(__file__).with_name("v7_rh_quote_worker.py")
QUOTE_WORKER_TIMEOUT_SECONDS = 30.0
WORKER_COLD_RESET_IDLE_GRACE_SECONDS = 120.0  # V7.5.4: stay warm through 60s post-exit cooldowns
# A transient gateway or MCP connection failure must not turn one XSP parity
# request into a lost scan. Retries always use a fresh worker so a poisoned
# authenticated session/pipe cannot be reused. The bridge remains read-only and
# fail-closed after the bounded retry budget.
QUOTE_WORKER_RETRY_ATTEMPTS = 3
QUOTE_WORKER_RETRY_BACKOFF_SECONDS = 0.5

_TRANSIENT_QUOTE_ERROR_MARKERS = (
    "mcp_dispatch_error:api error 429",
    "mcp_dispatch_error:api error 500",
    "mcp_dispatch_error:api error 502",
    "mcp_dispatch_error:api error 503",
    "mcp_dispatch_error:api error 504",
    "server returned an error response",
    "connection closed",
    "broken pipe",
    "quote_worker_closed_output",
    "quote_worker_exited:",
    "quote_worker_missing_pipe",
    "required_mcp_tool_not_discovered",
    "mcp_dispatch_failed:bridgeerror",
    # XSP parity is derived from live option books. Robinhood can briefly
    # return non-executable legs while the chain remains available; retry the
    # read-only request with a fresh worker, but still fail closed if all
    # bounded attempts fail.
    "index_parity_no_valid_executable_pair",
)


def is_transient_quote_error(error: BaseException) -> bool:
    """Return whether a read-only quote failure is safe to retry."""
    message = str(error).lower()
    return any(marker in message for marker in _TRANSIENT_QUOTE_ERROR_MARKERS)

# Freeze deployment identity when this process imports its sources. Calling the
# hash helpers on every heartbeat would read current disk bytes and could make a
# stale process appear to be running newly edited code before it is restarted.
PROCESS_ENGINE_SOURCE_SHA256 = engine.source_code_sha256()
PROCESS_DAEMON_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
PROCESS_CONFIG_SHA256 = engine.config_sha256()

_STOP = False


@dataclass(frozen=True)
class ModeDecision:
    mode: str
    interval_seconds: float
    action: str | None
    reason: str


def aggregate_runtime_mode(decisions: list[ModeDecision]) -> tuple[str, str]:
    """Summarize per-symbol decisions without manufacturing an unknown state.

    When every symbol is intentionally idle, the old aggregate default remained
    ``off_hours/unknown`` even though each symbol had a concrete reason such as
    ``outside_entry_window``. The health monitor correctly treated that pair as
    a daemon failure. Preserve the urgent-action priority, but carry an idle
    reason through when no symbol has work to do.
    """
    overall_mode = "off_hours"
    overall_reason = "unknown"
    idle_reasons: list[str] = []
    for decision in decisions:
        if decision.action is None:
            if decision.reason:
                idle_reasons.append(decision.reason)
            continue
        if overall_mode in ("off_hours", "relaxed") or decision.interval_seconds < 1.5:
            overall_mode = decision.mode
            overall_reason = decision.reason

    if overall_reason == "unknown" and idle_reasons:
        unique_reasons = set(idle_reasons)
        overall_reason = (
            next(iter(unique_reasons))
            if len(unique_reasons) == 1
            else "all_symbols_idle"
        )
    return overall_mode, overall_reason


def _hhmm(now: datetime) -> int:
    return now.astimezone(ET).hour * 100 + now.astimezone(ET).minute


def seconds_until_next_entry_start(now: datetime, symbol: str = engine.SYMBOL) -> float:
    current = now.astimezone(ET)
    if engine.is_index_option_symbol(symbol):
        # XSP trades overnight sessions; find the next venue-open boundary.
        weekday = current.weekday()
        tm = current.hour * 100 + current.minute
        # Saturday: sleep until Sunday 20:15 ET
        if weekday == 5:
            target = current.replace(hour=20, minute=15, second=0, microsecond=0) + timedelta(days=(6 - weekday))
            return max(1.0, (target - current).total_seconds())
        # Sunday before 20:15 or Friday after venue close: sleep to next open
        candidates = []
        # Same-day overnight open (20:20 entry window)
        if weekday in (6, 0, 1, 2, 3) and tm < 2020:
            candidates.append(current.replace(hour=20, minute=20, second=0, microsecond=0))
        # Regular-hours entry (09:35)
        if weekday < 5 and tm < 935:
            candidates.append(current.replace(hour=9, minute=35, second=0, microsecond=0))
        # Overnight open next day
        if weekday < 4 and tm >= 935:
            next_day = current + timedelta(days=1)
            candidates.append(next_day.replace(hour=20, minute=20, second=0, microsecond=0))
        # Sunday overnight after Friday close
        if weekday == 4 and tm >= 935:
            target = current + timedelta(days=(6 - weekday))
            candidates.append(target.replace(hour=20, minute=15, second=0, microsecond=0))
        if not candidates:
            return 60.0
        target = min(candidates)
        return max(1.0, (target - current).total_seconds())
    entry_hour, entry_minute = divmod(engine.ENTRY_START, 100)
    target = current.replace(
        hour=entry_hour, minute=entry_minute, second=0, microsecond=0,
    )
    if current >= target:
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return max(1.0, (target - current).total_seconds())


def choose_mode(
    state: dict[str, Any], now: datetime, symbol: str = engine.SYMBOL,
) -> ModeDecision:
    current = now.astimezone(ET)
    trade_limit = engine.trade_limit_for_symbol(symbol)
    if state.get("activePositions"):
        return ModeDecision("position", POSITION_INTERVAL_SECONDS, "monitor", "paper_position_open")
    if symbol == "SPY" and SPREAD_PAPER_ENABLED:
        try:
            spread_state = spread_paper.load_state()
        except Exception:
            return ModeDecision("spread_guard", RELAXED_INTERVAL_SECONDS, None, "spread_state_unavailable")
        if spread_state.get("activePosition") is not None:
            return ModeDecision(
                "spread_paper_position", SPREAD_PAPER_POSITION_INTERVAL_SECONDS,
                None, "spread_paper_position_open",
            )
    if trade_limit is not None and int(state.get("dailyTrades", 0)) >= trade_limit:
        return ModeDecision("relaxed", RELAXED_INTERVAL_SECONDS, None, "daily_trade_limit_reached")
    last_exit_at = state.get("lastExitAt")
    if isinstance(last_exit_at, str):
        try:
            elapsed = (now.astimezone(ET) - datetime.fromisoformat(last_exit_at).astimezone(ET)).total_seconds()
            if 0 <= elapsed < POST_TRADE_COOLDOWN_SECONDS:
                return ModeDecision("post_trade_relaxed", POST_TRADE_RELAX_SECONDS, None, "post_trade_cooldown")
        except ValueError:
            pass
    if engine.is_market_holiday(current.date()):
        return ModeDecision(
            "off_hours", OFF_HOURS_INTERVAL_SECONDS, None, "market_holiday"
        )
    if engine.is_index_option_symbol(symbol):
        if not engine.xsp_market_open(current):
            return ModeDecision(
                "off_hours",
                min(OFF_HOURS_INTERVAL_SECONDS, seconds_until_next_entry_start(current, symbol=symbol)),
                None, "market_closed",
            )
        if not engine.entry_window_open(current, symbol):
            return ModeDecision(
                "off_hours",
                min(OFF_HOURS_INTERVAL_SECONDS, seconds_until_next_entry_start(current, symbol=symbol)),
                None, "outside_entry_window",
            )
        tm = _hhmm(current)
        if engine.LUNCH_START <= tm <= engine.LUNCH_END:
            return ModeDecision("relaxed", 30.0, None, "lunch_chop_window")
        if trade_limit is not None and int(state.get("dailyTrades", 0)) >= RELAX_AFTER_TRADES:
            return ModeDecision(
                "near_limit_scan", NEAR_LIMIT_SCAN_INTERVAL_SECONDS, "scan",
                "two_trades_completed",
            )
        return ModeDecision("active_scan", ACTIVE_INTERVAL_SECONDS, "scan", "index_session_entry_window")
    if current.weekday() >= 5:
        return ModeDecision(
            "off_hours",
            min(OFF_HOURS_INTERVAL_SECONDS, seconds_until_next_entry_start(current)),
            None, "market_weekend",
        )
    tm = _hhmm(current)
    if engine.PREMARKET_WATCH_START <= tm < engine.MARKET_OPEN:
        return ModeDecision(
            "premarket_watch", PREMARKET_INTERVAL_SECONDS, "premarket",
            "premarket_price_target_tracking",
        )
    if engine.MARKET_OPEN <= tm < engine.ENTRY_START:
        return ModeDecision(
            "opening_watch", OPENING_WATCH_INTERVAL_SECONDS, "premarket",
            "opening_price_target_confirmation",
        )
    if not engine.ENTRY_START <= tm <= engine.ENTRY_END:
        return ModeDecision(
            "off_hours",
            min(OFF_HOURS_INTERVAL_SECONDS, seconds_until_next_entry_start(current)),
            None, "outside_entry_window",
        )
    if engine.LUNCH_START <= tm <= engine.LUNCH_END:
        return ModeDecision("relaxed", 30.0, None, "lunch_chop_window")
    if trade_limit is not None and int(state.get("dailyTrades", 0)) >= RELAX_AFTER_TRADES:
        return ModeDecision(
            "near_limit_scan", NEAR_LIMIT_SCAN_INTERVAL_SECONDS, "scan",
            "two_trades_completed",
        )
    return ModeDecision("active_scan", ACTIVE_INTERVAL_SECONDS, "scan", "entry_window")


def worker_cold_reset_due(idle_seconds: float, cold_reset_done: bool) -> bool:
    """V7.5.4: only release the quote worker after sustained idle.

    Post-exit cooldowns (60s) and other short idle gaps no longer pay the
    ~2s quote-worker cold start on the first re-entry scan. The existing
    any_position_open guard in run_loop still prevents resets while any
    symbol (or the spread module) holds a position.
    """
    return (not cold_reset_done) and idle_seconds >= WORKER_COLD_RESET_IDLE_GRACE_SECONDS


def failure_interval(base_interval: float, consecutive_failures: int) -> float:
    return min(MAX_FAILURE_INTERVAL_SECONDS, max(base_interval, base_interval * (2 ** consecutive_failures)))


def should_emit(result: dict[str, Any] | None, exit_code: int) -> bool:
    action = str((result or {}).get("action", ""))
    return exit_code != 0 or action in {"PAPER_ENTRY", "PAPER_EXIT", "ERROR"}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_runtime_status(payload: dict[str, Any]) -> None:
    record = {
        "engineVersion": engine.ENGINE_VERSION,
        "strategyId": engine.STRATEGY_ID,
        "releaseManifestSha256": engine.RELEASE_MANIFEST_SHA256,
        "engineSourceSha256": PROCESS_ENGINE_SOURCE_SHA256,
        "daemonSourceSha256": PROCESS_DAEMON_SOURCE_SHA256,
        "configSha256": PROCESS_CONFIG_SHA256,
        "mode": "paper_only",
        "liveOrdersEnabled": False,
        "updatedAt": datetime.now(ET).isoformat(),
        **payload,
    }
    _atomic_write(RUNTIME_STATUS_PATH, record)


def _append_event(payload: dict[str, Any]) -> None:
    EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(ET).isoformat(),
        **payload,
        "engineVersion": engine.ENGINE_VERSION,
        "strategyId": engine.STRATEGY_ID,
        "releaseManifestSha256": engine.RELEASE_MANIFEST_SHA256,
        "sourceCodeSha256": PROCESS_ENGINE_SOURCE_SHA256,
        "configSha256": PROCESS_CONFIG_SHA256,
    }
    with EVENT_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


ALERT_TARGETS = ("telegram", "photon")


def deliver_alert(message: str) -> None:
    """Deliver a qualified entry/exit/error without an LLM or cron process."""
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(HERMES_ROOT) + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )
    failures: list[str] = []
    for target in ALERT_TARGETS:
        completed = subprocess.run(
            [str(HERMES_PYTHON), "-m", "hermes_cli.main", "send",
             "--to", target, "--quiet", message],
            capture_output=True, text=True, timeout=30, check=False,
            env=environment,
        )
        if completed.returncode != 0:
            detail = completed.stderr[-500:] or completed.stdout[-500:]
            failures.append(f"{target}:{detail.strip()}")
    if failures:
        raise RuntimeError("alert_delivery_failed:" + "; ".join(failures))


class QuoteWorkerClient:
    """Line-delimited JSON client for the isolated read-only quote process."""

    def __init__(self) -> None:
        environment = os.environ.copy()
        environment["V7_RH_DIRECT_RUNTIME"] = "1"
        self.process = subprocess.Popen(
            [str(HERMES_PYTHON), str(QUOTE_WORKER_SCRIPT)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=environment,
        )

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        worker = self.process
        if worker.stdin is None or worker.stdout is None:
            raise RuntimeError("quote_worker_missing_pipe")
        worker.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        worker.stdin.flush()
        started = time.monotonic()
        while time.monotonic() - started < QUOTE_WORKER_TIMEOUT_SECONDS:
            import select
            ready, _, _ = select.select([worker.stdout], [], [], 0.25)
            if ready:
                line = worker.stdout.readline()
                if not line:
                    raise RuntimeError("quote_worker_closed_output")
                response = json.loads(line)
                if not response.get("ok"):
                    raise RuntimeError(str(response.get("error", "quote_worker_error")))
                data = response.get("data")
                if not isinstance(data, dict):
                    raise RuntimeError("quote_worker_missing_data")
                return data
            if worker.poll() is not None:
                detail = worker.stderr.read()[-500:] if worker.stderr else ""
                raise RuntimeError(f"quote_worker_exited:{worker.returncode}:{detail}")
        raise RuntimeError("quote_worker_timeout")

    def close(self) -> None:
        worker = self.process
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
        for stream in (worker.stdin, worker.stdout, worker.stderr):
            if stream is not None:
                stream.close()


def _direct_quote_request(request: dict[str, Any]) -> dict[str, Any]:
    """Fail-safe read-only fallback when the isolated worker times out.

    The fallback reaches only the same quote-read helpers already used by the
    worker. It has no order, account, position, or portfolio capability.
    """
    operation = request.get("op")
    if operation == "equity":
        return quote_bridge.quote_equity(str(request.get("symbol", "SPY")))
    if operation == "index":
        return quote_bridge.quote_index(
            str(request.get("symbol", "")),
            reference_underlying=float(request["reference_underlying"]),
            dte=int(request.get("dte", 1)),
        )
    if operation == "scalp_candidate":
        return quote_bridge.quote_scalp_candidate(
            str(request.get("symbol", "SPY")),
            str(request["option_type"]),
            float(request["underlying"]),
            int(request["dte"]),
            float(request["min_debit"]),
            float(request["max_debit"]),
            float(request["max_otm_pct"]),
        )
    if operation == "spread_candidates":
        return quote_bridge.quote_spread_candidates(
            str(request.get("symbol", "SPY")),
            dte=int(request.get("dte", 1)),
            max_strike_distance=float(request.get("max_strike_distance", 5.0)),
        )
    if operation == "spread_position":
        legs = request.get("legs")
        if not isinstance(legs, dict):
            raise RuntimeError("spread_position_requires_legs")
        return quote_bridge.build_spread_position_quotes(legs)
    if operation == "option":
        return quote_bridge.quote_option(
            str(request["occ_symbol"]),
            str(request["instrument_id"]) if request.get("instrument_id") else None,
        )
    raise RuntimeError(f"unsupported_quote_fallback_operation:{operation}")


class LiveRuntime:
    """Persistent authenticated bridge plus low-churn completed-bar cache."""

    def __init__(self) -> None:
        self._original_fetch_bars = engine.fetch_bars
        self._original_bridge = engine.bridge
        self._original_refresh_option_quote = engine.refresh_option_quote
        self._original_fetch_live_equity_quote = engine.fetch_live_equity_quote
        self._bar_cache: dict[str, list[dict[str, Any]]] = {}  # symbol -> cached bars
        self._bar_cache_at: dict[str, float] = {}  # symbol -> monotonic timestamp
        self.last_equity_quote: dict[str, dict[str, Any]] = {}  # symbol -> quote
        self._quote_worker: QuoteWorkerClient | None = None

    def install(self) -> None:
        engine.fetch_bars = self.fetch_bars  # type: ignore[assignment]
        engine.bridge = self.bridge  # type: ignore[assignment]
        engine.refresh_option_quote = self.refresh_option_quote  # type: ignore[assignment]
        engine.fetch_live_equity_quote = self.fetch_live_equity_quote  # type: ignore[assignment]

    def restore(self) -> None:
        engine.fetch_bars = self._original_fetch_bars  # type: ignore[assignment]
        engine.bridge = self._original_bridge  # type: ignore[assignment]
        engine.refresh_option_quote = self._original_refresh_option_quote  # type: ignore[assignment]
        engine.fetch_live_equity_quote = self._original_fetch_live_equity_quote  # type: ignore[assignment]
        self._close_quote_worker()

    def _close_quote_worker(self) -> None:
        worker = self._quote_worker
        self._quote_worker = None
        if worker is None:
            return
        worker.close()

    def _start_quote_worker(self) -> QuoteWorkerClient:
        if self._quote_worker is not None and self._quote_worker.is_alive():
            return self._quote_worker
        self._quote_worker = QuoteWorkerClient()
        return self._quote_worker

    def _worker_call(self, request: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(QUOTE_WORKER_RETRY_ATTEMPTS):
            worker = self._start_quote_worker()
            try:
                return worker.call(request)
            except Exception as exc:
                last_error = exc
                # Always discard the worker after a failed request. A connection
                # that returned a gateway error or closed its MCP pipe is not a
                # trusted session for the next attempt.
                self._close_quote_worker()
                if str(exc) == "quote_worker_timeout":
                    try:
                        return _direct_quote_request(request)
                    except Exception as fallback_exc:
                        raise RuntimeError(
                            f"quote_worker_timeout;direct_fallback_failed:{fallback_exc}"
                        ) from fallback_exc
                if (
                    not is_transient_quote_error(exc)
                    or attempt >= QUOTE_WORKER_RETRY_ATTEMPTS - 1
                ):
                    raise
                time.sleep(QUOTE_WORKER_RETRY_BACKOFF_SECONDS * (2 ** attempt))
        # The loop either returns or re-raises, but retain a typed guard if the
        # retry constants are changed incorrectly in a future edit.
        if last_error is not None:
            raise last_error
        raise RuntimeError("quote_worker_retry_exhausted")

    def cold_reset(self) -> None:
        """Release cached bars and force MCP rediscovery after long idle periods."""
        self._close_quote_worker()
        self._bar_cache = {}
        self._bar_cache_at = {}
        self.last_equity_quote = {}


    def _index_signal_bars(
        self, symbol: str, days: int, *, force: bool,
    ) -> list[dict[str, Any]]:
        """Fetch/cache the approved continuous indicator feed for an index option."""
        feed = engine.signal_feed_for_symbol(symbol)
        cache_key = f"__index_signal__:{symbol}:{days}:{feed}"
        now_mono = time.monotonic()
        cached_at = self._bar_cache_at.get(cache_key, 0.0)
        if force or cache_key not in self._bar_cache or now_mono - cached_at >= BAR_REFRESH_SECONDS:
            self._bar_cache[cache_key] = self._original_fetch_bars(feed, days, force=force)
            self._bar_cache_at[cache_key] = now_mono
        return self._bar_cache[cache_key]

    def _index_reference_bars(
        self, symbol: str, days: int, *, force: bool,
    ) -> list[dict[str, Any]]:
        """Fetch/cache the session-appropriate cash/futures parity reference feed."""
        feed = engine.index_reference_feed_for_symbol(symbol)
        cache_key = f"__index_reference__:{symbol}:{days}:{feed}"
        now_mono = time.monotonic()
        cached_at = self._bar_cache_at.get(cache_key, 0.0)
        if force or cache_key not in self._bar_cache or now_mono - cached_at >= BAR_REFRESH_SECONDS:
            self._bar_cache[cache_key] = self._original_fetch_bars(feed, days, force=force)
            self._bar_cache_at[cache_key] = now_mono
        return self._bar_cache[cache_key]

    def _index_reference_underlying(self, symbol: str) -> float:
        raw_bars = self._index_reference_bars(symbol, 5, force=False)
        if not raw_bars:
            raise RuntimeError("xsp_signal_feed_unavailable")
        try:
            last_close = float(raw_bars[-1]["close"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("xsp_signal_feed_invalid") from exc
        if last_close <= 0:
            raise RuntimeError("xsp_signal_feed_invalid")
        return round(last_close * engine.signal_price_scale_for_symbol(symbol), 6)

    def _scaled_index_signal_bars(
        self, symbol: str, days: int, *, force: bool,
    ) -> list[dict[str, Any]]:
        raw_bars = self._index_signal_bars(symbol, days, force=force)
        if not raw_bars:
            raise RuntimeError("xsp_signal_feed_unavailable")
        quote = self.fetch_live_equity_quote(symbol)
        try:
            spot = float(quote["mid"])
            feed_last_close = float(raw_bars[-1]["close"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("xsp_index_quote_invalid") from exc
        if spot <= 0 or feed_last_close <= 0:
            raise RuntimeError("xsp_index_quote_invalid")
        scale = spot / feed_last_close
        feed = engine.signal_feed_for_symbol(symbol)
        scaled: list[dict[str, Any]] = []
        for raw in raw_bars:
            item = dict(raw)
            try:
                for field in ("open", "high", "low", "close"):
                    item[field] = round(float(raw[field]) * scale, 6)
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("xsp_signal_feed_invalid") from exc
            item["signalFeed"] = feed
            item["signalPriceScale"] = round(scale, 10)
            scaled.append(item)
        return scaled

    def fetch_bars(self, symbol: str = engine.SYMBOL, days: int = 5, *, force: bool = False):
        now_mono = time.monotonic()
        cached_at = self._bar_cache_at.get(symbol, 0.0)
        if force or symbol not in self._bar_cache or now_mono - cached_at >= BAR_REFRESH_SECONDS:
            if engine.is_index_option_symbol(symbol):
                self._bar_cache[symbol] = self._scaled_index_signal_bars(symbol, days, force=force)
            else:
                self._bar_cache[symbol] = self._original_fetch_bars(symbol, days, force=force)
            self._bar_cache_at[symbol] = now_mono
        return self._bar_cache[symbol]

    def poll_equity(self, symbol: str = engine.SYMBOL) -> dict[str, Any]:
        if engine.is_index_option_symbol(symbol):
            payload = self._worker_call({
                "op": "index", "symbol": symbol,
                "reference_underlying": self._index_reference_underlying(symbol),
            })
        else:
            payload = self._worker_call({"op": "equity", "symbol": symbol})
        if payload.get("kind") != engine.underlying_quote_kind_for_symbol(symbol):
            raise RuntimeError("unexpected_live_underlying_quote_kind")
        quote = payload.get("quote", {})
        bid = float(quote["bid_price"])
        ask = float(quote["ask_price"])
        if bid <= 0 or ask < bid:
            raise RuntimeError("invalid_live_equity_quote")
        payload["mid"] = round((bid + ask) / 2.0, 4)
        payload["bid"] = bid
        payload["ask"] = ask
        payload["_snapshotPath"] = str(quote_bridge.save_latest_snapshot(payload))
        self.last_equity_quote[symbol] = payload
        return payload

    def fetch_live_equity_quote(self, symbol: str = engine.SYMBOL) -> dict[str, Any]:
        cached = self.last_equity_quote.get(symbol)
        if cached is None:
            return self.poll_equity(symbol)
        fetched_at = cached.get("fetched_at")
        try:
            age = (
                datetime.now(ET)
                - datetime.fromisoformat(str(fetched_at)).astimezone(ET)
            ).total_seconds()
        except ValueError:
            age = ACTIVE_INTERVAL_SECONDS + 1
        return self.poll_equity(symbol) if age > ACTIVE_INTERVAL_SECONDS else cached

    def refresh_option_quote(self, *args: str) -> dict[str, Any]:
        """Retry a stale exit quote through a newly authenticated worker."""
        argv = list(args)
        if "--option" in argv or "--option-quote" in argv:
            self._close_quote_worker()
        return self.bridge(*args)

    def bridge(self, *args: str) -> dict[str, Any]:
        argv = list(args)
        if "--spread-candidates" in argv:
            symbol = argv[argv.index("--spread-candidates") + 1]
            dte = int(argv[argv.index("--dte") + 1]) if "--dte" in argv else spread_paper.DEFAULT_DTE
            max_strike_distance = (
                float(argv[argv.index("--max-strike-distance") + 1])
                if "--max-strike-distance" in argv else spread_paper.BRIDGE_MAX_STRIKE_DISTANCE
            )
            payload = self._worker_call({
                "op": "spread_candidates", "symbol": symbol, "dte": dte,
                "max_strike_distance": max_strike_distance,
            })
            snapshot = quote_bridge.save_snapshot(payload)
        elif "--spread-position" in argv:
            encoded_legs = argv[argv.index("--spread-position") + 1]
            legs = json.loads(encoded_legs)
            if not isinstance(legs, dict):
                raise RuntimeError("spread_position_legs_not_object")
            payload = self._worker_call({"op": "spread_position", "legs": legs})
            snapshot = quote_bridge.save_latest_snapshot(payload)
        elif "--scalp-candidates" in argv:
            symbol = argv[argv.index("--scalp-candidates") + 1]
            option_type = argv[argv.index("--option-type") + 1]
            underlying = float(argv[argv.index("--underlying") + 1])
            dte = int(argv[argv.index("--dte") + 1])
            min_debit = float(argv[argv.index("--min-debit") + 1])
            max_debit = float(argv[argv.index("--max-debit") + 1])
            max_otm_pct = float(argv[argv.index("--max-otm-pct") + 1])
            payload = self._worker_call({
                "op": "scalp_candidate", "symbol": symbol,
                "option_type": option_type, "underlying": underlying,
                "dte": dte, "min_debit": min_debit, "max_debit": max_debit,
                "max_otm_pct": max_otm_pct,
            })
            snapshot = quote_bridge.save_snapshot(payload)
        elif "--option" in argv or "--option-quote" in argv:
            flag = "--option" if "--option" in argv else "--option-quote"
            occ_symbol = argv[argv.index(flag) + 1]
            instrument_id = argv[argv.index("--instrument-id") + 1] if "--instrument-id" in argv else None
            payload = self._worker_call({
                "op": "option", "occ_symbol": occ_symbol,
                "instrument_id": instrument_id,
            })
            snapshot = quote_bridge.save_latest_snapshot(payload)
        elif "--equity" in argv:
            symbol = argv[argv.index("--equity") + 1]
            payload = self._worker_call({"op": "equity", "symbol": symbol})
            snapshot = quote_bridge.save_latest_snapshot(payload)
        else:
            raise RuntimeError(f"unsupported_read_only_bridge_command:{argv}")
        data = dict(payload)
        data["_snapshotPath"] = str(snapshot)
        return data


def _capture_engine_call(action: str, symbol: str = engine.SYMBOL) -> tuple[int, dict[str, Any] | None, str]:
    output = io.StringIO()
    callback: Callable[..., int] = engine.scan if action == "scan" else engine.monitor
    with contextlib.redirect_stdout(output):
        exit_code = callback(symbol)
    raw = output.getvalue().strip()
    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = None
    return exit_code, parsed, raw


def monitor_after_entry(
    result: dict[str, Any] | None,
    symbol: str = engine.SYMBOL,
) -> tuple[int, dict[str, Any] | None, str]:
    """Run the first position monitor in the same daemon cycle as an entry.

    The normal one-second position cadence remains the safety net. This
    same-cycle follow-up removes the avoidable gap between recording a paper
    entry and the first exact-instrument quote check.
    """
    if not isinstance(result, dict) or result.get("action") != "PAPER_ENTRY":
        return 0, None, ""
    return _capture_engine_call("monitor", symbol)


def _capture_premarket_call(
    runtime: LiveRuntime, symbol: str = engine.SYMBOL,
) -> tuple[int, dict[str, Any], str]:
    """Persist the live premarket target; this path has no option/order capability."""
    observed_at = datetime.now(ET)
    bars = runtime.fetch_bars(symbol)
    current_price = None
    quote_error = None
    try:
        current_price = float(runtime.poll_equity(symbol)["mid"])
    except Exception as exc:
        # The extended-hours bar remains usable if the brokerage quote is briefly
        # unavailable. Surface the fallback in the audit payload instead of idling.
        quote_error = str(exc)
    context = engine.track_premarket_price(
        symbol,
        now=observed_at,
        current_price=current_price,
        bars=bars,
    )
    result = {
        "action": "PREMARKET_WATCH",
        "symbol": symbol,
        "target": context,
        "quoteFallback": quote_error,
    }
    return 0, result, json.dumps(result, separators=(",", ":"))


def _scan_fingerprint(result: dict[str, Any] | None, exit_code: int, symbol: str = engine.SYMBOL) -> tuple[Any, ...]:
    result = result or {}
    signal_data = result.get("signal") if isinstance(result.get("signal"), dict) else {}
    return (
        symbol, exit_code, result.get("action"), result.get("reason"),
        signal_data.get("signalBarStart"), signal_data.get("direction"),
        signal_data.get("score"), signal_data.get("relativeVolume"),
    )


def _read_state(symbol: str = engine.SYMBOL) -> dict[str, Any]:
    with engine.exclusive_state_lock(symbol):
        state = engine.load_state(symbol)
        metadata_changed = bool(state.pop("_stateMetadataNeedsPersist", False))
        previous_reset_date = state.get("lastResetDate")
        engine.reset_daily(state, symbol=symbol)
        if state.get("lastResetDate") != previous_reset_date or metadata_changed:
            engine.save_state(state, symbol)
        return state


def _run_spread_paper_phase(runtime: LiveRuntime, now: datetime) -> dict[str, Any]:
    """Run the isolated SPY spread phase without competing with V7 SPY."""
    if not SPREAD_PAPER_ENABLED:
        return {"action": "disabled", "strategyId": spread_paper.STRATEGY_ID}
    try:
        v7_spy_state = _read_state("SPY")
    except Exception as exc:
        return {"action": "skip", "reason": f"v7_spy_state_unavailable:{type(exc).__name__}"}
    if v7_spy_state.get("activePositions"):
        spread_state = spread_paper.load_state()
        spread_state["lastSkipReason"] = "v7_spy_position_open"
        spread_paper.save_state(spread_state)
        return {"action": "skip", "reason": "v7_spy_position_open"}
    return spread_paper.run_once(
        now=now,
        dte=spread_paper.DEFAULT_DTE,
        bridge_fn=runtime.bridge,
    )


def symbol_iteration_order(symbol_states: dict[str, dict[str, Any]]) -> list[str]:
    """Process symbols with open positions before idle symbols, stably."""
    return sorted(
        engine.SYMBOLS,
        key=lambda symbol: 0 if symbol_states.get(symbol, {}).get("activePositions") else 1,
    )


def _initialize_state() -> None:
    """Persist release metadata once at daemon startup for each symbol."""
    for symbol in engine.SYMBOLS:
        with engine.exclusive_state_lock(symbol):
            state = engine.load_state(symbol)
            engine.reset_daily(state, symbol=symbol)
            engine.save_state(state, symbol)


def _handle_signal(_signum, _frame) -> None:
    global _STOP
    _STOP = True


def _sleep_until(deadline: float) -> None:
    while not _STOP:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.25))


def run_loop(*, max_iterations: int | None = None) -> int:
    import fcntl

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"action": "ERROR", "reason": "daemon_already_running"}))
            return 2
        _atomic_write(PID_PATH, {"pid": os.getpid(), "startedAt": datetime.now(ET).isoformat()})
        runtime = LiveRuntime()
        runtime.install()
        _initialize_state()
        failures = 0
        iteration = 0
        last_mode: dict[str, str | None] = {}  # symbol -> last mode
        last_scan_fingerprint: dict[str, tuple[Any, ...]] = {}  # symbol -> fingerprint
        cold_reset_done = False
        idle_since: float | None = None  # V7.5.4: quote-worker idle timer (grace before cold reset)
        spread_last_tick = 0.0
        spread_last_result: dict[str, Any] | None = None
        try:
            while not _STOP and (max_iterations is None or iteration < max_iterations):
                iteration += 1
                tick_started = time.monotonic()
                tick_max_exit_code = 0
                tick_result = None
                tick_raw = ""
                tick_had_failure = False
                # Process open positions first, so a slow idle-symbol scan cannot
                # delay the monitor path for another symbol.
                priority_states: dict[str, dict[str, Any]] = {}
                for _symbol in engine.SYMBOLS:
                    try:
                        priority_states[_symbol] = _read_state(_symbol)
                    except Exception:
                        priority_states[_symbol] = {}
                for symbol in symbol_iteration_order(priority_states):
                    try:
                        state = priority_states.get(symbol) or _read_state(symbol)
                        decision = choose_mode(state, datetime.now(ET), symbol=symbol)
                        result = None
                        raw = ""
                        exit_code = 0
                        if decision.action:
                            if decision.action == "premarket":
                                exit_code, result, raw = _capture_premarket_call(runtime, symbol)
                            else:
                                if decision.action == "scan":
                                    runtime.poll_equity(symbol)
                                exit_code, result, raw = _capture_engine_call(decision.action, symbol)
                        if decision.action == "scan":
                            fingerprint = _scan_fingerprint(result, exit_code, symbol)
                            if fingerprint != last_scan_fingerprint.get(symbol):
                                photon._audit(raw, result, exit_code, symbol=symbol)
                                last_scan_fingerprint[symbol] = fingerprint
                        if should_emit(result, exit_code):
                            emit_raw = raw
                            if result and result.get("action") == "PAPER_ENTRY":
                                passed, reasons = photon.alert_quality_gate(result.get("position") or {})
                                result["photonAlert"] = {"passed": passed, "reasons": reasons}
                                if passed:
                                    emit_raw = photon.format_entry_alert(result["position"])
                                else:
                                    _append_event({"event": "ENTRY_ALERT_REJECTED", "symbol": symbol, "reasons": reasons})
                                    emit_raw = ""
                            if emit_raw:
                                print(emit_raw, flush=True)
                                deliver_alert(emit_raw)
                        followup_code, followup_result, followup_raw = monitor_after_entry(result, symbol)
                        if followup_code != 0:
                            tick_had_failure = True
                            tick_max_exit_code = max(tick_max_exit_code, followup_code)
                        if followup_result is not None and tick_result is None:
                            tick_result = followup_result
                        if followup_raw and not tick_raw:
                            tick_raw = followup_raw
                        if should_emit(followup_result, followup_code) and followup_raw:
                            print(followup_raw, flush=True)
                            deliver_alert(followup_raw)
                        if decision.mode != last_mode.get(symbol):
                            _append_event({"event": "MODE_CHANGE", "symbol": symbol, **asdict(decision)})
                            last_mode[symbol] = decision.mode
                        # cold_reset kills the quote worker — only safe when NO symbol
                        # has an active position. Defer until after symbols_status is built.
                        pass
                        if exit_code != 0:
                            tick_had_failure = True
                            tick_max_exit_code = max(tick_max_exit_code, exit_code)
                        if tick_result is None and result is not None:
                            tick_result = result
                        if tick_raw == "" and raw:
                            tick_raw = raw
                    except Exception as exc:
                        tick_had_failure = True
                        _append_event({"event": "SYMBOL_ITERATION_ERROR", "symbol": symbol, "error": str(exc), "failures": failures + 1})
                        tick_max_exit_code = max(tick_max_exit_code, 1)
                if SPREAD_PAPER_ENABLED:
                    try:
                        spread_runtime_state = spread_paper.load_state()
                        spread_interval = (
                            SPREAD_PAPER_POSITION_INTERVAL_SECONDS
                            if spread_runtime_state.get("activePosition") is not None
                            else SPREAD_PAPER_IDLE_INTERVAL_SECONDS
                        )
                        if time.monotonic() - spread_last_tick >= spread_interval:
                            spread_last_tick = time.monotonic()
                            spread_last_result = _run_spread_paper_phase(runtime, datetime.now(ET))
                            if spread_last_result.get("action") in {"paper_entry", "paper_exit", "quote_failure"}:
                                print(json.dumps({
                                    "action": "SPREAD_" + str(spread_last_result["action"]).upper(),
                                    "strategyId": spread_paper.STRATEGY_ID,
                                    "reason": spread_last_result.get("reason"),
                                    "pnl": spread_last_result.get("pnl"),
                                    "structure": (spread_last_result.get("position") or {}).get("structure"),
                                }, separators=(",", ":")), flush=True)
                    except Exception as exc:
                        tick_had_failure = True
                        spread_last_result = {
                            "action": "error",
                            "reason": f"spread_phase_error:{type(exc).__name__}",
                        }
                        _append_event({
                            "event": "SPREAD_PAPER_PHASE_ERROR",
                            "error": str(exc),
                        })
                # DAEMON-LEVEL MAX-HOLD SAFETY NET: if any position has been held longer
                # than the maximum dynamic hold (35 min for strongest signals) + 5 minutes,
                # force a monitor call regardless of the daemon's current mode. If the
                # monitor still doesn't close the position within 2 additional minutes
                # (42 min absolute ceiling), force-close it directly in state. This prevents
                # the 329-minute hold bug from recurring even if the sleep interval, quote
                # worker, or monitor logic fails to exit the position.
                _SAFETY_NET_ABSOLUTE_CEILING = 42  # minutes; no position may exceed this
                for _sym in engine.SYMBOLS:
                    try:
                        _st = _read_state(_sym)
                        for _pos in _st.get("activePositions", []):
                            _entry_t = datetime.fromisoformat(_pos["entryTime"]).astimezone(ET)
                            _held = (datetime.now(ET) - _entry_t).total_seconds() / 60
                            if _held > 40:
                                _append_event({"event": "MAX_HOLD_SAFETY_NET", "symbol": _sym,
                                               "positionId": _pos.get("positionId"),
                                               "heldMinutes": round(_held, 1)})
                                _exit_code, _result, _raw = _capture_engine_call("monitor", _sym)
                                if _exit_code != 0:
                                    tick_had_failure = True
                                    tick_max_exit_code = max(tick_max_exit_code, _exit_code)
                                if should_emit(_result, _exit_code) and _raw:
                                    print(_raw, flush=True)
                                    deliver_alert(_raw)
                                # If position is STILL open after monitor, and held past the
                                # absolute ceiling, force-close it directly in the state file.
                                if _held > _SAFETY_NET_ABSOLUTE_CEILING:
                                    _post_st = _read_state(_sym)
                                    _still_open = [p for p in _post_st.get("activePositions", [])
                                                   if p.get("positionId") == _pos.get("positionId")]
                                    if _still_open:
                                        _force_pos = _still_open[0]
                                        _force_entry = float(_force_pos.get("entryAsk", 0))
                                        # Try to get a fresh bid from the latest quote snapshot
                                        _force_bid = _force_entry * 0.85  # fallback: assume -15%
                                        _force_occ = _force_pos.get("occSymbol", "")
                                        _quote_snap = Path.home() / ".hermes" / "state" / "quotes" / f"latest_option_quote_{_force_occ}.json"
                                        if _quote_snap.exists():
                                            try:
                                                _qdata = json.loads(_quote_snap.read_text())
                                                _force_bid = float(_qdata.get("bid", _force_entry * 0.85))
                                            except Exception:
                                                pass
                                        _force_pnl = round((_force_bid - _force_entry) * 100, 2)
                                        _force_pnl_pct = round((_force_bid / _force_entry - 1) * 100, 2) if _force_entry > 0 else -100.0
                                        engine.log({
                                            "event": "SAFETY_NET_FORCE_CLOSE",
                                            "symbol": _sym,
                                            "positionId": _pos.get("positionId"),
                                            "heldMinutes": round(_held, 1),
                                            "reason": "safety_net_force_close",
                                            "entryAsk": _force_entry,
                                            "exitBid": _force_bid,
                                            "pnl": _force_pnl,
                                            "pnlPct": _force_pnl_pct,
                                        }, _sym)
                                        _append_event({"event": "SAFETY_NET_FORCE_CLOSE", "symbol": _sym,
                                                       "positionId": _pos.get("positionId"),
                                                       "heldMinutes": round(_held, 1),
                                                       "pnl": _force_pnl,
                                                       "pnlPct": _force_pnl_pct})
                                        # Directly close in state
                                        _post_st["activePositions"] = []
                                        _post_st["dailyPnl"] = round(_post_st.get("dailyPnl", 0.0) + _force_pnl, 2)
                                        _post_st["dailyLoss"] = round(max(0.0, -_post_st["dailyPnl"]), 2)
                                        if _force_pnl < 0:
                                            _post_st["consecutiveLosses"] = _post_st.get("consecutiveLosses", 0) + 1
                                            _post_st["dailyGrossLoss"] = round(_post_st.get("dailyGrossLoss", 0.0) + abs(_force_pnl), 2)
                                        else:
                                            _post_st["consecutiveLosses"] = 0
                                        _post_st["lastExitAt"] = datetime.now(ET).isoformat()
                                        engine.save_state(_post_st, _sym)
                                        alert_msg = (f"🚨 SAFETY NET FORCE-CLOSE: {_sym} {_pos.get('positionId')} held {round(_held, 1)}min — "
                                                     f"exceeded {_SAFETY_NET_ABSOLUTE_CEILING}min absolute ceiling. "
                                                     f"P&L: ${_force_pnl:+.2f} ({_force_pnl_pct:+.1f}%)")
                                        print(alert_msg, flush=True)
                                        deliver_alert(alert_msg)
                    except Exception as exc:
                        tick_had_failure = True
                        tick_max_exit_code = max(tick_max_exit_code, 1)
                        _append_event({
                            "event": "MAX_HOLD_SAFETY_NET_STATE_ERROR",
                            "symbol": _sym,
                            "error": str(exc),
                            "failures": failures + 1,
                        })
                # Aggregate runtime status across all symbols. Publish the
                # per-symbol action so the health gate can distinguish a symbol
                # being scanned from one being monitored or intentionally idle.
                symbols_status: dict[str, Any] = {}
                status_now = datetime.now(ET)
                for sym in engine.SYMBOLS:
                    try:
                        sym_state = _read_state(sym)
                        sym_decision = choose_mode(sym_state, status_now, symbol=sym)
                        symbols_status[sym] = {
                            "dailyTrades": sym_state.get("dailyTrades", 0),
                            "activePositions": len(sym_state.get("activePositions", [])),
                            "lastAction": None,
                            "mode": sym_decision.mode,
                            "action": sym_decision.action,
                            "reason": sym_decision.reason,
                            "interval_seconds": sym_decision.interval_seconds,
                        }
                    except Exception:
                        symbols_status[sym] = {
                            "dailyTrades": 0,
                            "activePositions": 0,
                            "lastAction": None,
                            "mode": "unknown",
                            "action": None,
                            "reason": "state_unavailable",
                            "interval_seconds": OFF_HOURS_INTERVAL_SECONDS,
                        }
                # Cold-reset the quote worker only when ALL symbols are idle (no positions).
                # This prevents killing the quote worker while a position is still open in
                # another symbol — the 329-minute hold bug was partly caused by the worker
                # being cold-reset while QQQ had an active position.
                any_position_open = any(
                    ss.get("activePositions", 0) > 0 for ss in symbols_status.values()
                )
                try:
                    any_position_open = any_position_open or (
                        spread_paper.load_state().get("activePosition") is not None
                    )
                except Exception:
                    any_position_open = True
                any_action = any(
                    choose_mode(_read_state(s), datetime.now(ET), symbol=s).action is not None
                    for s in engine.SYMBOLS
                )
                if not any_position_open and not any_action:
                    if idle_since is None:
                        idle_since = time.monotonic()
                    if worker_cold_reset_due(time.monotonic() - idle_since, cold_reset_done):
                        runtime.cold_reset()
                        cold_reset_done = True
                else:
                    idle_since = None
                    cold_reset_done = False
                # Use the minimum interval across ALL symbols so that any open
                # position guarantees 1-second monitoring even when other symbols
                # are off-hours. Previously used only the first symbol's interval,
                # which caused a QQQ position to be monitored at SPY's 300s
                # off-hours interval — the 329-minute hold bug.
                min_interval = OFF_HOURS_INTERVAL_SECONDS  # start high
                for _sym in engine.SYMBOLS:
                    try:
                        _sym_state = _read_state(_sym)
                        _sym_decision = choose_mode(_sym_state, datetime.now(ET), symbol=_sym)
                        min_interval = min(min_interval, _sym_decision.interval_seconds)
                    except Exception:
                        pass
                if SPREAD_PAPER_ENABLED:
                    try:
                        if spread_paper.entry_window_open(datetime.now(ET)):
                            spread_interval = (
                                SPREAD_PAPER_POSITION_INTERVAL_SECONDS
                                if spread_paper.load_state().get("activePosition") is not None
                                else SPREAD_PAPER_IDLE_INTERVAL_SECONDS
                            )
                            min_interval = min(min_interval, spread_interval)
                    except Exception:
                        min_interval = min(min_interval, SPREAD_PAPER_IDLE_INTERVAL_SECONDS)
                failures = failures + 1 if tick_had_failure else 0
                interval = failure_interval(min_interval, failures)
                live_equity_data = {}
                for sym, quote in runtime.last_equity_quote.items():
                    live_equity_data[sym] = {
                        "bid": quote.get("bid"),
                        "ask": quote.get("ask"),
                        "mid": quote.get("mid"),
                        "venueTimestamp": quote.get("timestamp"),
                        "fetchedAt": quote.get("fetched_at"),
                    } if quote else None
                # Compute overall mode/reason from the most urgent symbol.
                # Keep explicit reasons when all symbols are intentionally idle;
                # otherwise the monitor would mistake outside_entry_window for
                # a daemon failure.
                mode_decisions: list[ModeDecision] = []
                for _sym in engine.SYMBOLS:
                    try:
                        _ms = choose_mode(_read_state(_sym), datetime.now(ET), symbol=_sym)
                        mode_decisions.append(_ms)
                    except Exception:
                        pass
                overall_mode, overall_reason = aggregate_runtime_mode(mode_decisions)
                write_runtime_status({
                    "mode": overall_mode,
                    "interval_seconds": interval,
                    "reason": overall_reason,
                    "iteration": iteration,
                    "consecutiveFailures": failures,
                    "symbols": symbols_status,
                    "spreadPaper": {
                        "enabled": SPREAD_PAPER_ENABLED,
                        "strategyId": spread_paper.STRATEGY_ID,
                        "strategyVersion": spread_paper.STRATEGY_VERSION,
                        "liveOrdersEnabled": False,
                        "activePosition": bool((spread_paper.load_state().get("activePosition") if SPREAD_PAPER_ENABLED else None)),
                        "dailyTradeCount": (spread_paper.load_state().get("dailyTradeCount", 0) if SPREAD_PAPER_ENABLED else 0),
                        "dailyPnl": (spread_paper.load_state().get("dailyPnl", 0.0) if SPREAD_PAPER_ENABLED else 0.0),
                        "lastAction": (spread_last_result or {}).get("action"),
                        "lastReason": (spread_last_result or {}).get("reason"),
                    },
                    "liveEquity": live_equity_data,
                    "tickDurationSeconds": round(time.monotonic() - tick_started, 4),
                    "processId": os.getpid(),
                })
                _sleep_until(tick_started + interval)
        except Exception as exc:
            failures += 1
            interval = failure_interval(ACTIVE_INTERVAL_SECONDS, failures)
            error = {"action": "ERROR", "reason": "daemon_iteration_error", "error": str(exc)}
            print(json.dumps(error), flush=True)
            _append_event({"event": "ITERATION_ERROR", "error": str(exc), "failures": failures})
            write_runtime_status({
                "mode": "error_backoff", "iteration": iteration,
                "consecutiveFailures": failures, "error": str(exc),
                "interval_seconds": interval, "processId": os.getpid(),
            })
        finally:
            runtime.restore()
            PID_PATH.unlink(missing_ok=True)
            write_runtime_status({"mode": "stopped", "iteration": iteration, "processId": os.getpid()})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="V7.4 adaptive multi-symbol paper-only live runner")
    parser.add_argument("--once", action="store_true", help="Run one adaptive iteration and exit")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    return run_loop(max_iterations=1 if args.once else None)


if __name__ == "__main__":
    raise SystemExit(main())
