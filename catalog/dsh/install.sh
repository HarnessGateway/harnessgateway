#!/usr/bin/env bash
set -euo pipefail
VERSION=0.2.0-rc.2
mkdir -p "$HG_TOOLS/bin" "$HG_TOOLS/lib"
npm install --global --prefix "$HG_TOOLS" --no-audit --no-fund --strict-allow-scripts --allow-scripts=@deepseek-ai/dsh-subprocess-local,koffi,node-pty,@google/genai,protobufjs "@deepseek-ai/dsh@$VERSION"
"$HG_TOOLS/bin/dsh" --version </dev/null
