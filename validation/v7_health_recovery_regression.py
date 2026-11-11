#!/usr/bin/env python3
"""Regression tests for the proactive V7 operational responder."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

import v7_health_recovery as recovery
import v7_scalp_paper as engine


ET = ZoneInfo("America/New_York")


class HealthRecoveryTests(unittest.TestCase):
    def _report(self, failures: list[str]) -> dict:
        return {
            "healthy": False,
            "failures": failures,
            "runtime": {"liveOrdersEnabled": False},
        }

    def test_transient_quote_failure_is_restartable(self):
        report = self._report(["equity_quote_stale:SPY"])
        positions = {"known": True, "positions": []}
        self.assertEqual("restart_daemon", recovery.select_action(report, positions, {}))

    def test_ledger_integrity_failure_never_gets_restart(self):
        report = self._report(["SPY:orphaned_entry"])
        positions = {"known": True, "positions": []}
        self.assertIsNone(recovery.select_action(report, positions, {}))

    def test_open_position_blocks_restart(self):
        report = self._report(["heartbeat_stale"])
        positions = {
            "known": True,
            "positions": [{"symbol": "QQQ", "positionId": "paper-open"}],
        }
        self.assertIsNone(recovery.select_action(report, positions, {}))

    def test_unknown_position_state_blocks_restart(self):
        report = self._report(["equity_quote_stale:SPY"])
        positions = {"known": False, "positions": [], "errors": ["state_missing:SPY"]}
        self.assertIsNone(recovery.select_action(report, positions, {}))

    def test_state_reset_issue_uses_existing_preflight_fix(self):
        report = self._report(["state_reset_stale:SPY"])
        positions = {"known": True, "positions": []}
        self.assertEqual("preflight_fix", recovery.select_action(report, positions, {}))

    def test_restart_cooldown_blocks_repeat_restart(self):
        current = datetime(2026, 8, 28, 12, 0, tzinfo=ET)
        state = {"lastActionAt": (current - timedelta(minutes=1)).isoformat()}
        report = self._report(["equity_quote_stale:SPY"])
        positions = {"known": True, "positions": []}
        self.assertIsNone(
            recovery.select_action(report, positions, state, current=current)
        )

    def test_unresolved_restart_incident_never_restarts_again_after_cooldown(self):
        current = datetime(2026, 8, 30, 18, 30, tzinfo=ET)
        state = {
            "lastAction": "restart_daemon",
            "lastActionAt": (current - timedelta(minutes=30)).isoformat(),
            "lastFailureFingerprint": "equity_quote_stale:SPY",
            "lastOutcome": "blocked",
        }
        report = self._report(["consecutive_failures"])
        positions = {"known": True, "positions": []}
        self.assertIsNone(
            recovery.select_action(report, positions, state, current=current)
        )

    def test_corrupt_recovery_state_fails_closed(self):
        report = self._report(["equity_quote_stale:SPY"])
        positions = {"known": True, "positions": []}
        self.assertIsNone(
            recovery.select_action(report, positions, {"state_invalid": True})
        )

    def test_auto_resume_requires_clean_report_and_zero_positions(self):
        report = {
            "healthy": True,
            "failures": [],
            "runtime": {"liveOrdersEnabled": False},
        }
        self.assertTrue(recovery.can_auto_resume(report, {"known": True, "positions": []}))
        self.assertFalse(
            recovery.can_auto_resume(
                report,
                {"known": True, "positions": [{"symbol": "SPY", "positionId": "open"}]},
            )
        )
        self.assertFalse(recovery.can_auto_resume(report, {"known": False, "positions": []}))

    def test_positions_snapshot_proves_empty_state_for_every_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            for symbol in engine.SYMBOLS:
                (state_dir / f"v7_scalp_state_{symbol}.json").write_text(
                    json.dumps({"activePositions": []})
                )
            with patch.object(recovery, "STATE_DIR", state_dir):
                result = recovery._positions_snapshot()
        self.assertTrue(result["known"], result)
        self.assertEqual([], result["positions"])

    def test_missing_state_does_not_prove_zero_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(recovery, "STATE_DIR", Path(tmp)):
                result = recovery._positions_snapshot()
        self.assertFalse(result["known"], result)
        self.assertTrue(result["errors"])

    def test_redaction_removes_credential_like_diagnostics(self):
        scrubbed = recovery._redact("authorization=Bearer-secret token=abc123")
        self.assertNotIn("Bearer-secret", scrubbed)
        self.assertNotIn("abc123", scrubbed)
        self.assertIn("[REDACTED]", scrubbed)

    def test_wait_for_heartbeat_rejects_stopped_runtime_marker(self):
        current = datetime(2026, 9, 14, 14, 32, 11, tzinfo=ET)
        previous = {
            "updatedAt": (current - timedelta(seconds=1)).isoformat(),
            "processId": 39696,
        }
        stopped = {
            "updatedAt": current.isoformat(),
            "processId": 39696,
            "mode": "stopped",
            "consecutiveFailures": None,
        }
        with patch.object(recovery, "now_et", return_value=current), patch.object(
            recovery, "_read_runtime", return_value=stopped
        ), patch.object(recovery, "time") as clock:
            clock.monotonic.side_effect = [0.0, 1.0, 31.0]
            clock.sleep.return_value = None
            self.assertFalse(recovery._wait_for_heartbeat(previous))

    def test_run_once_restarts_transient_fault_then_resumes(self):
        unhealthy = self._report(["equity_quote_stale:SPY"])
        clean = {
            "healthy": True,
            "failures": [],
            "runtime": {"liveOrdersEnabled": False},
        }
        no_positions = {"known": True, "positions": []}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kill_path = root / "v7_kill_switch.json"
            kill_path.write_text(json.dumps({"active": True, "reason": "health_gate:test"}))
            with patch.object(recovery, "KILL_SWITCH_PATH", kill_path), patch.object(
                recovery, "RECOVERY_STATE_PATH", root / "recovery_state.json"
            ), patch.object(
                recovery, "RECOVERY_LATEST_PATH", root / "recovery_latest.json"
            ), patch.object(
                recovery, "RECOVERY_LOG_PATH", root / "recovery.jsonl"
            ), patch.object(
                recovery, "_load_recovery_state", return_value={}
            ), patch.object(
                recovery,
                "evaluate_health_once",
                side_effect=[(unhealthy, None), (clean, None), (clean, None), (clean, None), (clean, None)],
            ), patch.object(
                recovery,
                "_positions_snapshot",
                side_effect=[no_positions] * 5,
            ), patch.object(
                recovery,
                "_restart_daemon",
                return_value={"returncode": 0, "heartbeat_fresh": True},
            ) as restart, patch.object(
                recovery,
                "_resume_gate",
            ) as resume, patch.object(recovery, "time") as clock:
                clock.sleep.return_value = None

                def clear_kill_switch():
                    kill_path.unlink()
                    return {"returncode": 0}

                resume.side_effect = clear_kill_switch
                result = recovery.run_once()

        self.assertEqual(0, result)
        restart.assert_called_once()
        resume.assert_called_once()

    def test_run_once_refuses_restart_when_position_is_open(self):
        unhealthy = self._report(["heartbeat_stale"])
        open_positions = {
            "known": True,
            "positions": [{"symbol": "QQQ", "positionId": "paper-open"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kill_path = root / "v7_kill_switch.json"
            kill_path.write_text(json.dumps({"active": True, "reason": "test"}))
            with patch.object(recovery, "KILL_SWITCH_PATH", kill_path), patch.object(
                recovery, "RECOVERY_STATE_PATH", root / "recovery_state.json"
            ), patch.object(
                recovery, "RECOVERY_LATEST_PATH", root / "recovery_latest.json"
            ), patch.object(
                recovery, "RECOVERY_LOG_PATH", root / "recovery.jsonl"
            ), patch.object(
                recovery, "_load_recovery_state", return_value={}
            ), patch.object(
                recovery, "evaluate_health_once", return_value=(unhealthy, None)
            ), patch.object(
                recovery, "_positions_snapshot", return_value=open_positions
            ), patch.object(recovery, "_restart_daemon") as restart, patch.object(
                recovery, "_resume_gate"
            ) as resume:
                result = recovery.run_once()
                kill_switch_remains = kill_path.exists()

        self.assertEqual(1, result)
        restart.assert_not_called()
        resume.assert_not_called()
        self.assertTrue(kill_switch_remains)

    def test_run_once_preserves_manual_kill_switch_after_clean_health(self):
        clean = {
            "healthy": True,
            "failures": [],
            "runtime": {"liveOrdersEnabled": False},
        }
        no_positions = {"known": True, "positions": []}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kill_path = root / "v7_kill_switch.json"
            kill_path.write_text(json.dumps({
                "active": False,
                "reason": "explicit control installed; paper evidence collection enabled",
            }))
            with (
                patch.object(recovery, "KILL_SWITCH_PATH", kill_path),
                patch.object(recovery, "RECOVERY_STATE_PATH", root / "recovery_state.json"),
                patch.object(recovery, "RECOVERY_LATEST_PATH", root / "recovery_latest.json"),
                patch.object(recovery, "RECOVERY_LOG_PATH", root / "recovery.jsonl"),
                patch.object(recovery, "_load_recovery_state", return_value={}),
                patch.object(recovery, "evaluate_health_once", return_value=(clean, None)),
                patch.object(recovery, "_positions_snapshot", return_value=no_positions),
                patch.object(recovery, "_resume_gate") as resume,
            ):
                result = recovery.run_once()
                kill_switch_remains = kill_path.exists()
        self.assertEqual(0, result)
        resume.assert_not_called()
        self.assertTrue(kill_switch_remains)


    def test_runtime_release_drift_does_not_block_bounded_restart(self):
        failures = [
            "engineSourceSha256_mismatch",
            "configSha256_mismatch",
            "releaseManifestSha256_mismatch",
        ]

        def fake_run(command, *, timeout):
            self.assertEqual(90, timeout)
            if command[-1] == "check":
                return {"returncode": 2}
            self.assertEqual("static-check", command[-1])
            return {"returncode": 0}

        with patch.object(recovery, "_run", side_effect=fake_run) as command:
            result = recovery._run_release_sync_if_needed(failures)

        self.assertEqual(0, result["returncode"])
        command.assert_called_once()
        self.assertEqual("static-check", command.call_args.args[0][-1])


if __name__ == "__main__":
    unittest.main()
