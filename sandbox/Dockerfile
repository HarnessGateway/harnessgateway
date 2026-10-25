# syntax=docker/dockerfile:1.7
# The sandbox base image every harness package runs in, under gVisor, as uid 10001 with a read-only root.
# It provides the common runtimes a coding-agent CLI needs; a package installs its CLI into $HG_TOOLS with install.sh.
#
#   bash, coreutils, findutils, tar, git, curl, jq, ripgrep, procps, unzip, xz
#   python3 (3.11) with venv and pip;  node 22 with npm
#   hgkit, the optional helper library for package mappers, on PYTHONPATH
#
# Build: docker build -t harnessgateway/sandbox:1 sandbox/
# Behind a TLS-intercepting proxy, pass its CA as a build secret (never baked in):
#   docker build --secret id=ca,src=/path/ca.pem ...
FROM node:22-bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive
RUN --mount=type=secret,id=ca,required=false sh -eu <<'EOF'
if [ -s /run/secrets/ca ]; then
  sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources
  install -m 0644 /run/secrets/ca /tmp/hg-build-ca.pem      # apt fetches as _apt, which cannot read the secret
  echo 'Acquire::https::CAInfo "/tmp/hg-build-ca.pem";' > /etc/apt/apt.conf.d/99ca
fi
apt-get update
apt-get install -y --no-install-recommends ca-certificates bash coreutils findutils tar gzip xz-utils unzip git curl jq \
  ripgrep procps less file python3 python3-venv python3-pip tini
rm -f /etc/apt/apt.conf.d/99ca /tmp/hg-build-ca.pem
sed -i 's|https://deb.debian.org|http://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources
rm -rf /var/lib/apt/lists/*
EOF

COPY hgkit /opt/hg/lib/hgkit
RUN useradd -u 10001 -m -d /home/hg -s /bin/bash hg \
 && mkdir -p /workspace /state /run/hg /opt/hg/package /opt/hg/tools /opt/hg/runtime \
 && chown 10001:10001 /workspace /state /home/hg \
 && npm config --global set update-notifier false fund false audit false

ENV PYTHONPATH=/opt/hg/lib PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 NPM_CONFIG_UPDATE_NOTIFIER=false
USER 10001
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
