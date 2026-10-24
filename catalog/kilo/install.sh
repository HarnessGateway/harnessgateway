#!/usr/bin/env bash
set -euo pipefail
VERSION=7.8.12
case "$(uname -m)" in x86_64) arch=x64 ;; aarch64|arm64) arch=arm64 ;; *) echo "unsupported arch" >&2; exit 1 ;; esac
tmp=$(mktemp -d)
curl -fsSL "https://github.com/Kilo-Org/kilocode/releases/download/v$VERSION/kilo-linux-$arch.tar.gz" -o "$tmp/kilo.tgz"
mkdir -p "$tmp/x" && tar -xzf "$tmp/kilo.tgz" -C "$tmp/x"
bin=$(find "$tmp/x" -type f -name kilo -perm -u+x | head -n 1)
dir=$(dirname "$bin")
rm -rf "$HG_TOOLS/kilo" && mkdir -p "$HG_TOOLS/bin" && cp -r "$dir" "$HG_TOOLS/kilo" && chmod -R a+rX "$HG_TOOLS/kilo"
printf '#!/bin/sh\nexec "%s/kilo/kilo" "$@"\n' "$HG_TOOLS" > "$HG_TOOLS/bin/kilo" && chmod 755 "$HG_TOOLS/bin/kilo"
"$HG_TOOLS/bin/kilo" --version
