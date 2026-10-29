#!/usr/bin/env bash
# One turn of Gemini CLI: map.py runs `gemini -o stream-json` and prints Responses events (README.md).
set -uo pipefail
exec python3 "$HG_PACKAGE_DIR/map.py"
