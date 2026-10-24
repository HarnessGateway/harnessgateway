#!/usr/bin/env bash
set -euo pipefail
VERSION=1.3.0
mkdir -p "$HG_TOOLS/bin" "$HG_TOOLS/lib"
npm install --global --prefix "$HG_TOOLS" --no-audit --no-fund --strict-allow-scripts --allow-scripts=@google/genai,protobufjs --no-update-notifier "@earendil-works/pi-coding-agent@$VERSION"
PI_OFFLINE=1 "$HG_TOOLS/bin/pi" --version
