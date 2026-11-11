#!/usr/bin/env python3
"""Durable regressions for the deterministic Dream extractor.

The filename intentionally does not start with ``test_``: Hermes' disk-cleanup
plugin treats newly created test_* files under ~/.hermes as ephemeral. Run with:
    python3 tests/dream_regression_suite.py -v
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import dream


class DreamExtractionRegressions(unittest.TestCase):
    @staticmethod
    def _session(*contents: str) -> list[dict]:
        return [{
            "id": "fixture",
            "messages": [
                {"role": "user", "content": content, "timestamp": time.time()}
                for content in contents
            ],
        }]

    def test_async_and_embedded_task_wrappers_do_not_become_insights(self):
        sessions = self._session(
            "[ASYNC DELEGATION BATCH COMPLETE — deleg_fixture]\n"
            "A subagent says: don't use broken X; this failed.",
            "Ok\n\n[Your active task list was preserved across context compression]\n"
            "- test failed and needs a fix",
        )
        self.assertEqual([], dream.extract_signals(sessions))

    def test_real_user_correction_is_still_extracted(self):
        signals = dream.extract_signals(self._session("Don't use Alex; use Evan instead."))
        self.assertTrue(any(signal["type"] == "correction" for signal in signals), signals)

    def test_ordinary_instruction_is_not_a_lesson(self):
        signals = dream.extract_signals(self._session("Make sure it sounds human."))
        self.assertFalse(any(signal["type"] == "lesson" for signal in signals), signals)

    def test_subagent_sessions_are_excluded_at_load_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            connection = sqlite3.connect(db_path)
            connection.executescript("""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    title TEXT,
                    started_at REAL NOT NULL,
                    message_count INTEGER,
                    tool_call_count INTEGER,
                    model TEXT,
                    archived INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    tool_name TEXT,
                    timestamp REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
            """)
            now = time.time()
            connection.executemany(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                [
                    ("direct", "photon", "direct", now, 1, 0, "fixture"),
                    ("delegated", "subagent", "delegated", now, 1, 0, "fixture"),
                ],
            )
            connection.executemany(
                "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
                [
                    ("direct", "Don't use the old voice.", now),
                    ("delegated", "Fix the broken scanner and patch the issue.", now),
                ],
            )
            connection.commit()
            connection.close()

            sessions = dream.load_recent_sessions(db_path, days=1)

        self.assertEqual(["direct"], [session["id"] for session in sessions])

    def test_successful_storage_count_excludes_errors_and_dry_runs(self):
        results = [
            "Stored 'dream-lesson-20260810' (1 insights)",
            "Error storing 'dream-pattern-20260810': connection refused",
            "Failed to store 'dream-correction-20260810': HTTP 500",
            "[DRY] Would store 'dream-preference-20260810' with 1 insights",
        ]
        self.assertEqual(1, dream.count_successful_stores(results))


if __name__ == "__main__":
    unittest.main()
