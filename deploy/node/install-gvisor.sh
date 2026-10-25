#!/usr/bin/env bash
# Prepare a node host: Docker plus gVisor (runsc), registered as a Docker runtime. Run as root on a Linux host.
# Idempotent. Current gVisor releases ship as one tarball holding runsc, its shim and the gvisor-bin sidecars, which
# must sit next to runsc.
#
#   sudo ./install-gvisor.sh                          # the latest release
#   sudo GVISOR_RELEASE=20261019 ./install-gvisor.sh  # a pinned release (recommended for fleets)
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
ARCH=$(uname -m)
RELEASE=${GVISOR_RELEASE:-latest}
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
command -v bzip2 >/dev/null || { apt-get update && apt-get install -y bzip2; }
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
base="https://storage.googleapis.com/gvisor/releases/release/${RELEASE}/${ARCH}"
curl -fsSL -o "$tmp/gvisor.tar.bz2" "$base/gvisor.tar.bz2"
curl -fsSL -o "$tmp/gvisor.tar.bz2.sha512" "$base/gvisor.tar.bz2.sha512"
(cd "$tmp" && sha512sum -c gvisor.tar.bz2.sha512)
tar -xjf "$tmp/gvisor.tar.bz2" -C "$tmp"
install -m 0755 "$tmp/runsc" "$tmp/containerd-shim-runsc-v1" /usr/local/bin/
rm -rf /usr/local/bin/gvisor-bin && cp -r "$tmp/gvisor-bin" /usr/local/bin/gvisor-bin
/usr/local/bin/runsc install -- --platform=systrap
systemctl restart docker 2>/dev/null || service docker restart
docker run --rm --runtime=runsc hello-world >/dev/null
echo "gVisor ready: $(/usr/local/bin/runsc --version | head -1)"
