#!/usr/bin/env bash
# One turn: the request's input text is a bash command, run in the workspace. The mapper (map.py) prints the
# Responses events: a shell_call, its shell_call_output, an assistant message with the output, response.completed.
set -uo pipefail
exec python3 "$HG_PACKAGE_DIR/map.py"
