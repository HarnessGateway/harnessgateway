#!/usr/bin/env bash
# The minimal harness needs only python3 (in the sandbox image). A real package installs its pinned CLI here, e.g.:
#   npm install -g --prefix "$HG_TOOLS" --no-audit --no-fund some-cli@1.2.3 && "$HG_TOOLS/bin/some-cli" --version
set -euo pipefail
mkdir -p "$HG_TOOLS/bin"
python3 --version
