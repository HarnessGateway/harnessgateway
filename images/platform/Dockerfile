# The platform image (spec §18.2): the program, the Docker command-line client, the kit, and the packages
# that ship with the platform. Steps run from this same image. Build it from the repository's root:
#
#   docker build -f images/platform/Dockerfile -t ghcr.io/harnessgateway/harnessgateway:<version> .

FROM python:3.13-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY harnessgateway harnessgateway
# On a network that re-signs TLS, add `--secret id=ca,src=<its CA bundle>` to the build: pip then trusts it for
# this step, and nothing of it stays in the image.
RUN --mount=type=secret,id=ca,required=false \
    if [ -s /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    pip install --no-cache-dir --prefix /install .

FROM docker:28-cli AS docker

FROM python:3.13-slim
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY --from=build /install /usr/local
COPY packages /packages
ENV PYTHONUNBUFFERED=1
EXPOSE 8000 8001 8080
CMD ["harnessgateway", "serve"]
