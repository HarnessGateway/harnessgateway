#!/usr/bin/env bash
# Nothing to install: bash, coreutils, git, curl, jq, ripgrep, Python 3.12 and Node 22 are in the
# base image. Fork this package and add the CLI of your own here (under $HG_TOOLS, see the template).
set -euo pipefail
mkdir -p "$HG_TOOLS/bin"
