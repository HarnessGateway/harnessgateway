# The platform's sandbox base image: Debian, Python 3.12, Node 22, git, curl, ripgrep, procps, tmux.
# Nothing of the platform and nothing of any package is in it; packages install their CLI into the
# tools layer at first use. The base-image workflow builds it for amd64 and arm64, scans it and
# publishes it as ghcr.io/harnessgateway/harnessgateway-base; a node pulls that when HG_BASE_IMAGE is
# empty, and builds this file itself only when the pull fails.
FROM python:3.12-slim-bookworm
ARG NODE_VERSION=22.23.4
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git ripgrep procps tmux xz-utils unzip jq less \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; arch="$(uname -m)"; case "$arch" in x86_64) a=x64 ;; aarch64) a=arm64 ;; *) echo "unsupported $arch"; exit 1 ;; esac; \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${a}.tar.xz" -o /tmp/node.tar.xz; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner; rm /tmp/node.tar.xz; \
    npm install -g npm@latest && npm cache clean --force; \
    node --version; npm --version; python3 --version
