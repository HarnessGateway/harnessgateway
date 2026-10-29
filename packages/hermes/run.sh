#!/usr/bin/env bash
# One turn of Hermes Agent: map.py runs `hermes chat --format stream-json` and prints Responses events (README.md).
set -uo pipefail
exec python3 "$HG_PACKAGE_DIR/map.py"
