#!/usr/bin/env bash
# One turn of Pi: map.py writes Pi's provider and MCP config, runs `pi -p --mode json` and maps its events onto
# Responses events (README.md says what each request field becomes).
set -uo pipefail
exec python3 "$HG_PACKAGE_DIR/map.py"
