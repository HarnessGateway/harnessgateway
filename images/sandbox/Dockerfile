# The default environment image (spec §7.6): what the harnesses that ship with the platform need, and the
# tools an agent reaches for. Any image can be an environment's image; this one is only the default.
#
#   docker build -f images/sandbox/Dockerfile -t ghcr.io/harnessgateway/sandbox:<version> images/sandbox

# Debian 13: its Python is 3.13. The platform's own package checker, which a package author may install in
# a sandbox, needs 3.12 or later.
FROM node:24-trixie-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash build-essential ca-certificates curl git jq less openssh-client procps python3 python3-pip \
      python3-venv ripgrep unzip xz-utils zstd \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10000 --user-group --create-home --home-dir /home/agent --shell /bin/bash agent

USER 10000:10000
WORKDIR /work
