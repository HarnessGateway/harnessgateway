#!/usr/bin/env bash
# Puts the CLI into $HG_TOOLS, once per package version. foobar ships inside this package, so the
# install is a copy; a real package runs `npm install --global --prefix "$HG_TOOLS" …` or a pip
# install here, with egress to install.egress only.
set -euo pipefail
mkdir -p "$HG_TOOLS/bin"
cp "$HG_HARNESS_DIR/bin/foobar" "$HG_TOOLS/bin/foobar"
chmod +x "$HG_TOOLS/bin/foobar"
"$HG_TOOLS/bin/foobar" --version
