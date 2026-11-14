# syntax=docker/dockerfile:1
# The default environment image (spec §7.6): what the harnesses that ship with the platform need, and the tools
# an agent reaches for. Any image can be an environment's image; this one is only the default.
#
#   bash, coreutils, findutils, grep, sed, gawk, diffutils, patch, tar, gzip, xz, zstd, unzip, file, less, procps,
#   git, openssh-client, curl, jq, ripgrep;  Node.js 26 with npm;  Python 3.14 with pip and venv
#
# Hardened: Chainguard's Wolfi base (glibc, rebuilt daily for security fixes), pinned by digest. No package manager
# and no BusyBox are left in it (the package database stays, so a scanner sees every package), no file raises
# privileges, and one user, 10000, may log in. The platform runs it as that user with a read-only root, no
# capabilities and its own command (spec §7.2). CI scans it with Trivy on every change and every week.
#
#   docker build -f images/sandbox/Dockerfile -t ghcr.io/harnessgateway/sandbox:<version> images/sandbox
#
# On a network that re-signs TLS, add `--secret id=ca,src=<its CA bundle>`: nothing of it stays in the image.
FROM cgr.dev/chainguard/wolfi-base@sha256:1f6b749929164c3d9e7cad0289254f39b2ba51e1dddaeb0fa57532ea03f2db31

USER 0
RUN --mount=type=secret,id=ca,required=false sh -eu <<'EOF'
if [ -s /run/secrets/ca ]; then export SSL_CERT_FILE=/run/secrets/ca; fi
apk update
apk add --no-cache bash coreutils findutils grep sed gawk diffutils patch gnutar gzip xz zstd unzip file less procps \
  git openssh-client curl jq ripgrep nodejs-26 npm python-3.14 py3.14-pip ca-certificates-bundle
[ -e /usr/bin/python3 ] || ln -s python3.14 /usr/bin/python3
npm config --global set update-notifier false fund false audit false
# one shell: bash serves as /bin/sh; then BusyBox and the package manager go
ln -sf bash /bin/sh
find / -xdev -lname '*busybox' -delete
apk del --no-cache wolfi-base busybox apk-tools
rm -rf /var/cache/apk /etc/apk/cache
# no file that raises privileges
find / -xdev -type f -perm /6000 -exec chmod a-s {} +
# one user who may log in: agent, 10000
echo 'agent:x:10000:10000:agent:/home/agent:/bin/bash' >> /etc/passwd
echo 'agent:x:10000:' >> /etc/group
sed -i -E '/^agent:/!s#:[^:]*$#:/sbin/nologin#' /etc/passwd
mkdir -p /home/agent /work
chown 10000:10000 /home/agent /work
EOF

ENV LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 NPM_CONFIG_UPDATE_NOTIFIER=false
LABEL org.opencontainers.image.title="harnessgateway-sandbox" \
      org.opencontainers.image.source="https://github.com/HarnessGateway/harnessgateway" \
      org.opencontainers.image.licenses="Apache-2.0"
USER 10000:10000
WORKDIR /work
