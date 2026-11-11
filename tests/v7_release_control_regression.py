#!/usr/bin/env python3
"""Regression checks for V7 release identity and provenance plumbing."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

import v7_release
import v7_release_sync
import v7_scalp_paper
import v7_scalp_photon_alert


class V7ReleaseControlTests(unittest.TestCase):
    def test_manifest_and_engine_identity_match(self) -> None:
        self.assertEqual(v7_release.ENGINE_VERSION, v7_scalp_paper.ENGINE_VERSION)
        self.assertEqual(v7_release.STRATEGY_ID, v7_scalp_paper.STRATEGY_ID)
        self.assertEqual(
            v7_release.MANIFEST_SHA256,
            v7_scalp_paper.RELEASE_MANIFEST_SHA256,
        )
        self.assertTrue(v7_release.RELEASE["changes"])
        self.assertEqual(5, v7_release.EVIDENCE_GATE["min_closed_trades"])
        self.assertEqual(0.0, v7_release.EVIDENCE_GATE["min_win_rate_pct"])
        self.assertEqual(60.0, v7_release.RELEASE["evidence_gate"]["min_win_rate_pct"])
        self.assertTrue(v7_release.EVIDENCE_GATE["require_positive_pnl"])

    def test_every_live_consumer_has_dynamic_version_reference(self) -> None:
        missing = []
        for filename, token in v7_release_sync.DYNAMIC_CONSUMERS.items():
            source = (SCRIPTS_DIR / filename).read_text(encoding="utf-8")
            if token not in source:
                missing.append(f"{filename}:{token}")
        self.assertEqual([], missing)

    def test_static_release_check_does_not_require_runtime_identity(self) -> None:
        with patch.object(v7_release_sync, "current_runtime", return_value={}):
            ok, details = v7_release_sync.static_check()
        self.assertTrue(ok, details)
        self.assertEqual([], details["errors"])

    def test_scan_audit_envelope_contains_frozen_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "attempts.jsonl"
            old_path = v7_scalp_photon_alert.SCAN_AUDIT
            try:
                v7_scalp_photon_alert.SCAN_AUDIT = audit_path
                v7_scalp_photon_alert._audit(
                    "",
                    {"action": "SKIP", "reason": "test"},
                    0,
                    symbol="SPY",
                )
            finally:
                v7_scalp_photon_alert.SCAN_AUDIT = old_path
            record = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(v7_scalp_paper.ENGINE_VERSION, record["engineVersion"])
        self.assertEqual(v7_scalp_paper.STRATEGY_ID, record["strategyId"])
        self.assertEqual(
            v7_scalp_paper.RELEASE_MANIFEST_SHA256,
            record["releaseManifestSha256"],
        )
        self.assertEqual(v7_scalp_paper.source_code_sha256(), record["sourceCodeSha256"])
        self.assertEqual(v7_scalp_paper.config_sha256(), record["configSha256"])

    def test_ledger_log_overwrites_stale_caller_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            old_path = v7_scalp_paper.log_path
            try:
                v7_scalp_paper.log_path = lambda _symbol: ledger
                v7_scalp_paper.log(
                    {
                        "event": "TEST_LINEAGE",
                        "engineVersion": "stale",
                        "strategyId": "stale",
                        "releaseManifestSha256": "stale",
                        "sourceCodeSha256": "stale",
                        "configSha256": "stale",
                    },
                    "SPY",
                )
            finally:
                v7_scalp_paper.log_path = old_path
            record = json.loads(ledger.read_text(encoding="utf-8"))
        self.assertEqual(v7_scalp_paper.ENGINE_VERSION, record["engineVersion"])
        self.assertEqual(v7_scalp_paper.STRATEGY_ID, record["strategyId"])
        self.assertEqual(v7_scalp_paper.RELEASE_MANIFEST_SHA256, record["releaseManifestSha256"])


if __name__ == "__main__":
    raise SystemExit(unittest.main(verbosity=2))
