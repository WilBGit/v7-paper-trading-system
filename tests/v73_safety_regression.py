#!/usr/bin/env python3
"""Stable tests-directory entry point for the durable V7.3 safety suite.

The implementation remains in ``validation/v73_regression_suite.py`` for
backward compatibility. This filename deliberately avoids the ephemeral
``test_*`` cleanup rule under ~/.hermes.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from validation.v73_regression_suite import *  # noqa: F401,F403


if __name__ == "__main__":
    unittest.main()
