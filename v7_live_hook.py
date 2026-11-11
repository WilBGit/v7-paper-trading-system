#!/usr/bin/env python3
"""Disabled compatibility shim for the retired live hook API.

The active V7.4.21 engine and daemon are paper-only. Transactional execution is
not reachable through this module. Live order routing is handled by the separate
v7_live_execution_daemon.py which watches paper state without modifying hashed
strategy source.
"""
from __future__ import annotations

from typing import Any


DISABLED_REASON = "active_v7421_paper_boundary"


def maybe_live_entry(position: dict[str, Any], *, dispatcher: Any = None) -> dict[str, Any]:
    """Return a disabled result without changing state or contacting a broker."""
    return {"live": False, "reason": DISABLED_REASON}


def maybe_live_exit(
    position: dict[str, Any],
    exit_bid: float,
    *,
    dispatcher: Any = None,
) -> dict[str, Any]:
    """Return a disabled result without changing state or contacting a broker."""
    return {"live": False, "reason": DISABLED_REASON}
