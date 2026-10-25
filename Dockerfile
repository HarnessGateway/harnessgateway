# syntax=docker/dockerfile:1.7
# The platform image: one image, three roles (api, gateway, node), chosen with `harnessgateway serve --role ...`.
# The api and gateway run as an unprivileged user. The node needs the Docker socket and root to hand bind mounts to the
# sandbox user, so compose and the node install run it as root; it also runs each sandbox's egress door from this image.
#
# Hardened like the sandbox image: Chainguard's Wolfi base pinned by digest, no package manager, BusyBox, /bin/sh or
# setuid files left in the image (bash is there only to syntax-check package scripts). CI scans it with Trivy on every
# change and weekly (.github/workflows/images.yml).
#
# Build: docker build -t harnessgateway/platform:1 .
# Behind a TLS-intercepting proxy, pass its CA as a build secret: --secret id=ca,src=/path/ca.pem
FROM cgr.dev/chainguard/wolfi-base@sha256:360896efb0753a5701c48f860e17130c205eae5a264797aa3e7ea126ee2923a7

USER 0
WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY harnessgateway ./harnessgateway
COPY packages ./packages
RUN --mount=type=secret,id=ca,required=false sh -eu <<'EOF'
[ -s /run/secrets/ca ] && export SSL_CERT_FILE=/run/secrets/ca PIP_CERT=/run/secrets/ca
apk update
apk add --no-cache python-3.12 docker-cli git bash tini ca-certificates-bundle    # bash: `bash -n` on package scripts
python3.12 -m venv /opt/harnessgateway
/opt/harnessgateway/bin/pip install --no-cache-dir --disable-pip-version-check /src
/opt/harnessgateway/bin/pip uninstall -y pip
rm -rf /src /var/cache/apk /etc/apk/cache
echo 'harnessgateway:x:10002:10002:harnessgateway:/nonexistent:/sbin/nologin' >> /etc/passwd
echo 'harnessgateway:x:10002:' >> /etc/group
sed -i -E '/^harnessgateway:/!s#:[^:]*$#:/sbin/nologin#' /etc/passwd
# no privilege-raising files; then no shell or package manager (BusyBox provides sh, rm and find: it goes last, and
# its applet links, which apk does not own, after it)
find / -xdev -type f -perm /6000 -exec chmod a-s {} +
apk del --no-cache wolfi-base busybox apk-tools
python3.12 -c "import os, pathlib; [p.unlink() for d in ('/bin', '/sbin', '/usr/bin', '/usr/sbin') for p in \
pathlib.Path(d).iterdir() if p.is_symlink() and os.readlink(p).endswith('busybox')]"
EOF

ENV PATH=/opt/harnessgateway/bin:/usr/bin:/bin PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
LABEL org.opencontainers.image.title="harnessgateway-platform" \
      org.opencontainers.image.source="https://github.com/HarnessGateway/harnessgateway" \
      org.opencontainers.image.licenses="Apache-2.0"
WORKDIR /
USER 10002
EXPOSE 8000 8081
ENTRYPOINT ["/usr/bin/tini", "--", "harnessgateway"]
CMD ["serve", "--role", "api"]
