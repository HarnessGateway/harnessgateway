# syntax=docker/dockerfile:1
# The sandbox image every box runs: under gVisor, as uid 10001, with a read-only root, no capabilities and
# no-new-privileges (all set by the launcher). It provides the common runtimes a coding-agent CLI needs; a package
# installs its CLI into $HG_TOOLS with its `install`, in an install box, and the archive is restored into each box.
#
#   bash, coreutils, findutils, grep, sed, gawk, diffutils, patch, tar, gzip, xz, zstd, unzip, file, less, procps,
#   setsid, which, flock, git, curl, jq, ripgrep;  python 3.14 with venv and pip;  node 26 (LTS) with npm;
#   libatomic (the C runtime library official Node.js builds link against, which some CLIs' installers fetch);
#   tini as PID 1; the runner (/opt/hg/runner.py) as the container's command
#   hgkit and hgsnapshot, on PYTHONPATH, with the `openai` SDK (pinned to the platform's) so a package can validate
#   its own output against the Responses schema before the platform does
#
# Hardened: Chainguard's Wolfi base (glibc, rebuilt daily for CVE fixes) pinned by digest; no setuid or setgid files;
# no package manager or BusyBox left in the image (the apk database stays, so scanners see every package); one user.
# CI scans it with Trivy on every change and weekly (.github/workflows/security.yml).
#
# Build (from the repository root): docker build -t ghcr.io/harnessgateway/sandbox:3.2.0 -f sandbox/Dockerfile .
# Behind a TLS-intercepting proxy, pass its CA as a build secret (never baked in): --secret id=ca,src=/path/ca.pem
FROM cgr.dev/chainguard/wolfi-base@sha256:8509ee4351a2e501b7f98201f2e2642396e82020c61a589cacdcfe74b9de46c1

USER 0
RUN --mount=type=secret,id=ca,required=false sh -eu <<'EOF'
[ -s /run/secrets/ca ] && export SSL_CERT_FILE=/run/secrets/ca PIP_CERT=/run/secrets/ca
apk update
apk add --no-cache bash coreutils findutils grep sed gawk diffutils patch gnutar gzip xz zstd unzip file less procps \
  util-linux-misc debianutils flock git curl jq ripgrep python-3.14 py3.14-pip nodejs-26 npm libatomic tini ca-certificates-bundle
[ -e /usr/bin/python3 ] || ln -s python3.14 /usr/bin/python3
python3 -m pip install --no-cache-dir --disable-pip-version-check openai==3.46.0    # hgkit's validator
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
mkdir -p /home/hg /workspace /state /run/hg /opt/hg/package /opt/hg/tools /opt/hg/runtime /tmp/hg/blobs
chown 10001:10001 /workspace /state /home/hg /run/hg /opt/hg/package /opt/hg/tools /opt/hg/runtime
npm config --global set update-notifier false fund false audit false
EOF

COPY sandbox/hgkit /opt/hg/lib/hgkit
COPY sandbox/hgsnapshot /opt/hg/lib/hgsnapshot
COPY sandbox/runner.py /opt/hg/runner.py
ENV PYTHONPATH=/opt/hg/lib PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 NPM_CONFIG_UPDATE_NOTIFIER=false \
    HOME=/home/hg LANG=C.UTF-8 PATH=/opt/hg/tools/bin:/opt/hg/tools/node_modules/.bin:/usr/local/bin:/usr/bin:/bin
LABEL org.opencontainers.image.title="harnessgateway-sandbox" \
      org.opencontainers.image.source="https://github.com/HarnessGateway/harnessgateway" \
      org.opencontainers.image.licenses="Apache-2.0"
USER 10001
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/opt/hg/runner.py"]
