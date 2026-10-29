<p align="center"><img src="docs/mascot.png" width="220" alt="The HarnessGateway mascot, a hermit crab whose shell carries a circuit trace"></p>

# HarnessGateway

**Coding-agent harnesses as an API.** Send a standard OpenAI Responses request; HarnessGateway runs it through a
harness package (Claude Code, Codex, Gemini CLI, Hermes, OpenCode, Pi, or one of your own) in a gVisor sandbox,
streams standard Responses events back, and keeps the session so the next request continues it. Any OpenAI SDK
works unchanged.

[![CI](https://github.com/HarnessGateway/harnessgateway/actions/workflows/ci.yml/badge.svg)](https://github.com/HarnessGateway/harnessgateway/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/HarnessGateway/harnessgateway)](https://github.com/HarnessGateway/harnessgateway/releases)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="hg_…")
r = client.responses.create(model="claude-code", input="Write a FizzBuzz in Rust and run its tests")
print(r.output_text)
r2 = client.responses.create(model="claude-code", previous_response_id=r.id, input="Now add a CLI flag for the limit")
```

## Why

Coding agents are becoming infrastructure: something an application calls, not something a person types at. Every
CLI has its own install, flags, configuration files and output format, and none of them was built to run many
sessions for many callers safely. HarnessGateway puts one contract and one sandbox in front of all of them:

- **One API for every agent.** The OpenAI Responses schema, in both directions, validated with the SDK's own types.
  `model` is `harness[/provider[/model]]`; `previous_response_id` continues a session. There is no HarnessGateway
  schema to learn.
- **The request serves the task; the package defines the agent.** Input, files and instructions come with each
  request. The CLI and its version, the standing prompt, skills, hooks, permissions, MCP catalog, egress and secrets
  are a *harness package*: your operational IP, version-controlled and installed from git. An *overlay* puts your
  configuration on a built-in CLI without copying its mapping.
- **Sandboxed by default.** Each session runs under gVisor with no route out but its egress door, which enforces
  your allow and deny entries. No secret ever enters a sandbox: provider keys are swapped in at the gateway, and
  organization or caller secrets are placeholders inside, injected at the door toward their bound hosts only.
- **Sessions that survive.** The workspace, the agent's state and the CLI's own session store are snapshotted after
  every turn and restored on any node, so a conversation continues wherever its next turn runs.
- **Metered and observable.** Allocated CPU and memory, bytes through the door and storage held, per organization,
  in an idempotent ledger; every harness, gateway and door exchange traced. Tokens are not metered: you pay your
  model provider directly with your own key.
- **The platform knows no CLI.** Everything CLI-specific lives in a package. Adding an agent means writing a package,
  not changing the platform.

## Quick start

On a Linux host with Docker (gVisor is installed for you):

```bash
git clone https://github.com/HarnessGateway/harnessgateway && cd harnessgateway
sudo ./deploy/install.sh
```

The installer starts Postgres, the API with the provider gateway, a node, and the consoles, creates your
organization with the built-in harnesses, and prints your admin key and console sign-ins once. Then:

1. Open the console at `http://localhost:8800`, sign in as `admin`, and add a provider under **Setup → Providers**
   (an Anthropic, OpenAI or Gemini API endpoint and your key; the key is sealed and never shown again).
2. Open **Workbench → Chat**, pick a harness and the provider, and ask for something.
3. Or call the API with any OpenAI SDK pointed at `http://localhost:8000/v1`, as above.

The deployer console at `http://localhost:8800/ops` shows health, nodes, organizations, usage and configuration.
For many hosts, a managed Postgres, an S3 bucket and node pools, see [`docs/operations.md`](docs/operations.md).

## Built-in harnesses

| Harness | CLI | Provider APIs |
|---|---|---|
| `claude-code` | Anthropic Claude Code 2.1.310 | anthropic |
| `codex` | OpenAI Codex CLI 0.164.0 | openai (Responses) |
| `gemini-cli` | Google Gemini CLI 0.65.0 | google |
| `hermes` | Nous Research Hermes Agent 0.22.5 | anthropic, openai |
| `opencode` | OpenCode 1.19.4 | anthropic, openai |
| `pi` | Pi 1.4.0 | anthropic, openai |
| `shell` | bash (no model; the reference package) | — |

Each package installs its CLI by the vendor's documented method, pinned to a version, and maps the CLI's stream to
Responses items (`message`, `reasoning`, `shell_call`, `apply_patch_call`, `mcp_call`, `function_call`). Any
provider that speaks one of the three API shapes works, including compatible endpoints from other vendors. See each
package's README under [`packages/`](packages/), and [`docs/packages.md`](docs/packages.md) to build your own.

## How it works

```
caller ──Responses──▶ API ──queue (Postgres)──▶ node ──▶ gVisor sandbox ── run.sh ── the CLI
                       ▲                                     │                 │
                       │ events (SSE)                        │ provider calls  │ egress (allow-only)
                       └─────────────────────────────────────┤                 │
                                                        door ─┴─▶ gateway (key swap) ─▶ your model provider
                                                             └─▶ allowed hosts (secrets injected in headers)
```

- The **API** validates the request, resolves the harness, provider and MCP servers, stores the request and queues a
  turn. Streams follow the turn's events from any API replica.
- A **node** claims the turn, installs the CLI once per package into a read-only tools layer, starts the session's
  sandbox with the package's seeds (or restores its snapshot), and runs one turn.
- The **door** is the sandbox's only network path: an allow-listed CONNECT proxy and the relay to the **gateway**,
  which exchanges the turn's token for your provider key.
- The package's stdout is validated event by event; the terminal event becomes the final `Response`. The session's
  workspace is saved, and the sandbox stays warm for the next turn.

The whole design, including the contract, packages, sandboxing, secrets, scale, metering and the extension seam for
a multi-tenant deployment, is in [`SPEC.md`](SPEC.md).

## Documentation

| | |
|---|---|
| [`SPEC.md`](SPEC.md) | the specification the code is checked against |
| [`docs/operations.md`](docs/operations.md) | one host, many hosts, every setting, upgrades, releasing, troubleshooting |
| [`docs/console.md`](docs/console.md) | the organization and deployer consoles and the chat |
| [`docs/packages.md`](docs/packages.md) | building a harness package or an overlay, hgkit, secrets, persistence, testing |
| [`docs/extensions.md`](docs/extensions.md) | the extension seam: tenants, admission, metering hooks |
| [`packages/`](packages/) | the built-in packages, each with its command, request mapping and known limits |
| [`CHANGELOG.md`](CHANGELOG.md) | releases |

## Contributing

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check . && .venv/bin/mypy && .venv/bin/pytest -q tests/unit packages
```

[`AGENTS.md`](AGENTS.md) describes the repository, its rules and how to work in it; it applies to people and to
coding agents alike. Read `SPEC.md` before changing behaviour, and change it first when the design changes.
Integration tests run against a live stack (`tests/integration`). Security issues: open a private security advisory
on GitHub rather than a public issue.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
