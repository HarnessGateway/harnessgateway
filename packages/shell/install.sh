#!/usr/bin/env bash
# Nothing to install: bash and coreutils are in the sandbox image. A real package installs its CLI into $HG_TOOLS.
set -euo pipefail
mkdir -p "$HG_TOOLS/bin"
