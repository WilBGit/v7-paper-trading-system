#!/usr/bin/env python3
"""Keep the active V7.4.23 strategy and daemon paper-only.

The transactional lifecycle is developed in ~/.hermes/quarantine and must not
be imported by the active engine or daemon until separately certified.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
STATE = Path.home() / ".hermes" / "state"


class ActivePaperBoundaryTests(unittest.TestCase):
    def test_active_engine_has_no_transactional_hook(self):
        source = (SCRIPTS / "v7_scalp_paper.py").read_text(encoding="utf-8")
        self.assertNotIn("v7_live_hook", source)
        self.assertNotIn("maybe_live_entry", source)
        self.assertNotIn("maybe_live_exit", source)
        self.assertNotIn("submit_entry_order", source)
        self.assertNotIn("submit_exit_order", source)

    def test_active_daemon_has_no_transactional_order_import(self):
        source = (SCRIPTS / "v7_scalp_live_daemon.py").read_text(encoding="utf-8")
        self.assertNotIn("v7_live_order_bridge", source)
        self.assertNotIn("place_option_order", source)
        self.assertNotIn("review_option_order", source)

    def test_all_active_runtime_states_remain_paper_only(self):
        runtime = json.loads((STATE / "v7_scalp_live_runtime.json").read_text(encoding="utf-8"))
        self.assertFalse(runtime.get("liveOrdersEnabled"))
        for path in STATE.glob("v7_scalp_state_*.json"):
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("dry_run", state.get("mode"), path.name)
            self.assertFalse(state.get("liveOrdersEnabled"), path.name)

    def test_legacy_live_hook_is_hard_disabled(self):
        hook = (SCRIPTS / "v7_live_hook.py").read_text(encoding="utf-8")
        self.assertNotIn("v7_live_order_bridge", hook)
        self.assertNotIn("handle_function_call", hook)
        self.assertNotIn("mcp__robinhood_trading__", hook)


if __name__ == "__main__":
    unittest.main(verbosity=2)
