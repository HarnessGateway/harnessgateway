# The default environment image (spec §7.6): what the harnesses that ship with the platform need, and the
# tools an agent reaches for. Any image can be an environment's image; this one is only the default.
#
#   docker build -f images/sandbox/Dockerfile -t ghcr.io/harnessgateway/sandbox:<version> images/sandbox

FROM node:24-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash build-essential ca-certificates curl git jq less openssh-client procps python3 python3-pip \
      python3-venv ripgrep unzip xz-utils zstd \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10000 --user-group --create-home --home-dir /home/agent --shell /bin/bash agent

USER 10000:10000
WORKDIR /work
