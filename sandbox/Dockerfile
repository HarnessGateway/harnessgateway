# syntax=docker/dockerfile:1.7
# The sandbox base image every harness package runs in: under gVisor, as uid 10001, with a read-only root, no
# capabilities and no-new-privileges (all set by the node). It provides the common runtimes a coding-agent CLI needs;
# a package installs its CLI into $HG_TOOLS with install.sh.
#
#   bash, coreutils, findutils, grep, sed, gawk, diffutils, patch, tar, gzip, xz, unzip, file, less, procps,
#   setsid, which, flock, git, curl, jq, ripgrep;  python 3.12 with venv and pip;  node 24 (LTS) with npm 12;
#   libatomic (the C runtime library official Node.js builds link against, which some CLIs' installers fetch);
#   tini as PID 1
#   hgkit, the optional helper library for package mappers, on PYTHONPATH
#
# Hardened: Chainguard's Wolfi base (glibc, rebuilt daily for CVE fixes) pinned by digest; no setuid or setgid files;
# no package manager or BusyBox left in the image (the apk database stays, so scanners see every package); one user.
# CI scans it with Trivy on every change and weekly (.github/workflows/images.yml).
#
# Build: docker build -t ghcr.io/harnessgateway/sandbox:1.0.1 sandbox/
# Behind a TLS-intercepting proxy, pass its CA as a build secret (never baked in):
#   docker build --secret id=ca,src=/path/ca.pem ...
FROM cgr.dev/chainguard/wolfi-base@sha256:360896efb0753a5701c48f860e17130c205eae5a264797aa3e7ea126ee2923a7

USER 0
RUN --mount=type=secret,id=ca,required=false sh -eu <<'EOF'
[ -s /run/secrets/ca ] && export SSL_CERT_FILE=/run/secrets/ca
apk update
apk add --no-cache bash coreutils findutils grep sed gawk diffutils patch gnutar gzip xz unzip file less procps \
  util-linux-misc debianutils flock git curl jq ripgrep python-3.12 py3.12-pip nodejs-24 npm libatomic tini ca-certificates-bundle
[ -e /usr/bin/python3 ] || ln -s python3.12 /usr/bin/python3
# one shell: bash serves as /bin/sh, then BusyBox and the package manager go
ln -sf bash /bin/sh
find / -xdev -lname '*busybox' -delete                    # its applet links are not the package's to remove
apk del --no-cache wolfi-base busybox apk-tools
rm -rf /var/cache/apk /etc/apk/cache
# no privilege-raising files
find / -xdev -type f -perm /6000 -exec chmod a-s {} +
# one user: hg (10001); nothing else may log in
echo 'hg:x:10001:10001:hg:/home/hg:/bin/bash' >> /etc/passwd
echo 'hg:x:10001:' >> /etc/group
sed -i -E '/^hg:/!s#:[^:]*$#:/sbin/nologin#' /etc/passwd
mkdir -p /home/hg /workspace /state /run/hg /opt/hg/package /opt/hg/tools /opt/hg/runtime
chown 10001:10001 /workspace /state /home/hg
npm config --global set update-notifier false fund false audit false
EOF

COPY hgkit /opt/hg/lib/hgkit
ENV PYTHONPATH=/opt/hg/lib PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 NPM_CONFIG_UPDATE_NOTIFIER=false \
    HOME=/home/hg LANG=C.UTF-8
LABEL org.opencontainers.image.title="harnessgateway-sandbox" \
      org.opencontainers.image.source="https://github.com/HarnessGateway/harnessgateway" \
      org.opencontainers.image.licenses="Apache-2.0"
USER 10001
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
