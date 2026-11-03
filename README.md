<p align="center"><img src="docs/mascot.png" width="220" alt="The HarnessGateway mascot, a hermit crab whose shell carries a circuit trace"></p>

# HarnessGateway

**Any coding-agent harness, behind one API.** Send a standard OpenAI Responses request; HarnessGateway runs it
through a *harness package* in a gVisor sandbox, streams standard Responses events back, and keeps the session so
the next request continues it. The request is the task; the package is the agent. A package is a handful of files
that teach the platform one CLI, so any harness fits: Claude Code, Codex, Pi, an 81-line agent from a GitHub
repository, or your own. You write a package in the console, or ask the built-in **harness package builder** for one
by name. Any OpenAI SDK works unchanged.

[![CI](https://github.com/HarnessGateway/harnessgateway/actions/workflows/ci.yml/badge.svg)](https://github.com/HarnessGateway/harnessgateway/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/HarnessGateway/harnessgateway)](https://github.com/HarnessGateway/harnessgateway/releases)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

<p align="center"><img src="docs/demo.gif" width="100%" alt="The console on a fresh deploy: a provider, the built-in harness package builder, three chats asking it to create harness packages for codex, pi and claude-code, each package imported from its session page and installed, a chat with each new harness writing and running FizzBuzz, and the sessions under Observability"></p>

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

- **One contract, every harness.** The OpenAI Responses schema, in both directions, validated with the SDK's own
  types, is the only interface: `model` is `harness[/provider[/model]]`, `previous_response_id` continues a session,
  and there is no HarnessGateway schema to learn. Because a harness is a package rather than an integration, the
  platform is not tied to any list of supported agents: whatever runs headless in a sandbox can be packaged.
- **The request serves the task; the package defines the agent.** Input, files and instructions come with each
  request. The CLI and its version, the standing prompt, skills, hooks, permissions, MCP catalog, egress and secrets
  are the package: a handful of files you write and edit in the console, starting from one template, and install
  with a click. The built-in `harness-package-builder` writes one from a name: it researches the CLI, pins the
  current release, runs it in its sandbox, and hands back a package with recorded fixtures and a test.
- **Sandboxed by default.** Each session runs under gVisor with no route out but its egress door, which enforces
  your allow and deny entries. No secret ever enters a sandbox: provider keys are swapped in at the gateway, and
  organization or caller secrets are placeholders inside, injected at the door toward their bound hosts only.
- **Sessions that survive.** The workspace, the agent's state and the CLI's own session store are snapshotted after
  every turn and restored on any node, so a conversation continues wherever its next turn runs.
- **Metered and observable.** Allocated CPU and memory, bytes through the door and storage held, per organization,
  in an idempotent ledger; every harness, gateway and door exchange traced. Tokens are not metered: you pay your
  model provider directly with your own key.
- **The platform knows no CLI.** Everything CLI-specific lives in a package. Adding an agent means adding a package,
  not changing the platform.

## Quick start

On a Linux host with Docker (gVisor is installed for you):

```bash
git clone https://github.com/HarnessGateway/harnessgateway && cd harnessgateway
sudo ./deploy/install.sh
```

The installer starts Postgres, the API with the provider gateway, a node, and the consoles, creates your
organization with the built-in harness package builder, and prints your admin key and console sign-ins once. Then:

1. Open the console at `http://localhost:8800`, sign in as `admin`, and add a provider under **Setup → Providers**
   (an Anthropic, OpenAI or Gemini API endpoint and your key; the key is sealed and never shown again).
2. Add a harness under **Setup → Harnesses**: **New harness** starts one from the template, you edit its three files
   for the CLI you want (the template's README says what to change in each), and **Install**. Or ask the built-in
   `harness-package-builder` in the chat for a harness for your CLI and import what it wrote from its session page.
3. Open **Workbench → Chat**, pick the harness and the provider, and ask for something.
4. Or call the API with any OpenAI SDK pointed at `http://localhost:8000/v1`, as above.

The deployer console at `http://localhost:8800/ops` shows health, nodes, organizations, usage and configuration.
For many hosts, a managed Postgres, an S3 bucket and node pools, see [`docs/operations.md`](docs/operations.md).

## Harnesses

The platform knows no CLI and no language. Every harness is a package of three files, and every package starts as a
copy of one template, the `shell` package under [`packages/`](packages/): a manifest (`harness.json`), the CLI's
pinned install (`install`), and one turn (`run`: an executable in any language that reads the request and prints
Responses events). The template's `run` is Python with hgkit, the Responses contract as a library: the request read
for you, the events (`message`, `reasoning`, `shell_call`, `apply_patch_call`, `mcp_call`, `function_call`) printed
correctly, and the platform's own validator to check with. You write and edit these in the console, install with a
click, and test with a request: in the chat, or over the API. Any provider that speaks one of the three API shapes
(`anthropic`, `openai`, `google`) works,
including compatible endpoints from other vendors. Or ask the one built-in harness: `harness-package-builder` is
Claude Code seeded with the skill that writes packages; say "Create a harness package for <name>" in the chat, and
turn what it wrote into a draft from its session page. See the template's README and
[`docs/packages.md`](docs/packages.md).

## How it works

```
caller ──Responses──▶ API ──queue (Postgres)──▶ node ──▶ gVisor sandbox ── run ──── the CLI
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
- The package's stdout is validated event by event, with the same reader hgkit gives packages to check themselves;
  the terminal event becomes the final `Response`. The session's
  workspace is saved, and the sandbox stays warm for the next turn.

The whole design, including the contract, packages, sandboxing, secrets, scale, metering and the extension seam for
a multi-tenant deployment, is in [`SPEC.md`](SPEC.md).

## Documentation

| | |
|---|---|
| [`SPEC.md`](SPEC.md) | the specification the code is checked against |
| [`docs/operations.md`](docs/operations.md) | one host, many hosts, every setting, upgrades, releasing, troubleshooting |
| [`docs/console.md`](docs/console.md) | the organization and deployer consoles and the chat |
| [`docs/packages.md`](docs/packages.md) | building a harness package, hgkit, secrets, persistence, testing |
| [`docs/extensions.md`](docs/extensions.md) | the extension seam: tenants, admission, metering hooks |
| [`packages/`](packages/) | the two shipped packages: `shell`, the template every harness starts from, and `harness-package-builder`, the built-in |
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
