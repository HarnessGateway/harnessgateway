# syntax=docker/dockerfile:1
# The platform image: one image for every role: `harnessgateway api`, `harnessgateway worker` (the Docker socket
# and root, to hand box directories to the sandbox user), `harnessgateway door`, and the admin commands. The console
# (console/, with the chat) ships in /opt/harnessgateway/console; the two packages the platform ships, in
# /opt/harnessgateway/packages: `shell`, the template the console starts every new harness from, and
# `harness-package-builder`, the built-in `init` installs.
#
# Hardened like the sandbox image: Chainguard's Wolfi base pinned by digest, no package manager, BusyBox, /bin/sh or
# setuid files left in the image (bash is there only to syntax-check package scripts). CI scans it with Trivy on every
# change and weekly (.github/workflows/security.yml); a release attests its build provenance.
#
# Build: docker build -t ghcr.io/harnessgateway/harnessgateway:3.3.1 .
# Behind a TLS-intercepting proxy, pass its CA as a build secret: --secret id=ca,src=/path/ca.pem
FROM cgr.dev/chainguard/wolfi-base@sha256:94fa13e26d449f7d78dbcd02d12ea37a0da80d24fcc3e1f17b95d515dec607a7

USER 0
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY harnessgateway ./harnessgateway
COPY sandbox/hgkit ./sandbox/hgkit
COPY sandbox/hgsnapshot ./sandbox/hgsnapshot
COPY packages ./packages
COPY console ./console
RUN --mount=type=secret,id=ca,required=false sh -eu <<'EOF'
[ -s /run/secrets/ca ] && export SSL_CERT_FILE=/run/secrets/ca PIP_CERT=/run/secrets/ca
apk update
apk add --no-cache python-3.14 uv docker-cli bash tini ca-certificates-bundle    # bash: `bash -n` on package scripts
# the environment exactly as uv.lock pins it: no resolution at build time, so two builds of one commit agree
cd /src && UV_PROJECT_ENVIRONMENT=/opt/harnessgateway UV_NO_CACHE=1 uv sync --frozen --no-dev --no-editable --python /usr/bin/python3.14
cp -r /src/packages /opt/harnessgateway/packages && find /opt/harnessgateway/packages -mindepth 2 -maxdepth 2 -name tests -type d -exec rm -rf {} +   # a package's own tests; its seeds keep theirs
cp -r /src/console /opt/harnessgateway/console
rm -rf /src /var/cache/apk /etc/apk/cache
echo 'harnessgateway:x:10002:10002:harnessgateway:/nonexistent:/sbin/nologin' >> /etc/passwd
echo 'harnessgateway:x:10002:' >> /etc/group
mkdir -p /var/lib/harnessgateway-objects /var/lib/harnessgateway-console
chown 10002:10002 /var/lib/harnessgateway-objects /var/lib/harnessgateway-console   # the file:// object store; the console's drafts
sed -i -E '/^harnessgateway:/!s#:[^:]*$#:/sbin/nologin#' /etc/passwd
# no privilege-raising files; then no shell or package manager (BusyBox provides sh, rm and find: it goes last, and
# its applet links, which apk does not own, after it)
find / -xdev -type f -perm /6000 -exec chmod a-s {} +
apk del --no-cache wolfi-base busybox apk-tools uv
python3.14 -c "import os, pathlib; [p.unlink() for d in ('/bin', '/sbin', '/usr/bin', '/usr/sbin') for p in \
pathlib.Path(d).iterdir() if p.is_symlink() and os.readlink(p).endswith('busybox')]"
EOF

ENV PATH=/opt/harnessgateway/bin:/usr/bin:/bin PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HG_TEMPLATE_DIR=/opt/harnessgateway/packages/shell HG_BUILDER_DIR=/opt/harnessgateway/packages/harness-package-builder
LABEL org.opencontainers.image.title="harnessgateway-platform" \
      org.opencontainers.image.source="https://github.com/HarnessGateway/harnessgateway" \
      org.opencontainers.image.licenses="Apache-2.0"
WORKDIR /
USER 10002
EXPOSE 8000 3128
ENTRYPOINT ["/usr/bin/tini", "--", "harnessgateway"]
CMD ["api"]
