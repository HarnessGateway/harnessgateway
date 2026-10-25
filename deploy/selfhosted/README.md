# Self-hosted HarnessGateway

The same platform as the managed service, on one machine you own: Postgres, an S3 server, the api and gateway roles,
and a node that runs gVisor sandboxes. Organizations, keys, metering and every API are identical to SaaS.

## Requirements

- Linux x86_64 or arm64 with Docker 24+ and **gVisor** (`runsc`). `../node/install-gvisor.sh` installs and registers it.
- 4 vCPUs and 8 GB of memory as a starting point (each sandbox gets 2 vCPUs and 4 GB by default; the operator changes
  that per organization).
- Outbound HTTPS to your model providers and to the package registries your harnesses install from (npm, PyPI).

## Install

```bash
sudo ../node/install-gvisor.sh
cp .env.example .env
docker run --rm harnessgateway/platform:1 keygen >> .env     # HG_SECRET_KEY, HG_OPERATOR_KEY, HG_NODE_JOIN_TOKEN
$EDITOR .env                                                # database and S3 passwords
docker compose up -d
docker compose exec api harnessgateway create-org acme you@acme.io   # prints the organization's admin key once
```

Then, with the admin key:

```bash
curl -X PUT http://localhost:8000/v1/providers/anthropic -H "Authorization: Bearer $KEY" \
  -d '{"api": "anthropic", "base_url": "https://api.anthropic.com", "key": "sk-ant-..."}'
python - <<'PY'
from openai import OpenAI
c = OpenAI(base_url="http://localhost:8000/v1", api_key="sk-hg-...")
print(c.responses.create(model="claude-code/anthropic/claude-sonnet-4-5", input="Write hello.py and run it").output_text)
PY
```

## Verify

`deploy/verify-harnesses.py` signs up a throwaway organization through an invite, installs the built-in harnesses and
runs eight checks on each (file in the sandbox, recall, streaming, instructions, usage, cancel, provider error, traces):

```bash
pip install openai httpx
HG_API=http://localhost:8000 HG_OPERATOR_KEY=... DEEPSEEK_API_KEY=... OPENAI_API_KEY=... python deploy/verify-harnesses.py
```

## TLS and exposure

The api binds to 127.0.0.1 by default. To serve other machines, put a TLS-terminating proxy in front (Caddy, nginx,
a cloud load balancer) and set `HG_BIND=0.0.0.0` only behind it. The gateway port (8081) never needs to be exposed:
sandboxes reach it on the internal `harnessgateway` network.

## Operations

- **Upgrade**: `docker compose pull && docker compose up -d`. The api applies database migrations on start.
- **Backup**: the `postgres` and `objects` volumes, plus `.env` (without `HG_SECRET_KEY` the stored provider keys
  cannot be decrypted).
- **More capacity**: run more nodes on other machines with `../node/run-node.sh`, pointed at this api.
- **Invite-only sign-up**: `docker compose exec api harnessgateway invite --uses 5 --label pilot` prints a code an
  organization redeems at `POST /v1/signup`.
- Logs are JSON lines: `docker compose logs -f api node`.
