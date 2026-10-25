#!/usr/bin/env bash
# One turn of Hermes Agent: run.py drives `hermes acp` (Agent Client Protocol over stdio) and map.py turns what it
# reports (session updates, Hermes's log, new rows of its state.db transcript) into Responses events with hgkit.
set -uo pipefail
cd "$HG_WORKSPACE"
exec python3 "$HG_PACKAGE_DIR/run.py"
