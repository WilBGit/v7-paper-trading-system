#!/usr/bin/env python3
"""Deterministic nightly audit context for the active paper-trading release.

Discovers the active release from enabled Hermes cron jobs, resolves wrapper
scripts to their Python engine, summarizes state/trades/cron decisions, and
emits mechanical-vs-risk findings for the nightly Dreaming agent.

This collector is read-only. It never calls a broker, writes trading state, or
changes strategy parameters.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
JOBS_PATH = HERMES_HOME / "cron" / "jobs.json"
SCRIPTS_DIR = HERMES_HOME / "scripts"
OUTPUT_DIR = HERMES_HOME / "cron" / "output"

TRADING_JOB_RE = re.compile(
    r"(?:\bV\d+(?:\.\d+)?\b.*(?:Trading|Paper|Scalp)|"
    r"(?:Paper|Scalp).*(?:Scan|Monitor|Results|Review)|"
    r"Alpha Floor Daily Channel Fill)",
    re.I,
)
PYTHON_TARGET_RE = re.compile(r"(?P<path>/[^\s\"']+\.py)\b")
PATH_FILE_RE = re.compile(r"^(?P<name>STATE_PATH|LOG_PATH)\s*=.*?[\"'](?P<file>[^\"']+\.(?:json|jsonl))[\"']", re.M)
SCAN_AUDIT_RE = re.compile(r"^SCAN_AUDIT\s*=.*?[\"'](?P<file>[^\"']+\.jsonl)[\"']", re.M)


def load_jobs(path: Path = JOBS_PATH) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    jobs = raw.get("jobs", raw) if isinstance(raw, dict) else raw
    return jobs if isinstance(jobs, list) else []


def is_active(job: dict[str, Any]) -> bool:
    return bool(job.get("enabled", True)) and job.get("state", "scheduled") not in {"paused", "removed"}


def trading_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [job for job in jobs if is_active(job) and TRADING_JOB_RE.search(str(job.get("name", "")))]


def is_scan_job(job: dict[str, Any]) -> bool:
    """Identify scanners by either their label or scheduled script path."""
    return "scan" in f"{job.get('name', '')} {job.get('script', '')}".lower()


def resolve_python_target(job: dict[str, Any], scripts_dir: Path = SCRIPTS_DIR) -> Path | None:
    script = job.get("script")
    if not script:
        return None
    wrapper = Path(script)
    if not wrapper.is_absolute():
        wrapper = scripts_dir / wrapper
    if wrapper.suffix == ".py":
        return wrapper if wrapper.exists() else None
    try:
        text = wrapper.read_text()
    except OSError:
        return None
    match = PYTHON_TARGET_RE.search(text)
    return Path(match.group("path")) if match else None


def select_engine(jobs: list[dict[str, Any]], scripts_dir: Path = SCRIPTS_DIR) -> Path | None:
    targets = []
    for job in jobs:
        if any(word in str(job.get("name", "")).lower() for word in ("scan", "monitor", "results", "review")):
            target = resolve_python_target(job, scripts_dir)
            if target:
                targets.append(target)
    if not targets:
        return None
    return Counter(targets).most_common(1)[0][0]


def source_constants(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError):
        return {}
    values: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            continue
        if isinstance(value, (str, int, float, bool, type(None))):
            values[node.targets[0].id] = value
    return values


def active_release_metadata(scripts_dir: Path) -> dict[str, str]:
    """Read the active release identity instead of inferring it from state."""
    try:
        payload = json.loads((scripts_dir / "v7_release.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    engine_version = payload.get("engine_version")
    strategy_id = payload.get("strategy_id")
    result: dict[str, str] = {}
    if isinstance(engine_version, str) and engine_version:
        result["ENGINE_VERSION"] = engine_version
    if isinstance(strategy_id, str) and strategy_id:
        result["STRATEGY_ID"] = strategy_id
    return result


def active_runtime_symbols(scripts_dir: Path) -> set[str]:
    """Return the symbol universe declared by the active engine."""
    try:
        import sys

        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import v7_scalp_paper as engine
        return {str(symbol).upper() for symbol in engine.SYMBOLS}
    except (ImportError, AttributeError, OSError, ValueError):
        return set()


def discover_data_paths(engine: Path | None, home: Path = HERMES_HOME) -> tuple[dict[str, Path], dict[str, Path]]:
    """Discover per-symbol state and log paths.

    Returns (state_paths, log_paths) dicts keyed by symbol.
    Handles both legacy single-file and V7.4 per-symbol layouts.
    """
    state_paths: dict[str, Path] = {}
    log_paths: dict[str, Path] = {}
    state_dir = home / "state"

    # V7.4 per-symbol files (preferred). Discover only the active scanner
    # universe so leftover IWM/XSP files cannot re-enter tracking.
    try:
        import v7_scalp_paper as engine
        active_symbols = {str(symbol).upper() for symbol in engine.SYMBOLS}
    except Exception:
        active_symbols = {"SPY", "QQQ"}
    for sp in sorted(state_dir.glob("v7_scalp_state_*.json")):
        sym = sp.stem.removeprefix("v7_scalp_state_")
        if sym and sp.is_file() and sym.upper() in active_symbols:
            state_paths[sym] = sp
    for lp in sorted(state_dir.glob("v7_scalp_trades_*.jsonl")):
        sym = lp.stem.removeprefix("v7_scalp_trades_")
        if sym and lp.is_file() and sym.upper() in active_symbols:
            log_paths[sym] = lp

    # Legacy single-file fallback for SPY
    if "SPY" not in state_paths:
        legacy_state = state_dir / "v7_scalp_state.json"
        if legacy_state.exists():
            state_paths["SPY"] = legacy_state
    if "SPY" not in log_paths:
        legacy_log = state_dir / "v7_scalp_trades.jsonl"
        if legacy_log.exists():
            log_paths["SPY"] = legacy_log

    # Also try regex discovery from engine source as last resort
    if not state_paths and engine and engine.exists():
        try:
            text = engine.read_text()
        except OSError:
            pass
        else:
            found = {m.group("name"): home / "state" / Path(m.group("file")).name for m in PATH_FILE_RE.finditer(text)}
            if "STATE_PATH" in found and found["STATE_PATH"].exists():
                state_paths["SPY"] = found["STATE_PATH"]
            if "LOG_PATH" in found and found["LOG_PATH"].exists():
                log_paths["SPY"] = found["LOG_PATH"]

    return state_paths, log_paths


def read_json(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=PT)
    except ValueError:
        return None


def read_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if not path:
        return []
    rows = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def recent_rows(rows: list[dict[str, Any]], now: datetime, hours: int = 24) -> list[dict[str, Any]]:
    cutoff = now - timedelta(hours=hours)
    return [row for row in rows if (ts := parse_timestamp(row.get("timestamp"))) and ts.astimezone(now.tzinfo) >= cutoff]


def cron_expr(job: dict[str, Any]) -> str:
    schedule = job.get("schedule", "")
    if isinstance(schedule, dict):
        return str(schedule.get("expr") or schedule.get("display") or "")
    return str(schedule)


def expand_cron_hours(field: str) -> list[int]:
    hours: set[int] = set()
    for piece in field.split(","):
        piece = piece.strip()
        if piece == "*":
            hours.update(range(24)); continue
        if "-" in piece:
            start, end = piece.split("-", 1)
            if start.isdigit() and end.isdigit():
                hours.update(range(int(start), int(end) + 1))
        elif piece.isdigit():
            hours.add(int(piece))
    return sorted(hour for hour in hours if 0 <= hour <= 23)


def last_cron_minute(field: str) -> int:
    if field == "*" or field.startswith("*/"):
        step = int(field.split("/", 1)[1]) if "/" in field else 1
        return max(range(0, 60, step))
    values = []
    for piece in field.split(","):
        if piece.isdigit():
            values.append(int(piece))
        elif "-" in piece:
            span, _, step_text = piece.partition("/")
            start, end = map(int, span.split("-", 1))
            step = int(step_text) if step_text else 1
            values.extend(range(start, end + 1, step))
    return max(values) if values else 0


def scanner_coverage_end(job: dict[str, Any]) -> int | None:
    fields = cron_expr(job).split()
    if len(fields) != 5:
        return None
    hours = expand_cron_hours(fields[1])
    return (max(hours) * 100 + last_cron_minute(fields[0])) if hours else None


def entry_end_pt(constants: dict[str, Any], now: datetime) -> int | None:
    raw = constants.get("ENTRY_END")
    if not isinstance(raw, int):
        return None
    et_dt = datetime(now.year, now.month, now.day, raw // 100, raw % 100, tzinfo=ET)
    pt_dt = et_dt.astimezone(PT)
    return pt_dt.hour * 100 + pt_dt.minute


def inspect_support_data_lineage(
    jobs: list[dict[str, Any]], scripts_dir: Path, *, active_positions: bool
) -> list[dict[str, Any]]:
    """Detect enabled reports that still consume a retired release's data."""
    findings: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for job in jobs:
        descriptor = f"{job.get('name', '')} {job.get('script', '')}"
        if not re.search(r"premarket|brief|report|alpha_floor_daily_fill", descriptor, re.I):
            continue
        target = resolve_python_target(job, scripts_dir)
        if not target or target in seen or not target.exists():
            continue
        seen.add(target)
        try:
            source = target.read_text(errors="replace")
        except OSError:
            continue

        evidence: list[str] = []
        if target.name.startswith("v63_"):
            evidence.append("retired v63 target filename")
        if re.search(r"^\s*DATA\s*=.*['\"]v6\.2['\"]", source, re.M):
            evidence.append("retired v6.2 DATA assignment")
        if "v63_morning_premarket_brief.py" in source:
            evidence.append("retired v63 premarket dependency")
        if evidence:
            findings.append({
                "severity": "high",
                "class": "mechanical-auto-fixable",
                "auto_fixable": not active_positions,
                "code": "retired_support_data_lineage",
                "job": job.get("name"),
                "target": str(target),
                "evidence": evidence,
                "detail": "An enabled report/support job reads or imports a retired release instead of the active paper engine data lineage.",
            })
    return findings


