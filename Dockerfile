# syntax=docker/dockerfile:1
# The platform image: the program, and the Docker client its driver starts sandboxes with. `serve` and `proxy`
# both run from it.
#
# The base is Chainguard's Wolfi, pinned by digest: its packages (Python, the Docker client) are rebuilt as fixes
# land, which is what lets the scan in CI pass, on every change and every week. The Python environment is exactly
# what uv.lock pins, so two builds of one commit agree. uv stays in the image: tests/run.sh adds pytest with it.
FROM cgr.dev/chainguard/wolfi-base@sha256:be019c1c74fe69204a42c4e10bc4d886a49bf5403ffa74374ef7baa408fc9a26

USER 0
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY console ./console
RUN apk add --no-cache python-3.13 uv docker-cli ca-certificates-bundle \
 && UV_PROJECT_ENVIRONMENT=/opt/harnessgateway UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never \
      uv sync --locked --no-dev --no-editable --python /usr/bin/python3.13 \
 && cd / && rm -rf /src

WORKDIR /
ENV PATH=/opt/harnessgateway/bin:/usr/bin:/bin PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
LABEL org.opencontainers.image.title="harnessgateway" \
      org.opencontainers.image.source="https://github.com/HarnessGateway/harnessgateway" \
      org.opencontainers.image.licenses="Apache-2.0"
EXPOSE 8000 8001 8080
CMD ["harnessgateway", "serve"]
