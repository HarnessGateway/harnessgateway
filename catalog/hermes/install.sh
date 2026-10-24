#!/usr/bin/env bash
set -euo pipefail
VERSION=0.19.0
mkdir -p "$HG_TOOLS/bin"
python3 -m venv "$HG_TOOLS/hermes-venv"
"$HG_TOOLS/hermes-venv/bin/pip" install -q --disable-pip-version-check "hermes-agent[anthropic]==$VERSION"
ln -sf ../hermes-venv/bin/hermes "$HG_TOOLS/bin/hermes"
"$HG_TOOLS/bin/hermes" --version
