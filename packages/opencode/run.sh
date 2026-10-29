#!/usr/bin/env bash
# One OpenCode turn: map.py reads the Responses request ($HG_REQUEST), writes OpenCode's config for the turn (one
# provider at $HG_PROVIDER_URL with the per-turn token), drives `opencode serve` over its HTTP API in the workspace and
# maps its event stream onto Responses events.
set -uo pipefail
exec python3 "$HG_PACKAGE_DIR/map.py"
