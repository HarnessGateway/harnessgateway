<p align="center"><img src="docs/assets/picky.png" width="140" alt="Picky, the HarnessGateway hermit crab"></p>

# HarnessGateway

Run any coding-agent harness (Claude Code, Codex, Hermes, Pi, or your own) in a platform-owned gVisor sandbox, behind
the **OpenAI Responses API**. One platform serves managed multi-tenant SaaS, self-hosted and hybrid deployments.

```python
from openai import OpenAI

client = OpenAI(base_url="https://api.harnessgateway.ai/v1", api_key="sk-hg-...")
first = client.responses.create(model="claude-code/anthropic/claude-sonnet-4-5",
                                input="Add a /health endpoint to app.py and run the tests.")
print(first.output_text)
follow_up = client.responses.create(model="claude-code", previous_response_id=first.id,
                                    input="Now add a test for it.")
```

## How it works

- **No schema of our own.** The client's Responses request goes, verbatim, to a **harness package**; the package runs
  its CLI and prints Responses events, which the platform validates and streams back. The package decides how its
  harness uses every field: it is the organization's operational IP.
- **The platform owns the sandbox.** Each session gets a gVisor sandbox with its own workspace, home and state, kept
  between turns. Outbound traffic only reaches the package's allowed hosts; provider keys never enter the sandbox (a
  gateway swaps a per-turn token for the organization's key).
- **Bring your own providers.** Organizations register their Anthropic- or OpenAI-shaped endpoints and pay them
  directly. The platform bills only the compute, network and storage its sessions use, at unit cost times the markup.
- **Packages are open.** Built-in packages ship with the platform; organizations upload their own or install any public
  git repository (the free marketplace). The [Harness Package Builder skill](skills/harness-package-builder/SKILL.md)
  teaches an agent (or a person) to build one.

## Built-in harnesses

| Harness | Package | Provider shapes |
|---|---|---|
| Claude Code 2.1.310 | `packages/claude-code` | anthropic |
| OpenAI Codex 0.164.0 | `packages/codex` | openai (Responses API) |
| Hermes Agent 0.19.0 | `packages/hermes` | anthropic, openai (chat completions) |
| Pi 1.4.0 | `packages/pi` | anthropic, openai (Responses or chat completions) |
| shell (reference, no model) | `packages/shell` | none |

## Run it

- Self-hosted on one machine: [`deploy/selfhosted`](deploy/selfhosted/README.md)
- Managed SaaS and hybrid: [`deploy/saas`](deploy/saas/README.md)
- Development: `uv venv && uv pip install -e '.[dev]' && pytest tests/unit`

## Documentation

[Architecture](docs/architecture.md) · [API](docs/api.md) · [Package contract](docs/package-contract.md) ·
[Operations](docs/operations.md) · [Billing](docs/billing.md) · [Security](docs/security.md)

Licensed under the Apache License 2.0.
