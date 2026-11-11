#!/bin/bash
# Macro event schedule refresh — daily 5:50 AM PT. Official BLS/Fed feeds only.
# Echoes only when the refresh FAILS so failures surface; success is silent.
PY=/path/to/project/.hermes/hermes-agent/venv/bin/python3
OUT=$("$PY" /path/to/project/.hermes/scripts/v7_macro_event_runner.py refresh 2>&1)
STATUS=$(printf '%s' "$OUT" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
[ "$STATUS" = "ok" ] && exit 0
printf 'MACRO-SCHEDULE-REFRESH-FAILED %s\n' "$OUT"
exit 1
