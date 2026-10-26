#!/usr/bin/env bash
# Hermes Agent 0.19.0 (PyPI hermes-agent, Nous Research) in a venv under $HG_TOOLS, with the extras this package uses:
# anthropic (Anthropic Messages transport), acp (the Agent Client Protocol server run.py drives), mcp (MCP servers
# from the request's tools). Every dependency is pinned (the set pip resolved for 0.19.0 on Python 3.11), so the
# tools layer is reproducible. Self-contained: the platform caches it by the hash of this file.
set -euo pipefail
VERSION=0.19.0
VENV="$HG_TOOLS/hermes"
python3 -m venv "$VENV"
cat > /tmp/hermes-constraints.txt <<'PINS'
agent-client-protocol==0.9.0
annotated-doc==0.0.5
annotated-types==0.8.0
anthropic==0.87.0
anyio==4.15.1
attrs==26.1.0
certifi==2026.5.20
cffi==2.1.1
charset-normalizer==3.5.2
click==8.5.0
croniter==6.0.0
cryptography==46.0.7
distro==1.9.0
docstring_parser==0.18.0
fastapi==0.162.0
fire==0.7.1
h11==0.16.0
httpcore==1.0.9
httptools==0.9.0
httpx==0.28.1
httpx-sse==0.4.3
idna==3.20
Jinja2==3.1.6
jiter==0.18.0
jsonschema==4.26.0
jsonschema-specifications==2025.9.1
Markdown==3.10.2
markdown-it-py==4.2.0
MarkupSafe==3.0.4
mcp==1.26.0
mdurl==0.1.2
openai==2.24.0
opentelemetry-api==1.45.1
packaging==26.0
pathspec==1.1.1
pillow==12.2.0
prompt_toolkit==3.0.52
psutil==7.2.2
ptyprocess==0.7.0
pycparser==3.12
pydantic==2.13.4
pydantic-settings==2.16.1
pydantic_core==2.46.4
Pygments==2.21.0
PyJWT==2.13.0
python-dateutil==3.0.0
python-dotenv==1.2.2
python-multipart==0.0.32
pytz==2026.5
PyYAML==6.0.3
referencing==0.37.0
requests==2.33.0
rich==14.3.3
rpds-py==2026.9.1
ruamel.yaml==0.18.17
ruamel.yaml.clib==0.2.15
six==1.17.0
sniffio==1.3.1
socksio==1.0.0
sse-starlette==3.6.0
starlette==1.0.1
tenacity==9.1.4
termcolor==3.3.0
tqdm==4.70.1
typing-inspection==0.4.4
typing_extensions==4.16.0
tzdata==2026.5
urllib3==2.8.0
uvicorn==0.54.0
uvloop==0.23.0
watchfiles==1.3.0
wcwidth==0.9.4
websockets==15.0.1
PINS
"$VENV/bin/pip" install --quiet --no-cache-dir --disable-pip-version-check --no-input \
  -c /tmp/hermes-constraints.txt "hermes-agent[anthropic,acp,mcp]==$VERSION"
mkdir -p "$HG_TOOLS/bin"
ln -sf ../hermes/bin/hermes "$HG_TOOLS/bin/hermes"
HERMES_HOME=/tmp/hermes-check "$HG_TOOLS/bin/hermes" acp --check
test "$(HERMES_HOME=/tmp/hermes-check "$HG_TOOLS/bin/hermes" acp --version)" = "$VERSION"
