# syntax=docker/dockerfile:1
# The platform image (spec §18.2): the program, the Docker command-line client, the kit, and the packages that ship
# with the platform. `serve`, `proxy` and the steps all run from it. Build it from the repository's root:
#
#   docker build -f images/platform/Dockerfile -t ghcr.io/harnessgateway/harnessgateway:<version> .
#
# Hardened like the sandbox image: Chainguard's Wolfi base (glibc, rebuilt daily for security fixes), pinned by
# digest. Its Python environment is exactly what uv.lock pins, so two builds of one commit agree. No package
# manager or BusyBox is left in it (the package database stays, so a scanner sees every package), and no file
# raises privileges. bash and coreutils stay: `harnessgateway package check` runs a package's scripts. It runs as
# root: `serve` drives Docker through its socket, and the steps hand files to the sandbox's user. CI scans it with
# Trivy on every change and every week.
#
# On a network that re-signs TLS, add `--secret id=ca,src=<its CA bundle>`: nothing of it stays in the image.
FROM cgr.dev/chainguard/wolfi-base@sha256:1f6b749929164c3d9e7cad0289254f39b2ba51e1dddaeb0fa57532ea03f2db31

USER 0
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY harnessgateway ./harnessgateway
COPY packages /packages
RUN --mount=type=secret,id=ca,required=false sh -eu <<'EOF'
if [ -s /run/secrets/ca ]; then export SSL_CERT_FILE=/run/secrets/ca; fi
apk update
apk add --no-cache python-3.13 uv docker-cli bash coreutils findutils ca-certificates-bundle
[ -e /usr/bin/python3 ] || ln -s python3.13 /usr/bin/python3
UV_PROJECT_ENVIRONMENT=/opt/harnessgateway UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never \
  uv sync --frozen --no-dev --no-editable --python /usr/bin/python3.13
cd / && rm -rf /src /var/cache/apk /etc/apk/cache
# one shell: bash serves as /bin/sh; then BusyBox and the package manager go; then no file that raises privileges
ln -sf bash /bin/sh
find / -xdev -lname '*busybox' -delete
apk del --no-cache wolfi-base busybox apk-tools uv
find / -xdev -type f -perm /6000 -exec chmod a-s {} +
EOF

ENV PATH=/opt/harnessgateway/bin:/usr/bin:/bin PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
LABEL org.opencontainers.image.title="harnessgateway" \
      org.opencontainers.image.source="https://github.com/HarnessGateway/harnessgateway" \
      org.opencontainers.image.licenses="Apache-2.0"
WORKDIR /
EXPOSE 8000 8001 8080
CMD ["harnessgateway", "serve"]
