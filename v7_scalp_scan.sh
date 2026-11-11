#!/bin/bash
set -o pipefail
out=$(/usr/bin/env python3 /path/to/project/.hermes/scripts/v7_scalp_paper.py scan 2>&1)
rc=$?
if printf '%s\n' "$out" | grep -qE '"action": "(PAPER_ENTRY|PAPER_EXIT|ERROR)"'; then
  printf '%s\n' "$out"
elif [ "$rc" -ne 0 ]; then
  printf '%s\n' "$out"
fi
exit "$rc"
