#!/usr/bin/env bash
# Installs Anthropic's Claude Code CLI, pinned, into $HG_TOOLS (bin/claude). The npm package's postinstall places the
# native linux binary (from the matching optional dependency) over bin/claude.exe, so no node process stays resident.
# npm 12 runs no install scripts unless allowed: only that one is.
set -euo pipefail
VERSION=2.1.310
mkdir -p "$HG_TOOLS/bin"
npm install --global --prefix "$HG_TOOLS" --no-audit --no-fund --no-update-notifier \
  --allow-scripts=@anthropic-ai/claude-code "@anthropic-ai/claude-code@$VERSION"
got=$("$HG_TOOLS/bin/claude" --version)
echo "installed: $got"
case "$got" in "$VERSION "*) ;; *) echo "expected Claude Code $VERSION, got: $got" >&2; exit 1 ;; esac
