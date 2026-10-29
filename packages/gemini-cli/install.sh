#!/usr/bin/env bash
# Installs Google's Gemini CLI, pinned, into $HG_TOOLS (bin/gemini, a node script), the way its README documents
# (`npm install -g @google/gemini-cli`; https://github.com/google-gemini/gemini-cli). npm picks the optional prebuilt
# node-pty for the machine where one is published (the shell tool falls back to plain child processes without it);
# no dependency install script is needed, so all are skipped. Fails unless `gemini --version` prints $VERSION.
set -euo pipefail
VERSION=0.65.0
npm install --global --prefix "$HG_TOOLS" --no-audit --no-fund --no-update-notifier --ignore-scripts \
  "@google/gemini-cli@$VERSION"
got=$(HOME="$(mktemp -d)" "$HG_TOOLS/bin/gemini" --version)
echo "installed: gemini $got"
[ "$got" = "$VERSION" ] || { echo "expected Gemini CLI $VERSION, got: $got" >&2; exit 1; }
