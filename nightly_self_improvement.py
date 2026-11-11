#!/usr/bin/env python3
"""Hybrid nightly context: general Hermes dreaming + active trading audit."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run_script(path: Path, args: list[str], timeout: int = 150) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(path), *args],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = result.stdout.strip()
    if result.stderr.strip():
        output += ("\n" if output else "") + "STDERR:\n" + result.stderr.strip()
    return result.returncode, output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1)
    # Kept for CLI compatibility. The nightly collector is always read-only.
    parser.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    dream_args = ["--days", str(args.days), "--dry-run"]

    dream_rc, dream_output = run_script(ROOT / "dream.py", dream_args)
    audit_rc, audit_output = run_script(ROOT / "trading_dream_audit.py", [])

    print("# Nightly Self-Improvement Context")
    print("\n## General Session Dream")
    print(f"collector_exit={dream_rc}")
    print(dream_output or "No general dream output.")
    print("\n## Active Trading Release Audit")
    print(f"collector_exit={audit_rc}")
    print(audit_output or "No trading audit output.")
    print("\n## Agent Handoff")
    print("Review the context above under the cron prompt's paper-safety and change-control rules.")

    # A quiet day may make dream.py return 1; the trading audit must still reach
    # the agent. Only fail the collector if the trading audit itself failed.
    return 0 if audit_rc == 0 else audit_rc


if __name__ == "__main__":
    raise SystemExit(main())
