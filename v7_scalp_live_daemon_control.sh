#!/bin/bash
set -euo pipefail

label="com.hermes.v73-paper-live-daemon"
domain="gui/$(id -u)"
plist="$HOME/Library/LaunchAgents/${label}.plist"

case "${1:-status}" in
  start)
    /bin/mkdir -p "$HOME/.hermes/logs" "$HOME/.hermes/state"
    /bin/launchctl bootout "$domain/$label" >/dev/null 2>&1 || true
    /bin/launchctl bootstrap "$domain" "$plist"
    /bin/launchctl kickstart -k "$domain/$label"
    ;;
  stop)
    /bin/launchctl bootout "$domain/$label" >/dev/null 2>&1 || true
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    /bin/launchctl print "$domain/$label"
    ;;
  *)
    printf 'usage: %s {start|stop|restart|status}\n' "$0" >&2
    exit 2
    ;;
esac