def load_recent_cron_decisions(job_ids: list[str], now: datetime, output_dir: Path = OUTPUT_DIR) -> list[dict[str, Any]]:
    cutoff = now.timestamp() - 86400
    decisions = []
    for job_id in job_ids:
        directory = output_dir / job_id
        if not directory.exists():
            continue
        for path in directory.glob("*.md"):
            try:
                if path.stat().st_mtime < cutoff:
                    continue
                text = path.read_text()
            except OSError:
                continue
            for line in reversed(text.splitlines()):
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload["job_id"] = job_id
                payload["output_file"] = str(path)
                decisions.append(payload)
                break
    return decisions


def load_recent_scan_attempts(
    scan_jobs: list[dict[str, Any]], now: datetime, home: Path, scripts_dir: Path
) -> list[dict[str, Any]]:
    """Read explicit scan audit logs used when successful cron stdout is silent."""
    decisions: list[dict[str, Any]] = []
    for job in scan_jobs:
        target = resolve_python_target(job, scripts_dir)
        if not target:
            continue
        try:
            match = SCAN_AUDIT_RE.search(target.read_text())
        except OSError:
            continue
        if not match:
            continue
        path = home / "state" / Path(match.group("file")).name
        for row in recent_rows(read_jsonl(path), now):
            result = row.get("result")
            if isinstance(result, dict):
                decisions.append({**result, "job_id": job.get("id") or job.get("job_id"),
                                  "audit_file": str(path)})
    return decisions


