#!/usr/bin/env bash
# Installs Nous Research's Hermes Agent by its official installer (the one Linux method its installation docs give:
# curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash), pinned with the installer's own --commit to the
# v0.22.5 release, into $HG_TOOLS. The installer puts everything under $HOME (the checkout, its managed Python, Node
# and tools, and the dependency environment its install record selects), so it runs with HOME=$HG_TOOLS/home and all
# of it lands in the tools layer. --non-interactive skips setup and the shell-profile prompt; the browser and
# computer-use drivers are skipped (no display in the sandbox). Hermes installs the Anthropic SDK lazily on first use,
# which a read-only tools layer cannot do, so `hermes pm install --extra anthropic` (Hermes's own command) adds it now.
set -euo pipefail
VERSION=0.22.5
COMMIT=bf0203a794a07aac3ff764972ab9087baa4e4f4e      # tag v0.22.5
export HOME="$HG_TOOLS/home"
# uv opens dozens of downloads at once, and through the sandbox's egress proxy some of them time out connecting.
# Hermes's package manager does not pass uv's concurrency settings on (only index and transport ones, such as
# UV_HTTP_TIMEOUT), and its remedy for a failed download is to run the installer (or `hermes pm install`) again: that
# resumes over the checkout and the uv cache already in $HOME, so each try downloads only what is still missing.
export UV_HTTP_TIMEOUT=120
tries() {
  local n
  for n in 1 2 3 4 5 6; do
    "$@" && return 0
    echo "failed (try $n of 6): $*" >&2
  done
  return 1
}
curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /tmp/hermes-install.sh
tries bash /tmp/hermes-install.sh --commit "$COMMIT" --non-interactive --skip-browser --skip-computer-use
hermes="$HOME/.local/bin/hermes"
tries "$hermes" pm install --extra anthropic
got=$("$hermes" --version 2>/dev/null | head -n 1)
echo "installed: $got"
case "$got" in "Hermes Agent v$VERSION "*) ;; *) echo "expected Hermes Agent $VERSION, got: $got" >&2; exit 1 ;; esac
