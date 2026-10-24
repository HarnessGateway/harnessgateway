#!/usr/bin/env bash
# OpenClaw needs Node 24 (the base image has Node 22): a private Node under $HG_TOOLS, then openclaw.
set -euo pipefail
VERSION=2026.10.6
NODE_VERSION=24.22.1
case "$(uname -m)" in x86_64) a=x64 ;; aarch64|arm64) a=arm64 ;; *) echo "unsupported arch" >&2; exit 1 ;; esac
mkdir -p "$HG_TOOLS/bin" "$HG_TOOLS/lib"
curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${a}.tar.xz" -o "$HG_TOOLS/node.tar.xz"
mkdir -p "$HG_TOOLS/node" && tar -xJf "$HG_TOOLS/node.tar.xz" -C "$HG_TOOLS/node" --strip-components=1 && rm "$HG_TOOLS/node.tar.xz"
export PATH="$HG_TOOLS/node/bin:$PATH"
npm install --global --prefix "$HG_TOOLS/openclaw" --no-audit --no-fund --strict-allow-scripts --allow-scripts=openclaw,protobufjs,node-pty,koffi,@google/genai "openclaw@$VERSION" \
  || npm install --global --prefix "$HG_TOOLS/openclaw" --no-audit --no-fund "openclaw@$VERSION"
printf '#!/bin/sh\nexport PATH="%s/node/bin:$PATH"\nexec "%s/openclaw/bin/openclaw" "$@"\n' "$HG_TOOLS" "$HG_TOOLS" > "$HG_TOOLS/bin/openclaw"
chmod 755 "$HG_TOOLS/bin/openclaw"
"$HG_TOOLS/bin/openclaw" --version
