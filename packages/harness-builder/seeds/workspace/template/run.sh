#!/usr/bin/env bash
# One turn of the shell harness: map.py runs the input text as a bash command and prints Responses events.
set -uo pipefail
exec python3 "$HG_PACKAGE_DIR/map.py"
