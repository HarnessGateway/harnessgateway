#!/usr/bin/env bash
set -euo pipefail
VERSION=2.1.310
mkdir -p "$HG_TOOLS/bin" "$HG_TOOLS/lib"
npm install --global --prefix "$HG_TOOLS" --no-audit --no-fund --strict-allow-scripts --allow-scripts=@anthropic-ai/claude-code "@anthropic-ai/claude-code@$VERSION"
"$HG_TOOLS/bin/claude" --version
