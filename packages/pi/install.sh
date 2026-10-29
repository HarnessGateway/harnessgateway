#!/usr/bin/env bash
# Installs Pi into $HG_TOOLS, pinned, by the npm route its README documents
# (`npm install -g --ignore-scripts @earendil-works/pi-coding-agent`; https://github.com/earendil-works/pi). The only
# install scripts in the tree are protobufjs's postinstall (a version warning) and @google/genai's no-op preinstall,
# so dependency scripts are skipped, as the README's command does.
set -euo pipefail
VERSION=1.4.0
npm install --global --prefix "$HG_TOOLS" --no-audit --no-fund --no-update-notifier --ignore-scripts \
  "@earendil-works/pi-coding-agent@$VERSION"
got=$(HOME="$(mktemp -d)" PI_OFFLINE=1 PI_TELEMETRY=0 PI_SKIP_VERSION_CHECK=1 "$HG_TOOLS/bin/pi" --version 2>&1)
echo "installed: pi $got"
case "$got" in *"$VERSION"*) ;; *) echo "expected Pi $VERSION, got: $got" >&2; exit 1 ;; esac
