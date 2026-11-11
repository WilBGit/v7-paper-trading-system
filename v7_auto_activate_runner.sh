#!/bin/bash
# Wrapper for v7_live_auto_activate.py — report-only gate monitor
# It never enables live trading; manual activation requires the explicit monitor command.
cd /path/to/project/.hermes/scripts
export V7_RH_DIRECT_RUNTIME=1
export PATH=/path/to/project/.hermes/hermes-agent/venv/bin:$PATH
/path/to/project/.hermes/hermes-agent/venv/bin/python3 v7_live_auto_activate.py 2>&1
