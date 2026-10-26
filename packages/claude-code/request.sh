#!/usr/bin/env bash
# One turn of Claude Code. map.py reads the Responses request ($HG_REQUEST), runs `claude -p ... --output-format
# stream-json` in the workspace with the provider and model pinned, and maps its stream onto Responses events.
set -uo pipefail
cd "$HG_WORKSPACE"
exec python3 "$HG_PACKAGE_DIR/map.py"
