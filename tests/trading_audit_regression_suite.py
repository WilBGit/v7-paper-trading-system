#!/usr/bin/env python3
"""Durable regressions for future-version-aware trading Dream audits.

This stable filename avoids Hermes' ephemeral ``test_*`` cleanup rule. Run with:
    python3 tests/trading_audit_regression_suite.py -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import nightly_self_improvement as nightly
import trading_dream_audit as audit_module


class TradingAuditRegressions(unittest.TestCase):
    def test_engine_release_wins_over_stale_state_and_drift_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            scripts = home / "scripts"
            state_dir = home / "state"
            cron_dir = home / "cron"
            scripts.mkdir(parents=True)
            state_dir.mkdir()
            cron_dir.mkdir()

            (scripts / "engine.py").write_text(
                "from pathlib import Path\n"
                "ROOT = Path.home()\n"
                "STATE_PATH = ROOT / 'state' / 'fixture_state.json'\n"
                "LOG_PATH = ROOT / 'state' / 'fixture_trades.jsonl'\n"
                "ENGINE_VERSION = '9.1.2-paper-scalp'\n"
                "STRATEGY_ID = 'fixture-strategy'\n"
                "MAX_TRADES = 3\nMIN_DEBIT = 75.0\nMAX_DEBIT = 175.0\n"
                "MAX_DAILY_LOSS = 40.0\nENTRY_END = 1500\n"
            )
            (cron_dir / "jobs.json").write_text(json.dumps({"jobs": [{
                "id": "scan-fixture",
                "name": "V9.1 Paper Scalp Scan",
                "script": "engine.py",
                "schedule": "*/5 6-12 * * 1-5",
                "enabled": True,
                "state": "scheduled",
                "last_status": "ok",
            }]}))
            (state_dir / "fixture_state.json").write_text(json.dumps({
                "version": "9.1-paper-scalp",
                "mode": "dry_run",
                "liveOrdersEnabled": False,
                "fundingGate": "blocked_fixture",
                "activePositions": [],
                "tradeLimit": 3,
                "minDebit": 75.0,
                "maxDebit": 175.0,
                "dailyLossLimit": 40.0,
            }))
            (state_dir / "fixture_trades.jsonl").write_text("")

            result = audit_module.audit(
                home=home,
                now=datetime(2026, 8, 10, 2, 0, tzinfo=audit_module.PT),
            )

        self.assertEqual("9.1.2-paper-scalp", result["active_release"])
        drifts = [finding for finding in result["findings"] if finding.get("code") == "state_release_metadata_drift"]
        self.assertTrue(drifts, result["findings"])
        self.assertEqual("version", drifts[0]["field"])

    def test_silent_scan_attempt_log_is_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            scripts = home / "scripts"
            state_dir = home / "state"
            scripts.mkdir(parents=True)
            state_dir.mkdir()
            (scripts / "alert.py").write_text(
                "from pathlib import Path\n"
                "SCAN_AUDIT = Path.home() / '.hermes/state/fixture_scan_attempts.jsonl'\n"
            )
            now = datetime(2026, 8, 10, 2, 0, tzinfo=audit_module.PT)
            (state_dir / "fixture_scan_attempts.jsonl").write_text(json.dumps({
                "timestamp": now.isoformat(),
                "result": {"action": "SKIP", "reason": "market_closed"},
            }) + "\n")
            jobs = [{"id": "scanner", "name": "Entry Alerts", "script": "alert.py"}]

            decisions = audit_module.load_recent_scan_attempts(jobs, now, home, scripts)

        self.assertEqual(1, len(decisions))
        self.assertEqual("market_closed", decisions[0]["reason"])

    def test_retired_premarket_data_lineage_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            scripts = home / "scripts"
            state_dir = home / "state"
            cron_dir = home / "cron"
            scripts.mkdir(parents=True)
            state_dir.mkdir()
            cron_dir.mkdir()
            (scripts / "engine.py").write_text(
                "from pathlib import Path\n"
                "ROOT = Path.home()\n"
                "STATE_PATH = ROOT / 'state' / 'engine_state.json'\n"
                "LOG_PATH = ROOT / 'state' / 'engine_trades.jsonl'\n"
                "ENGINE_VERSION = '9.1-paper'\n"
            )
            (scripts / "premarket.py").write_text(
                "from pathlib import Path\n"
                "ROOT = Path('/retired')\n"
                "DATA = ROOT / 'data' / 'v6.2'\n"
                "TRADES = DATA / 'trades.jsonl'\n"
                "DECISIONS = DATA / 'decisions.jsonl'\n"
            )
            (state_dir / "engine_state.json").write_text(json.dumps({
                "version": "9.1-paper",
                "mode": "dry_run",
                "liveOrdersEnabled": False,
                "fundingGate": "blocked_fixture",
                "activePositions": [],
            }))
            (state_dir / "engine_trades.jsonl").write_text("")
            (cron_dir / "jobs.json").write_text(json.dumps({"jobs": [
                {
                    "id": "scan",
                    "name": "V9 Paper Scalp Scan",
                    "script": "engine.py",
                    "schedule": "*/5 6-12 * * 1-5",
                    "enabled": True,
                },
                {
                    "id": "brief",
                    "name": "V9 Trading Morning Premarket Brief",
                    "script": "premarket.py",
                    "schedule": "55 5 * * 1-5",
                    "enabled": True,
                },
            ]}))
            result = audit_module.audit(
                home=home,
                now=datetime(2026, 8, 10, 2, 0, tzinfo=audit_module.PT),
            )

        codes = {finding.get("code") for finding in result["findings"]}
        self.assertIn("retired_support_data_lineage", codes)

    def test_scanner_coverage_includes_last_step_minute(self):
        job = {"schedule": "*/5 6-12 * * 1-5"}
        self.assertEqual(1255, audit_module.scanner_coverage_end(job))

    def test_nightly_collector_always_runs_dream_in_dry_run_mode(self):
        calls = []

        def fake_run(path, args, timeout=150):
            calls.append((path.name, list(args)))
            return 0, "ok"

        with patch.object(nightly, "run_script", side_effect=fake_run), patch.object(
            sys, "argv", ["nightly_self_improvement.py", "--days", "2"]
        ):
            self.assertEqual(0, nightly.main())

        self.assertEqual(["--days", "2", "--dry-run"], calls[0][1])
        self.assertEqual("trading_dream_audit.py", calls[1][0])


if __name__ == "__main__":
    unittest.main()
