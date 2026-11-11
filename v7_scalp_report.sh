#!/bin/bash
# Active V7 multi-symbol daily report — reports all active symbols.
# Calls the engine's report command once per symbol and concatenates results.
set -euo pipefail

SCRIPT="/path/to/project/.hermes/scripts/v7_scalp_paper.py"
SCRIPT_DIR="$(dirname "$SCRIPT")"
SYMBOLS="$(
    PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" \
        /usr/bin/env python3 -c 'import v7_scalp_paper as engine; print(" ".join(engine.SYMBOLS))'
)"

for SYMBOL in $SYMBOLS; do
    echo "=== $SYMBOL ==="
    /usr/bin/env python3 "$SCRIPT" report --symbol "$SYMBOL"
    echo ""
done
