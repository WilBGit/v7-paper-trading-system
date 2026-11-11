#!/bin/bash
# Macro event paper runner — cron wrapper (silent on idle, loud on events).
# Runs one bounded segment of the isolated CPI/PPI/FOMC PAPER watcher.
PY=/path/to/project/.hermes/hermes-agent/venv/bin/python3
OUT=$("$PY" /path/to/project/.hermes/scripts/v7_macro_event_runner.py run 2>/dev/null)
STATUS=$(printf '%s' "$OUT" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
case "$STATUS" in
  ""|idle|idle_until_prearm|holding|no_entry|kill_switch_active) exit 0 ;;
  *) printf 'MACRO-PAPER %s\n' "$OUT" ;;
esac
