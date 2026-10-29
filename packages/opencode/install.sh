#!/usr/bin/env bash
# Installs OpenCode 1 the way its docs give for Node.js (https://opencode.ai/docs: `npm install -g opencode-ai`), pinned,
# into $HG_TOOLS. opencode-ai's postinstall picks the native binary that runs on this machine from its optional
# dependencies (npm installs the ones whose os/cpu match) and links it to the package's bin/, so that one script is
# allowed. The installed tree is left as npm and the postinstall made it; no CPU is named here, and the result is
# checked by running the CLI.
set -euo pipefail
VERSION=1.19.4
npm install --global --prefix "$HG_TOOLS" --no-audit --no-fund --no-update-notifier --allow-scripts=opencode-ai \
  "opencode-ai@$VERSION"
got=$(HOME="$(mktemp -d)" OPENCODE_DISABLE_AUTOUPDATE=1 "$HG_TOOLS/bin/opencode" --version)
echo "installed: opencode $got"
test "$got" = "$VERSION"
