#!/usr/bin/env bash
set -euo pipefail
VERSION=1.88.0
mkdir -p "$HG_TOOLS/bin" "$HG_TOOLS/lib"
npm install --global --prefix "$HG_TOOLS" --no-audit --no-fund --strict-allow-scripts "command-code@$VERSION"
"$HG_TOOLS/bin/command-code" --version
