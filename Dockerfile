# syntax=docker/dockerfile:1.7
# The platform image: one image, three roles (api, gateway, node), chosen with `harnessgateway serve --role ...`.
# The api and gateway run as an unprivileged user. The node needs the Docker socket and root to hand bind mounts to the
# sandbox user, so compose and the node install run it as root; it also runs each sandbox's egress door from this image.
#
# Build: docker build -t harnessgateway/platform:1 .
# Behind a TLS-intercepting proxy, pass its CA as a build secret: --secret id=ca,src=/path/ca.pem
FROM docker:27-cli AS dockercli

FROM python:3.12-slim-bookworm
ARG DEBIAN_FRONTEND=noninteractive
RUN --mount=type=secret,id=ca,required=false sh -eu <<'EOF'
if [ -s /run/secrets/ca ]; then
  sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources
  install -m 0644 /run/secrets/ca /tmp/hg-build-ca.pem      # apt fetches as _apt, which cannot read the secret
  echo 'Acquire::https::CAInfo "/tmp/hg-build-ca.pem";' > /etc/apt/apt.conf.d/99ca
fi
apt-get update
apt-get install -y --no-install-recommends ca-certificates git tini
rm -f /etc/apt/apt.conf.d/99ca /tmp/hg-build-ca.pem
sed -i 's|https://deb.debian.org|http://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources
rm -rf /var/lib/apt/lists/*
EOF
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY harnessgateway ./harnessgateway
COPY packages ./packages
RUN --mount=type=secret,id=ca,required=false sh -eu <<'EOF'
[ -s /run/secrets/ca ] && export PIP_CERT=/run/secrets/ca
pip install --no-cache-dir .
EOF
RUN useradd -u 10002 -r -s /usr/sbin/nologin harnessgateway
ENV PYTHONUNBUFFERED=1
USER 10002
EXPOSE 8000 8081
ENTRYPOINT ["/usr/bin/tini", "--", "harnessgateway"]
CMD ["serve", "--role", "api"]
