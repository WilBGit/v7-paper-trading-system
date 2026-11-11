#!/usr/bin/env bash
# Deterministic V7 archive import. No LLM or terminal sandbox required.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DB_DIR="$HERMES_HOME/trading-db"
PYTHON="/usr/local/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
    PYTHON="$(command -v python3)"
fi

cd "$DB_DIR"
"$PYTHON" db_manager.py import
"$PYTHON" db_manager.py status
