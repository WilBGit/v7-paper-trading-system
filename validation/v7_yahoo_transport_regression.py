#!/usr/bin/env python3
"""Regression tests for the V7 Yahoo chart transport failover."""
from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import v7_scalp_paper as engine  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._payload


def _payload():
    return {
        "chart": {
            "result": [{
                "timestamp": [1_700_000_000],
                "indicators": {
                    "quote": [{
                        "open": [100.0],
                        "high": [101.0],
                        "low": [99.0],
                        "close": [100.5],
                        "volume": [12345.0],
                    }],
                },
            }],
            "error": None,
        },
    }


class YahooTransportTests(unittest.TestCase):
    def test_tls_timeout_on_query1_falls_back_to_query2(self):
        calls = []
        responses = [
            urllib.error.URLError("<urlopen error _ssl.c:993: The handshake operation timed out>"),
            _FakeResponse(_payload()),
        ]

        def fake_urlopen(request, timeout):
            calls.append((request.full_url, timeout))
            outcome = responses.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch.object(engine.urllib.request, "urlopen", side_effect=fake_urlopen):
            bars = engine.fetch_bars("QQQ", days=5)

        self.assertEqual(1, len(bars))
        self.assertEqual(100.5, bars[0]["close"])
        self.assertEqual(2, len(calls))
        self.assertIn("query1.finance.yahoo.com", calls[0][0])
        self.assertIn("query2.finance.yahoo.com", calls[1][0])
        self.assertEqual(engine.YAHOO_CHART_TIMEOUT_SECONDS, calls[0][1])
        self.assertEqual(engine.YAHOO_CHART_TIMEOUT_SECONDS, calls[1][1])

    def test_all_hosts_fail_with_bounded_attempts_and_diagnostic(self):
        calls = []

        def fake_urlopen(request, timeout):
            calls.append((request.full_url, timeout))
            raise urllib.error.URLError("temporary yahoo outage")

        with patch.object(engine.urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaisesRegex(RuntimeError, "yahoo_chart_fetch_failed:QQQ"):
                engine.fetch_bars("QQQ", days=5)

        self.assertEqual(engine.YAHOO_CHART_MAX_ATTEMPTS, len(calls))
        self.assertLessEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
