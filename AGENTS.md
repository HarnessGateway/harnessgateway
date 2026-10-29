# AGENTS.md

How to work in this repository. It is written for coding agents and applies to people the same way.

## What this is

HarnessGateway runs coding-agent CLIs as an API. A caller sends an OpenAI Responses request; the platform runs it
through a **harness package** (a CLI with its configuration) in a gVisor sandbox and streams Responses events back,
keeping the session so the next request continues it. The request serves the task; the package defines the agent.
Self-hosted is one organization; a SaaS adds tenants through the extension seam.

**`SPEC.md` is the specification.** Read it before changing behaviour, and change it first when the design changes.
The code is checked against it; a change the spec does not describe is a spec change.

## Layout

| Path | What it holds |
|---|---|
| `harnessgateway/domain/` | pure rules, no I/O: the Responses contract, manifests and overlay composition, packages (digest, archive), egress entries, MCP resolution, capacity, meters, turn states |
| `harnessgateway/ports/`, `adapters/` | interfaces (BlobStore, KeyWrapper, sandbox Runtime) and their implementations (file and S3 stores, local and KMS key wrapping, Docker with gVisor). Vendor SDKs live only in `adapters/` |
| `harnessgateway/services/` | one class per responsibility: orgs, keys, vault, providers, harnesses, files, containers, turns (the caller's side), delivery (the node's side), scheduler, usage, traces, sweeper, operations. `platform.py` is the composition root |
| `harnessgateway/api/` | FastAPI routers: OpenAI-shaped (`responses.py`, `storage.py`), management (`manage.py`), the nodes' and operator's routes (`internal.py`); `http.py` is authentication, body limits and the error envelope |
| `harnessgateway/gateway/` | the provider relay: turn token + door token → the organization's key |
| `harnessgateway/node/` | the node agent (`agent.py`), its API client, snapshots (`state.py`), stdout batching (`pump.py`), the node CA (`trust.py`), the egress door (`door.py`), `runtime/turn.sh` |
| `harnessgateway/migrations/` | SQL migrations, applied in order under an advisory lock |
| `sandbox/` | the sandbox image and `hgkit`, the helper library package mappers use |
| `packages/` | the built-in harness packages, one folder each |
| `console/` | the organization and deployer consoles (`server.py`, `static/`) and the chat (`chat/`): applications on the public API, vanilla JS, no build step |
| `deploy/` | `compose.yaml`, `install.sh`, the Postgres init script, `.env.example` |
| `docs/` | operations, consoles, packages, extensions |
| `tests/unit`, `tests/integration` | unit tests; tests against a live stack (`HG_TEST_STACK=1`) |
| `scripts/version-check` | the one version, everywhere it is written |

## Checks

CI runs these on every push; run them before every commit, the whole suite, not only the part you changed:

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"     # once
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pytest -q tests/unit packages
scripts/version-check
```

Integration tests need a running stack: `HG_TEST_STACK=1 HG_TEST_URL=http://localhost:8000 HG_TEST_KEY=hg_...
.venv/bin/pytest -q tests/integration` (the operator tests also need `HG_TEST_OPERATOR_KEY`). They install and
remove a test overlay and set the organization's egress.

## Running it locally

`sudo ./deploy/install.sh` on a Linux host with Docker is the supported way and takes a few minutes. For development
without compose, run the processes from the checkout against a Postgres and a directory object store: `harnessgateway
serve` (`HG_DATABASE_URL`, `HG_BLOB_URL=file:///...`, `HG_SECRET_KEY`, `HG_NODE_JOIN_TOKEN`), then `harnessgateway init`
for the organization and admin key, then `harnessgateway node` as root with Docker and the `runsc` runtime
(`HG_UNSAFE_RUNTIME=runc` only where gVisor is unavailable; nothing is isolated then). The console is `python3
console/server.py` with `HG_KEY` (an admin key) and optionally `HG_OPERATOR_KEY`. Every setting is in
`docs/operations.md`.

## Rules

- **The contract is the OpenAI Responses schema, in both directions** (SPEC §4). No HarnessGateway request or
  response schema; output is validated with the OpenAI SDK's own types.
- **The platform knows no CLI.** Anything CLI-specific belongs in a package. Never change the platform for a harness:
  if a harness cannot work within the contract, stop and report the exact blocker and what it would need.
- **The request serves the task; the package defines the agent** (SPEC §3). A request may select or narrow what a
  package offers, never widen it.
- **No secret ever enters a sandbox** (SPEC §8): placeholders inside, values injected at the gateway or the door.
  No secret in any file, log, commit, test fixture or error message either.
- **Negative space.** Every boundary (request, manifest, settings, egress entry, path, archive, node report) refuses
  what is not allowed with a message that names what is. Invariants are asserted, not handled.
- **Ports and adapters.** Vendor SDKs only in `harnessgateway/adapters/`; standards (Postgres, OCI images, HTTP, the
  Responses wire) are used directly, not wrapped.
- **Packages** follow SPEC §5.8's layout: `harness.json`, `install.sh`, `run.sh` (one line into `map.py`), `map.py`,
  `README.md` (Files, The command, Request fields, Output mapping, Known limits, Tests), `tests/` with fixtures
  recorded from real runs. Install each CLI by its official, documented method, pinned to a version, and verify the
  version; never name a CPU in a script; no post-install edits of installed files. A built-in seeds nothing an
  organization would want to own.
- **Never mention "harnessrouter"** (any spelling) in any file. CI greps for it.

## Working style

- Think before coding: state assumptions, surface tradeoffs, ask when two readings lead to different work.
- Minimum code that solves the problem. No speculative features, abstractions for single-use code, or configurability
  nobody asked for. If 200 lines could be 50, rewrite.
- Surgical changes: touch only what the task needs, match the surrounding style, remove only what your change made
  unused. Mention unrelated dead code; do not delete it.
- Define the check before the change (a failing test, a reproduced bug, a command that must pass), then loop until
  it passes. Report outcomes as they are.
- Prose in code and docs says what a thing is and why, in plain sentences; no marketing, no filler.

## Releasing

`CHANGELOG.md`'s top entry is the version. `scripts/version-check` holds `pyproject.toml`, `harnessgateway/__init__.py`,
`deploy/compose.yaml`, `deploy/install.sh` and `docs/operations.md` to it. A push to `main` whose version has no tag
builds both images for amd64 and arm64, pushes them to GHCR, tags `v<version>` and publishes the entry as the
release. Both images are versioned together with the platform; a package revision bumps the package's own version
in its manifest.
