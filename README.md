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
  with a click. The built-in `harness-package-builder` writes one from a name.
- **Sandboxed by default, nothing baked in.** Each session runs in a box: a gVisor container that exists only while
  the session has work, with no route out but its door, which enforces your allow and deny entries. A package's CLI
  is installed once per organization into an archive the box restores, never into an image. No secret ever enters a
  box: provider keys and organization or caller secrets are placeholders inside, injected at the door toward their
  bound hosts only.
- **Sessions that survive, cheaply.** After every turn the box saves what changed, as content-addressed blobs shared
  across the organization; a fork or a switch of harness copies nothing. A session resumes from its last snapshot in
  a new box anywhere.
- **Stateless, scaling in and out.** The api, the worker and the door hold no state; Postgres keeps the records,
  Redis the moments, an object store the bytes. More load is more replicas; no capacity to plan.
- **Metered and observable.** Agent seconds, the CPU and memory behind them, bytes through the door and storage held,
  per organization, in an idempotent ledger; every harness run, door connection and provider call traced. Tokens
  are not metered: you pay your model provider directly with your own key.
- **The platform knows no CLI.** Everything CLI-specific lives in a package. Adding an agent means adding a package,
  not changing the platform.

## Quick start

On a Linux host with Docker (gVisor is installed for you):

```bash
git clone https://github.com/HarnessGateway/harnessgateway && cd harnessgateway
sudo ./deploy/install.sh
```

The installer starts Postgres, Redis, the api, a worker, the door and the console, creates your organization with the
built-in harness package builder and your admin account, and prints the invitation link that activates it and an admin
key for automation, once. Then:

1. Open the invitation link, choose a password, and add a provider under **Setup → Providers**
   (an Anthropic, OpenAI or Gemini API endpoint and your key; the key is sealed and never shown again).
2. Add a harness under **Setup → Harnesses**: **New harness** starts one from the template, you edit its three files
   for the CLI you want (the template's README says what to change in each), and **Install**. Or ask the built-in
   `harness-package-builder` in the chat for a harness for your CLI and import what it wrote from its session page.
3. Open **Workbench → Chat**, pick the harness and the provider, and ask for something.
4. Or call the API with any OpenAI SDK pointed at `http://localhost:8000/v1`, as above.

Under **Access**, invite developers by email or sign them in through your identity provider, and create an API key
per app: a run-only key is what an app holds, and its sessions and usage are read by that key. The platform console at
`http://localhost:8800/ops` shows health, boxes, organizations, usage, configuration and the audit log to platform
admins. For many hosts, a managed Postgres and Redis and an S3 bucket, see [`docs/operations.md`](docs/operations.md).

## Harnesses

The platform knows no CLI and no language. Every harness is a package of three files, and every package starts as a
copy of one template, the `shell` package under [`packages/`](packages/): a manifest (`harness.json`), the CLI's
pinned install (`install`), and one turn (`run`: an executable in any language that reads the request and prints
Responses events). The template's `run` is Python with hgkit, the Responses contract as a library: the request read
for you, the events (`message`, `reasoning`, `shell_call`, `apply_patch_call`, `mcp_call`, `function_call`) printed
correctly, and the platform's own validator to check with. You write and edit these in the console, install with a
click, and test with a request: in the chat, or over the API. Any provider that speaks one of the three API shapes
(`anthropic`, `openai`, `google`) works, including compatible endpoints from other vendors. Or ask the one built-in
harness: `harness-package-builder` is Claude Code seeded with the skill that writes packages; say "Create a harness
package for <name>" in the chat, and turn what it wrote into a draft from its session page. See the template's README
and [`docs/packages.md`](docs/packages.md).

## How it works

```
caller ──Responses──▶ api ──records──▶ Postgres      Redis ◀──moments & jobs──┐
                       ▲                                │                      │
                       │ events (SSE, from any replica) │ queue                │
                       │                             worker ──launcher──▶ box (gVisor) ── run ── the CLI
                       │                                                    │
                       └──────── policy, records ──── door ◀── the only exit ┘
                                                        └──▶ your model provider (key swapped in), allowed hosts
```

- The **api** validates the request, resolves the harness, provider and MCP servers, stores the request and queues a
  turn; when the session has no box, a worker launches one. Streams follow the turn's events from any api replica.
- The **box** (the runner inside a sandbox) sets itself up from the api: the package, the organization's install
  archive, the trust bundle, the session's last snapshot. It long-polls for its session's next turn, runs the
  package's `run`, streams the output, reports how it ended, saves what changed and kills everything it did not start.
  No turn within the standby, and it leaves.
- The **door** is the box's only network path: a CONNECT proxy that asks the api what the box may reach now, refuses
  the rest, and intercepts bound hosts to swap placeholders for values in request headers.
- The package's stdout is validated event by event, with the same reader hgkit gives packages to check themselves;
  the terminal event becomes the final `Response`.

The whole design, including the contract, packages, boxes, snapshots, the door, metering and the extension seam, is
in [`SPEC.md`](SPEC.md).

## Documentation

| | |
|---|---|
| [`SPEC.md`](SPEC.md) | the specification the code is checked against |
| [`docs/operations.md`](docs/operations.md) | one host, many hosts, every setting, upgrades, releasing, troubleshooting |
| [`docs/api.md`](docs/api.md) | the API as a conversation: turns, sessions, management |
| [`docs/security.md`](docs/security.md) | isolation, egress, secrets, access, limits |
| [`docs/console.md`](docs/console.md) | the organization and platform consoles and the chat |
| [`docs/packages.md`](docs/packages.md) | building a harness package, hgkit, secrets, persistence, testing |
| [`docs/extensions.md`](docs/extensions.md) | the extension seam: tenants, launchers, admission, metering hooks |
| [`packages/`](packages/) | the two shipped packages: `shell`, the template every harness starts from, and `harness-package-builder`, the built-in |
| [`CHANGELOG.md`](CHANGELOG.md) | releases |

## Contributing

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check . && .venv/bin/mypy && .venv/bin/pytest -q tests/unit tests/runner packages
```

[`AGENTS.md`](AGENTS.md) describes the repository, its rules and how to work in it; it applies to people and to
coding agents alike. Read `SPEC.md` before changing behaviour, and change it first when the design changes.
Integration tests run against a live stack (`tests/integration`). Security issues: open a private security advisory
on GitHub rather than a public issue.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
