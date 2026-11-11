#!/usr/bin/python3
"""Exec V7 daemons with Hermes' dependency-complete managed Python runtime."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> None:
    home = Path(os.environ.get("HOME", str(Path.home())))
    installs = home / ".hermes" / "installs"
    candidates: list[tuple[float, Path, Path]] = []
    for facts_path in installs.glob("*/facts.json"):
        try:
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
            package = facts["packages"]["venv"]
            runtime = Path(package["environment"])
            interpreter = runtime / "bin" / "python3"
            if interpreter.is_file() and os.access(interpreter, os.X_OK):
                candidates.append((facts_path.stat().st_mtime, facts_path.parent.resolve(), interpreter))
        except (OSError, KeyError, TypeError, ValueError):
            continue
    if not candidates:
        raise RuntimeError("no dependency-complete Hermes runtime found")
    _, install_root, interpreter = max(candidates, key=lambda item: item[0])
    allowed_root = (install_root / "environments").resolve()
    if allowed_root not in interpreter.absolute().parents:
        raise RuntimeError(f"invalid Hermes runtime path: {interpreter}")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    os.execve(str(interpreter), [str(interpreter), *sys.argv[1:]], env)


if __name__ == "__main__":
    main()
