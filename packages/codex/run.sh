#!/usr/bin/env bash
# One Codex turn: map.py reads the Responses request ($HG_REQUEST), runs `codex exec --json` in the workspace with the
# provider pinned to $HG_PROVIDER_URL/v1 and the per-turn token, and maps its JSONL onto Responses events.
set -uo pipefail
exec python3 "$HG_PACKAGE_DIR/map.py"
