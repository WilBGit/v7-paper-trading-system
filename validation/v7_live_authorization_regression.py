#!/usr/bin/env python3
"""Regression tests for V7 live authorization in health gate, recovery, and preflight."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _auth_file(tmpdir: Path, **overrides) -> Path:
    import v7_live_auth

    payload = {
        "authorized": True,
        "authorizedBy": "Project Contributor",
        "authorizedAt": "2026-09-03T13:50:00-07:00",
        "accountNumber": "801336223",
        **v7_live_auth._current_identity(),
        "readinessAttested": True,
        **overrides,
    }
    p = tmpdir / "v7_live_authorization.json"
    p.write_text(json.dumps(payload))
    return p


class HealthGateAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        import v7_system_health_check as health
        import v7_live_auth
        self.health = health
        self.auth_module = v7_live_auth
        import v7_release
        self.auth_path = self.tmpdir / "v7_live_authorization.json"
        self.engine_version = v7_release.identity()["engineVersion"]

    def test_live_orders_enabled_is_failure_when_not_authorized(self):
        """Without authorization file, liveOrdersEnabled=true is still a failure."""
        runtime = {"liveOrdersEnabled": True, "processId": 123, "consecutiveFailures": 0,
                   "updatedAt": "2026-09-01T14:00:00-04:00", "mode": "active_scan",
                   "engineVersion": self.engine_version}
        with patch("v7_live_auth.AUTHORIZATION_PATH", self.tmpdir / "nonexistent.json"):
            result = self.health.inspect_runtime(runtime, now=self.health.now_et())
        self.assertIn("live_orders_enabled", result["failures"])

    def test_live_orders_enabled_passes_when_authorized_and_version_matches(self):
        """With valid authorization and matching engine version, liveOrdersEnabled=true is OK."""
        _auth_file(self.tmpdir)
        runtime = {"liveOrdersEnabled": True, "processId": 123, "consecutiveFailures": 0,
                   "updatedAt": "2026-09-01T14:00:00-04:00", "mode": "active_scan",
                   "engineVersion": self.engine_version}
        with patch("v7_live_auth.AUTHORIZATION_PATH", self.auth_path):
            with patch.object(self.auth_module, "_evidence_gate_met", return_value=True):
                result = self.health.inspect_runtime(runtime, now=self.health.now_et())
        self.assertNotIn("live_orders_enabled", result["failures"])

    def test_live_orders_enabled_fails_when_authorized_but_version_mismatches(self):
        """Authorization for a different engine version does not grant live privilege."""
        _auth_file(self.tmpdir, engineVersion="7.4.20-evidence-gate-wiring")
        runtime = {"liveOrdersEnabled": True, "processId": 123, "consecutiveFailures": 0,
                   "updatedAt": "2026-09-01T14:00:00-04:00", "mode": "active_scan",
                   "engineVersion": self.engine_version}
        with patch("v7_live_auth.AUTHORIZATION_PATH", self.auth_path):
            result = self.health.inspect_runtime(runtime, now=self.health.now_et())
        self.assertIn("live_orders_enabled", result["failures"])

    def test_live_orders_enabled_fails_when_not_authorized_field(self):
        """Authorization file exists but authorized=false is not a grant."""
        _auth_file(self.tmpdir, authorized=False)
        runtime = {"liveOrdersEnabled": True, "processId": 123, "consecutiveFailures": 0,
                   "updatedAt": "2026-09-01T14:00:00-04:00", "mode": "active_scan",
                   "engineVersion": self.engine_version}
        with patch("v7_live_auth.AUTHORIZATION_PATH", self.auth_path):
            result = self.health.inspect_runtime(runtime, now=self.health.now_et())
        self.assertIn("live_orders_enabled", result["failures"])

    def test_live_authorized_rejects_source_hash_mismatch(self):
        _auth_file(self.tmpdir, sourceCodeSha256="wrong-source")
        with patch("v7_live_auth.AUTHORIZATION_PATH", self.auth_path):
            with patch.object(self.auth_module, "_evidence_gate_met", return_value=True):
                self.assertFalse(self.auth_module.live_authorized())

    def test_live_authorized_rejects_config_hash_mismatch(self):
        _auth_file(self.tmpdir, configSha256="wrong-config")
        with patch("v7_live_auth.AUTHORIZATION_PATH", self.auth_path):
            with patch.object(self.auth_module, "_evidence_gate_met", return_value=True):
                self.assertFalse(self.auth_module.live_authorized())

    def test_live_authorized_rejects_missing_readiness_attestation(self):
        _auth_file(self.tmpdir, readinessAttested=False)
        with patch("v7_live_auth.AUTHORIZATION_PATH", self.auth_path):
            with patch.object(self.auth_module, "_evidence_gate_met", return_value=True):
                self.assertFalse(self.auth_module.live_authorized())

    def test_live_authorized_rejects_unmet_evidence_gate(self):
        _auth_file(self.tmpdir)
        with patch("v7_live_auth.AUTHORIZATION_PATH", self.auth_path):
            with patch.object(self.auth_module, "_evidence_gate_met", return_value=False):
                self.assertFalse(self.auth_module.live_authorized())

    def test_live_authorization_requires_transactional_sidecar_identity(self):
        identity = self.auth_module._current_identity()
        self.assertTrue(identity.get("liveDaemonSourceSha256"))
        self.assertTrue(identity.get("liveBridgeSourceSha256"))

        _auth_file(self.tmpdir)
        with patch("v7_live_auth.AUTHORIZATION_PATH", self.auth_path):
            with patch.object(self.auth_module, "_evidence_gate_met", return_value=True):
                self.assertTrue(self.auth_module.live_authorized())

    def test_paper_mode_still_passes_without_authorization(self):
        """Paper mode (liveOrdersEnabled=false) passes regardless of authorization."""
        runtime = {"liveOrdersEnabled": False, "processId": 123, "consecutiveFailures": 0,
                   "updatedAt": "2026-09-01T14:00:00-04:00", "mode": "active_scan",
                   "engineVersion": self.engine_version}
        with patch("v7_live_auth.AUTHORIZATION_PATH", self.tmpdir / "nonexistent.json"):
            result = self.health.inspect_runtime(runtime, now=self.health.now_et())
        self.assertNotIn("live_orders_enabled", result["failures"])


class StateCheckAuthorizationTests(unittest.TestCase):
    """The per-symbol state check also flags liveOrdersEnabled. Verify authorization applies."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        import v7_system_health_check as health
        import v7_scalp_paper as engine
        import v7_live_auth
        self.health = health
        self.engine = engine
        self.auth_module = v7_live_auth
        self.auth_path = self.tmpdir / "v7_live_authorization.json"
        self.state_dir = self.tmpdir / "state"
        self.state_dir.mkdir()

    def test_state_live_orders_passes_when_authorized(self):
        _auth_file(self.tmpdir)
        now = self.health.now_et()
        for symbol in self.engine.SYMBOLS:
            state = {
                "symbol": symbol,
                "activePositions": [],
                "liveOrdersEnabled": True,
                "lastResetDate": self.engine.trading_session_key(symbol, now),
                "updatedAt": now.isoformat(),
            }
            (self.state_dir / f"v7_scalp_state_{symbol}.json").write_text(json.dumps(state))
            (self.state_dir / f"v7_scalp_trades_{symbol}.jsonl").write_text("")

        with patch("v7_live_auth.AUTHORIZATION_PATH", self.auth_path):
            with patch.object(self.auth_module, "_evidence_gate_met", return_value=True):
                with patch.object(self.health, "STATE_DIR", self.state_dir):
                    result = self.health.evaluate_health(now=now)
        # No live_orders_enabled failures for any symbol
        for f in result["failures"]:
            self.assertFalse("live_orders_enabled" in f, f"unexpected failure: {f}")


class RecoveryAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        import v7_live_auth
        self.auth_module = v7_live_auth
        self.auth_path = self.tmpdir / "v7_live_authorization.json"

    def test_live_orders_enabled_is_non_autofix_without_authorization(self):
        """Without authorization, live_orders_enabled stays in NON_AUTOFIX_ROOTS."""
        import v7_health_recovery as recovery
        with patch("v7_live_auth.AUTHORIZATION_PATH", self.tmpdir / "nonexistent.json"):
            self.assertIn("live_orders_enabled", recovery.NON_AUTOFIX_ROOTS)

    def test_live_orders_enabled_removable_with_authorization(self):
        """With valid authorization, live_authorized() returns True."""
        _auth_file(self.tmpdir)
        with patch("v7_live_auth.AUTHORIZATION_PATH", self.auth_path):
            with patch.object(self.auth_module, "_evidence_gate_met", return_value=True):
                self.assertTrue(self.auth_module.live_authorized())


class ActivationEvidenceBarTests(unittest.TestCase):
    """Project Contributor's live bar is 25 exact-release closes + positive P&L, not the 5-trade review."""

    def setUp(self):
        import v7_live_auth
        import v7_live_gate_progress
        self.auth_module = v7_live_auth
        self.progress = v7_live_gate_progress

    def _snapshot(self, **overrides):
        payload = {
            "gateMet": True,
            "enableLiveAutomatically": False,
            "liveOrdersEnabled": False,
            "exactClosedTrades": 6,
            "exactNetPnl": 22.0,
        }
        payload.update(overrides)
        return payload

    def test_five_trade_review_does_not_meet_live_activation_bar(self):
        snapshot = self._snapshot(exactClosedTrades=6, exactNetPnl=22.0)
        with patch.object(self.progress, "build_snapshot", return_value=snapshot):
            self.assertFalse(self.auth_module._evidence_gate_met())

    def test_twenty_five_exact_closes_with_positive_pnl_meets_live_bar(self):
        snapshot = self._snapshot(exactClosedTrades=25, exactNetPnl=40.0)
        with patch.object(self.progress, "build_snapshot", return_value=snapshot):
            self.assertTrue(self.auth_module._evidence_gate_met())

    def test_twenty_five_exact_closes_with_nonpositive_pnl_fail_closed(self):
        snapshot = self._snapshot(exactClosedTrades=25, exactNetPnl=0.0)
        with patch.object(self.progress, "build_snapshot", return_value=snapshot):
            self.assertFalse(self.auth_module._evidence_gate_met())


class EnableMonitorActivationBarTests(unittest.TestCase):
    def test_manual_enable_path_requires_twenty_five_exact_closes(self):
        import v7_live_enable_monitor as monitor
        self.assertGreaterEqual(monitor.MIN_EXACT_CURRENT_RELEASE_TRADES, 25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
