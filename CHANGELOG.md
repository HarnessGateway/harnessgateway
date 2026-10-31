# Changelog

The release record. A release is the top entry: when `main` carries a version here that has no tag yet, the release
workflow builds and pushes the images, tags `v<version>` and publishes the entry as the release notes. To release:
add the entry, set the same version in `pyproject.toml`, `harnessgateway/__init__.py`, `deploy/compose.yaml`,
`deploy/install.sh` and `docs/operations.md` (`scripts/version-check` holds you to it), merge to `main`.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org).

## [1.1.1]

### Changed

- Sessions is observability only: the Sessions pages no longer open a session in the chat or start one, and every
  session and session header names the client that created it (the API key's label). Each turn now records the key,
  or the extension principal, that created it (`turns.actor`); a container's `metadata.created_by` carries it.

## [1.1.0]

### Added

- `GET /v1/containers/{id}/responses`: a session's responses in turn order, each as `GET /v1/responses/{id}` returns
  it; `GET /v1/containers/{id}/traces` (admin): every trace record of the session's turns with its `response_id`.
- The organization console's Sessions page opens a session: the transcript of every response as a conversation
  trace, live while a turn runs, with what the platform measured under each turn, and the workspace beside it (the
  chat's file panel, embedded). The Traces page is gone; a response's raw trace is one click from its turn.
- The chat's panel-only mode (`/chat/?panel=<session>`): the file panel alone, for embedding.

## [1.0.1]

### Fixed

- `deploy/install.sh` run as `curl … | sudo bash` stopped after starting the compose project, without creating the
  organization's console users or printing the admin key: the `docker compose exec` that runs `init` read the rest of
  the script from stdin. The script now runs from a function called on its last line, so bash reads all of it before
  executing any of it, and the compose commands get no stdin.

## [1.0.0]

The first release of HarnessGateway: coding-agent harnesses as an API.

A caller sends a standard OpenAI Responses request. The platform runs it through a **harness package** (a
coding-agent CLI with its configuration) in a gVisor sandbox and streams standard Responses events back, keeping
the session so the next request continues it. Any OpenAI SDK works unchanged. Self-hosted is one organization; a
SaaS adds tenants through the extension seam. The design is in `SPEC.md`; this release implements all of it.

The one rule behind every part: **the request serves the task; the package defines the agent.** What to work on
comes with each request. What the agent *is* (its CLI and version, standing instructions, skills, hooks, permissions,
MCP catalog, egress and secrets) is a package an organization version-controls and installs from git. The platform
itself knows no CLI, and no secret ever enters a sandbox.

### The contract

- `POST /v1/responses` accepts the OpenAI SDK's `ResponseCreateParams`; every event a package prints and the final
  `Response` are validated with the SDK's own types. There is no HarnessGateway request or response schema.
- `model` routes as `harness[/provider[/model]]`; `previous_response_id` continues a session on the same harness
  with any provider or model; `stream`, `background`, `store` and `Idempotency-Key` behave as OpenAI's do.
- Input files by `file_id` (`/v1/files`) or inline; `instructions` and system/developer messages; `reasoning`,
  `text.format`, `max_tool_calls` and the rest reach the package, which maps what its CLI supports and names what it
  ignored in `metadata.hg_ignored_fields`.
- MCP servers: a request selects and narrows the package's catalog, or adds a server on a host the organization
  already allows; a caller's own token never enters the sandbox.
- Sessions are OpenAI containers: `GET /v1/containers`, the workspace's files from the last save, uploads staged
  for the next turn; `GET /v1/models` lists every harness on every provider it can use.

### Harness packages

- A package is a folder: `harness.json`, `install.sh` (the CLI, pinned, by its official method), `run.sh` (one
  turn: the request in, Responses events out), `seeds/home` (the agent's standing configuration, re-applied in every
  new sandbox) and `seeds/workspace` (a starter project). Content-addressed by digest; installed from a built-in, a
  git repository or an upload; validated at the boundary with the field named.
- **Overlays**: `harness.json` with `extends` plus seeds. An organization ships its instruction files, skills, hooks,
  MCP catalog, settings and secrets on a built-in CLI without copying its mapping; the base is pinned by digest and
  composed at install.
- **hgkit**, in the sandbox image: `Request` (prompt, instructions, files placed by name, MCP servers), `Emitter`
  (valid events: streamed text, reasoning, shell, patch, MCP and function items, exactly one terminal event) and
  `State`.
- **Built-in packages**: `claude-code` 2.1.310, `codex` 0.164.0, `gemini-cli` 0.65.0, `hermes` 0.22.5,
  `opencode` 1.19.4, `pi` 1.4.0, and `shell` (no model; the reference package). Each maps its CLI's stream to
  Responses items (`message`, `reasoning`, `shell_call`, `apply_patch_call`, `mcp_call`, `function_call`), resumes
  its own session across turns, and ships tests on streams recorded from real turns.

### Sandboxes and the network

- One gVisor box per session: uid 10001, no capabilities, read-only root, size-limited `/tmp`, CPU, memory and
  process limits from the organization's capacity; writable `/workspace`, `/state` and `$HOME` only.
- A **door** per box is its only way out: a CONNECT proxy enforcing the organization's and the harness's egress
  entries (`host`, `*.domain`, `*`, `:port`, `!deny`; a deny wins at either level), no DNS inside the box, private
  addresses refused.
- **No secret in a box.** Provider keys are swapped in at the gateway for a per-turn token that works only through
  that box's door. Organization secrets and callers' MCP tokens are placeholders inside; the door replaces them in
  request headers toward their bound hosts only.
- Tools install once per package and image in an install box with the install egress; sessions snapshot
  `/workspace`, `/state` and the package's `persist` paths and restore on any node.

### Scale

- One image, one command: `serve --role api,gateway`, `node`, `door`, `migrate`, `init`, `node-token`, `keygen`.
  API and gateway replicas are stateless; nodes join pools with tokens and claim turns from Postgres with
  `SKIP LOCKED`; streams follow on any replica through `LISTEN/NOTIFY`; a sweeper fails the turns of lost nodes.
- Postgres is the one store, with row-level security on every tenant table; objects live on a local directory or
  any S3-compatible bucket; organization data keys are wrapped locally or by AWS KMS.

### Observability and metering

- Every turn's harness exchange, every provider call through the gateway and every door connection are recorded
  (`GET /v1/traces/{id}`), with bodies when the organization's observability is `full`; no record holds a secret.
- Usage is measured where it happens and kept as an idempotent ledger: `vcpu_seconds`, `memory_gib_seconds`,
  `egress_bytes`, `ingress_bytes`, `storage_gib_hours` (`GET /v1/usage`, by day, harness or session). Tokens are
  not metered: the organization pays its provider directly.

### The extension seam

- `HG_EXTENSIONS` loads classes that may add routes and migrations, authenticate requests, admit or refuse turns,
  and receive every trace record, usage batch and turn summary: what a multi-tenant SaaS needs, with nothing in the
  core forked. Operator endpoints under `/internal` give health, nodes, organizations and their capacity, usage
  across organizations, configuration and versions.

### The consoles

- An **organization console** on the public API: Setup (providers, harnesses with a package reader and editor to
  view, fork, write and install packages and overlays, egress), Workbench (the chat and a playground), Access (API
  keys, members), Observability (sessions, traces, usage).
- A **deployer console** at `/ops`: health, nodes and pools with join tokens, organizations, platform usage,
  versions, configuration, audit log.
- The **chat**: conversations as sessions, replies streamed as markdown with thinking and tool steps, attachments,
  and a side panel that renders what the agent built (pages run sandboxed; images, media, documents, notebooks,
  tables, code). Usable on its own or embedded.
- Local sign-in with initial passwords changed at first use; the browser never sees a platform key.

### Deployment

- `deploy/install.sh`: one Linux host with Docker goes from nothing to a running platform and consoles in one
  command (gVisor installed if absent, secrets generated, images pulled, the organization created, the admin key
  printed once). Many hosts: one Postgres, one bucket, replicas behind a load balancer, nodes joined with tokens.
- Images `ghcr.io/harnessgateway/harnessgateway` and `ghcr.io/harnessgateway/sandbox` for amd64 and arm64,
  hardened (Chainguard Wolfi, no package manager or setuid files left), versioned together with the platform.

### Not in this version

Kubernetes as a sandbox runtime; per-request egress narrowing; request-borne skills, hooks or configuration files;
`POST /v1/containers`; the Conversations API; live steering of a running turn; secret injection into request
bodies or URLs. Each is refused and reported as such, not half-built.
