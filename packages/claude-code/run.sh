#!/usr/bin/env bash
# One turn of Claude Code: map.py runs `claude -p --output-format stream-json` with the provider and model pinned and
# maps its stream onto Responses events (README.md says what each request field becomes).
set -uo pipefail
exec python3 "$HG_PACKAGE_DIR/map.py"
