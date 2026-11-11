#!/usr/bin/env bash
# Run the active V7 preflight check with auto-fix enabled
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/v7_preflight_check.py" --fix
