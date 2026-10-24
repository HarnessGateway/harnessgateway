#!/usr/bin/env bash
set -euo pipefail
VERSION=3.0.73
mkdir -p "$HG_TOOLS/bin" "$HG_TOOLS/lib"
npm install --global --prefix "$HG_TOOLS" --no-audit --no-fund --strict-allow-scripts --allow-scripts=cline,protobufjs "cline@$VERSION"
"$HG_TOOLS/bin/cline" --version
