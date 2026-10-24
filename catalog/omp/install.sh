#!/usr/bin/env bash
set -euo pipefail
VERSION=18.12.0
case "$(uname -m)" in x86_64) arch=x64 ;; aarch64|arm64) arch=arm64 ;; *) echo "unsupported arch" >&2; exit 1 ;; esac
mkdir -p "$HG_TOOLS/bin"
curl -fsSL "https://github.com/can1357/oh-my-pi/releases/download/v$VERSION/omp-linux-$arch" -o "$HG_TOOLS/bin/omp"
chmod 755 "$HG_TOOLS/bin/omp"
"$HG_TOOLS/bin/omp" --version
