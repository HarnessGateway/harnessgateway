#!/usr/bin/env bash
# Installs Pi into $HG_TOOLS, pinned. The only install scripts in the tree are protobufjs's postinstall (a version
# warning) and @google/genai's no-op preinstall, so dependency scripts are skipped altogether.
set -euo pipefail
VERSION=1.4.0
npm install --global --prefix "$HG_TOOLS" --no-audit --no-fund --no-update-notifier --ignore-scripts \
  "@earendil-works/pi-coding-agent@$VERSION"
HOME="$(mktemp -d)" PI_OFFLINE=1 PI_TELEMETRY=0 PI_SKIP_VERSION_CHECK=1 "$HG_TOOLS/bin/pi" --version
