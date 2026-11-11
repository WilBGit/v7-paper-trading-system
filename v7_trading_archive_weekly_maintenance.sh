#!/bin/bash
set -euo pipefail
cd /path/to/project/.hermes/trading-db
exec /usr/local/bin/python3 db_manager.py maintain