def audit(home: Path = HERMES_HOME, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(PT)
    jobs = trading_jobs(load_jobs(home / "cron" / "jobs.json"))
    scripts_dir = home / "scripts"
    engine = select_engine(jobs, scripts_dir)
    constants = source_constants(engine)
    release_metadata = active_release_metadata(scripts_dir)
    constants.update(release_metadata)
    state_paths, log_paths = discover_data_paths(engine, home)
    runtime_symbols = active_runtime_symbols(scripts_dir)
    if runtime_symbols:
        state_paths = {sym: path for sym, path in state_paths.items() if sym in runtime_symbols}
        log_paths = {sym: path for sym, path in log_paths.items() if sym in runtime_symbols}
    # Merge all per-symbol state into a combined view for audit
    combined_state: dict[str, Any] = {}
    all_rows: list[dict[str, Any]] = []
    for sym in sorted(state_paths.keys()):
        sym_state = read_json(state_paths[sym])
        if sym_state:
            if combined_state:
                # Merge numeric fields, keep the most recent
                for key in ("dailyTrades", "dailyPnl", "dailyLoss", "dailyGrossLoss", "consecutiveLosses"):
                    combined_state[key] = combined_state.get(key, 0) + sym_state.get(key, 0)
                combined_state.setdefault("symbols", {})[sym] = sym_state
            else:
                combined_state = dict(sym_state)
                combined_state["symbols"] = {sym: dict(sym_state)}
        if sym in log_paths:
            all_rows.extend(read_jsonl(log_paths[sym]))
    state = combined_state
    # Also load the legacy scan attempts log (shared across symbols)
    scan_attempts_path = home / "state" / "v7_scalp_scan_attempts.jsonl"
    rows = all_rows if all_rows else read_jsonl(scan_attempts_path)
    # Primary state for config drift checks: use SPY as representative
    primary_state_path = state_paths.get("SPY")
    primary_log_path = log_paths.get("SPY")
    declared_version = release_metadata.get("ENGINE_VERSION") or constants.get("VERSION")
    recent = recent_rows(rows, now)

    scan_jobs = [job for job in jobs if is_scan_job(job)]
    # Photon alert wrappers intentionally suppress stdout for normal scans, so
    # their explicit JSONL audit is authoritative. Cron output is a fallback for
    # releases that do not expose a scan-attempt log.
    scan_decisions = load_recent_scan_attempts(scan_jobs, now, home, scripts_dir)
    if not scan_decisions:
        scan_decisions = load_recent_cron_decisions(
            [str(job.get("id") or job.get("job_id")) for job in scan_jobs], now, home / "cron" / "output"
        )

    event_counts = Counter(str(row.get("event", "unknown")) for row in recent)
    quote_reasons = Counter(str(row.get("reason", "unknown")) for row in recent if row.get("event") == "SKIP_QUOTE")
    decision_reasons = Counter(str(item.get("reason", item.get("action", "unknown"))) for item in scan_decisions)
    exits = [row for row in rows if row.get("event") == "PAPER_EXIT_REAL_BID"]
    pnls = [float(row.get("pnl", 0) or 0) for row in exits]

    findings: list[dict[str, Any]] = []
    findings.extend(inspect_support_data_lineage(
        jobs, scripts_dir, active_positions=any(
            bool(sym_state.get("activePositions"))
            for sym_state in state.get("symbols", {}).values()
        ) if state.get("symbols") else bool(state.get("activePositions"))
    ))
    if not jobs:
        findings.append({"severity": "critical", "class": "mechanical", "auto_fixable": False, "code": "no_active_trading_jobs", "detail": "No enabled trading cron jobs were discovered."})
    if not engine:
        findings.append({"severity": "critical", "class": "mechanical", "auto_fixable": False, "code": "engine_not_resolved", "detail": "Could not resolve the active Python engine from scheduled wrappers."})

    for job in jobs:
        if job.get("last_status") == "error" or job.get("last_error"):
            findings.append({"severity": "high", "class": "mechanical", "auto_fixable": True, "code": "cron_error", "job_id": job.get("id"), "detail": f"{job.get('name')}: {job.get('last_error') or 'last_status=error'}"})

    for scan_job in scan_jobs:
        coverage = scanner_coverage_end(scan_job)
        required = entry_end_pt(constants, now)
        if coverage is not None and required is not None and coverage < required:
            findings.append({
                "severity": "high", "class": "mechanical", "auto_fixable": True,
                "code": "scanner_schedule_shorter_than_entry_window",
                "job_id": scan_job.get("id"), "coverage_end_pt": coverage,
                "entry_end_pt": required,
                "detail": f"Scanner coverage ends at {coverage:04d} PT but engine accepts entries until {required:04d} PT.",
            })

    config_map = {
        "MAX_TRADES": "tradeLimit", "MAX_DEBIT": "maxDebit", "MIN_DEBIT": "minDebit",
        "MAX_DAILY_LOSS": "dailyLossLimit",
    }
    for constant, field in config_map.items():
        if constant in constants and field in state and constants[constant] != state[field]:
            findings.append({"severity": "high", "class": "mechanical", "auto_fixable": True, "code": "state_config_drift", "constant": constant, "field": field, "source_value": constants[constant], "state_value": state[field]})

    release_metadata_map = {"ENGINE_VERSION": "version", "STRATEGY_ID": "strategyId"}
    for constant, field in release_metadata_map.items():
        source_value = constants.get(constant)
        if state and source_value is not None and state.get(field) != source_value:
            findings.append({
                "severity": "medium", "class": "mechanical",
                "auto_fixable": not any(
                    bool(sym_state.get("activePositions"))
                    for sym_state in state.get("symbols", {}).values()
                ) if state.get("symbols") else not bool(state.get("activePositions")),
                "code": "state_release_metadata_drift", "constant": constant,
                "field": field, "source_value": source_value,
                "state_value": state.get(field),
                "detail": (
                    f"State {field}={state.get(field)!r} does not match active engine "
                    f"{constant}={source_value!r}."
                ),
            })

    if state and state.get("liveOrdersEnabled") is not False:
        findings.append({"severity": "critical", "class": "safety", "auto_fixable": False, "code": "paper_safety_violation", "detail": "liveOrdersEnabled is not explicitly false. Stop and alert Project Contributor; do not trade."})
    if state and "blocked" not in str(state.get("fundingGate", "")).lower():
        findings.append({"severity": "critical", "class": "safety", "auto_fixable": False, "code": "funding_gate_not_blocked", "detail": "Funding gate is not blocked. Stop and alert Project Contributor; do not trade."})

    quote_skips = sum(quote_reasons.values())
    entries = event_counts.get("PAPER_ENTRY_REAL_ASK", 0)
    if quote_skips and entries == 0:
        findings.append({
            "severity": "medium", "class": "strategy_risk", "auto_fixable": False,
            "code": "contract_quality_bottleneck",
            "detail": f"Qualified signals reached quotes but produced {quote_skips} quality-gate skips and zero entries. Do not loosen risk gates automatically.",
            "reasons": dict(quote_reasons),
        })

    # The scheduled engine is the release source of truth. State metadata can
    # lag until the next paper command persists a normalized state document.
    version = declared_version or state.get("version") or (engine.name if engine else "unknown")
    live_authorized_status = False
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).resolve().parent))
        from v7_live_auth import live_authorized
        live_authorized_status = live_authorized()
    except Exception:
        pass
    return {
        "generated_at": now.isoformat(),
        "audit_mode": "read_only",
        "active_release": version,
        "engine": str(engine) if engine else None,
        "state_path": str(primary_state_path) if primary_state_path else None,
        "log_path": str(primary_log_path) if primary_log_path else None,
        "per_symbol_state_paths": {sym: str(p) for sym, p in state_paths.items()},
        "per_symbol_log_paths": {sym: str(p) for sym, p in log_paths.items()},
        "jobs": [{
            "id": job.get("id") or job.get("job_id"), "name": job.get("name"),
            "script": job.get("script"), "schedule": cron_expr(job),
            "last_status": job.get("last_status"), "last_run_at": job.get("last_run_at"),
        } for job in jobs],
        "paper_safety": {
            "mode": state.get("mode"), "liveOrdersEnabled": state.get("liveOrdersEnabled"),
            "liveAuthorized": live_authorized_status,
            "fundingGate": state.get("fundingGate"),
            "kill_switch_present": (home / "state" / "v7_kill_switch.json").exists(),
        },
        "state": state,
        "last_24h": {
            "event_counts": dict(event_counts), "quote_skip_reasons": dict(quote_reasons),
            "scan_decision_reasons": dict(decision_reasons), "scan_decisions": len(scan_decisions),
        },
        "all_time_closed": {
            "trades": len(pnls), "net_pnl": round(sum(pnls), 2),
            "win_rate_pct": round(sum(p > 0 for p in pnls) / len(pnls) * 100, 1) if pnls else 0.0,
            "avg_pnl": round(mean(pnls), 2) if pnls else 0.0,
        },
        "source_constants": {key: constants.get(key) for key in (
            "ENGINE_VERSION", "STRATEGY_ID",
            "MAX_TRADES", "MAX_DEBIT", "MIN_DEBIT", "MAX_DAILY_LOSS", "MAX_OTM_PCT",
            "MAX_QUOTE_SPREAD_PCT", "ENTRY_START", "ENTRY_END", "HARD_CLOSE",
        ) if key in constants},
        "findings": findings,
    }


def render(result: dict[str, Any]) -> str:
    lines = [
        "## Nightly Trading Audit Context",
        f"- Active release: {result['active_release']}",
        f"- Engine: {result['engine']}",
        f"- Paper safety: {json.dumps(result['paper_safety'], sort_keys=True)}",
        f"- 24h events: {json.dumps(result['last_24h']['event_counts'], sort_keys=True)}",
        f"- 24h quote skips: {json.dumps(result['last_24h']['quote_skip_reasons'], sort_keys=True)}",
        f"- 24h scan decisions: {json.dumps(result['last_24h']['scan_decision_reasons'], sort_keys=True)}",
        f"- All-time closed: {json.dumps(result['all_time_closed'], sort_keys=True)}",
        f"- Findings: {len(result['findings'])}",
    ]
    for finding in result["findings"]:
        lines.append(f"  - [{finding['severity']}] {finding['code']}: {finding.get('detail', '')}")
    lines.append("\nTRADING_AUDIT_JSON")
    lines.append(json.dumps(result, indent=2, sort_keys=True))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = audit()
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
