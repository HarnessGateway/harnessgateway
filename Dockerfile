# The platform image: the program, and the Docker client its driver starts sandboxes with. `serve` and `proxy`
# both run from it.
FROM python:3.13-slim
COPY --from=docker:28-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
# Exactly what uv.lock pins, then the platform itself.
RUN uv export --quiet --locked --no-dev --no-emit-project --output-file /tmp/requirements.txt \
 && uv pip install --system --no-cache --requirements /tmp/requirements.txt \
 && uv pip install --system --no-cache --no-deps . \
 && cd / && rm -rf /src /tmp/requirements.txt
WORKDIR /
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000 8001 8080
CMD ["harnessgateway", "serve"]
