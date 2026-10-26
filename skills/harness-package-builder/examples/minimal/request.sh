#!/usr/bin/env bash
# One turn. A CLI-based package would configure and run its CLI here and pipe its output through the mapper; the minimal
# harness is its own "CLI", so the mapper does everything.
set -uo pipefail
exec python3 "$HG_PACKAGE_DIR/agent.py"
