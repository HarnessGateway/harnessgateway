#!/usr/bin/env bash
set -euo pipefail
VERSION=0.163.0
mkdir -p "$HG_TOOLS/bin" "$HG_TOOLS/lib"
npm install --global --prefix "$HG_TOOLS" --no-audit --no-fund --strict-allow-scripts "@openai/codex@$VERSION"
"$HG_TOOLS/bin/codex" --version
