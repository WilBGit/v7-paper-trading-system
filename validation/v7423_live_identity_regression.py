#!/usr/bin/env python3
"""Regression coverage for V7.4.23 active live-sidecar identity plumbing.

The live bridge stays authorization-gated. These tests never dispatch a broker
call.
"""
from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import v7_live_execution_daemon as execution_daemon  # noqa: E402
import v7_live_order_bridge as order_bridge  # noqa: E402
import v7_release  # noqa: E402
import v7_release_sync  # noqa: E402


class LiveSidecarIdentityTests(unittest.TestCase):
    def test_execution_ref_id_uses_the_active_strategy_namespace(self) -> None:
        position_id = "paper-v7423-identity-test"
        expected = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{v7_release.STRATEGY_ID}:entry:{position_id}",
            )
        )
        self.assertEqual(expected, execution_daemon._bridge_ref_id(position_id, "entry"))

    def test_execution_identity_attests_transactional_sidecars(self):
        identity = execution_daemon._current_identity()
        self.assertTrue(identity.get("liveDaemonSourceSha256"))
        self.assertTrue(identity.get("liveBridgeSourceSha256"))

        task_id, user_task = order_bridge._release_dispatch_context()
        self.assertEqual(
            f"{v7_release.STRATEGY_ID}-live-order-execution",
            task_id,
        )
        self.assertEqual(
            f"{v7_release.ENGINE_VERSION} live option order execution",
            user_task,
        )

    def test_release_sync_validates_the_active_live_sidecars(self) -> None:
        self.assertEqual(
            "v7_release.STRATEGY_ID",
            v7_release_sync.DYNAMIC_CONSUMERS["v7_live_execution_daemon.py"],
        )
        self.assertEqual(
            "v7_release.identity()",
            v7_release_sync.DYNAMIC_CONSUMERS["v7_live_order_bridge.py"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
