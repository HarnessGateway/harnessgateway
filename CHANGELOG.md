# Changelog

The release record. A release is the top entry: when `main` carries a version here that has no tag yet, the release
workflow builds and pushes the images, tags `v<version>` and publishes the entry as the release notes. To release:
add the entry, set the same version in `pyproject.toml`, `harnessgateway/__init__.py`, `deploy/compose.yaml`,
`deploy/install.sh` and `docs/operations.md` (`scripts/version-check` holds you to it), merge to `main`.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org).

## [2.3.2]

### Fixed

- A package's output may carry a NUL character (a binary read back as text, echoed by the CLI); Postgres holds none
  in text or jsonb, so the turn's events could not be stored and the turn failed. The database boundary now stands
  U+FFFD in for NUL, in every parameter at any depth.
- When delivering a turn's output failed, the node stopped reading the process's stdout, the process could not reach
  its end, and the turn's failure was reported as a bare `TimeoutError` in place of its cause. The node drains the
  output to its end and reports the delivery failure; a process that still does not exit is logged, not an error.
- A box's `/tmp` is a tmpfs, and the kernel charges it to the box's memory: with the default capacity (4096 MB of
  memory, a 4096 MB `/tmp`) a package that filled `/tmp` starved its own processes and the box was killed. The memory
  limit is now `memory_mb` for the processes plus `tmp_mb` for `/tmp`. A box killed for its memory is reported as
  such ("the sandbox ran out of memory: …") in place of an exit code without a terminal event.

## [2.3.1]

### Changed

- The builder's skill is a skill again: what a harness package is and how to build one, with the questions to ask
  the person. The stages, the build journal and the per-turn pacing of 2.3.0 are gone.

## [2.3.0]

### Added

- Every turn receives the session's conversation so far in Responses form: `/run/hg/history.json` (`HG_HISTORY`),
  each earlier turn's input and output items through every fork back to the root, a cancelled turn's partial output
  included. It is the platform's own record and the harness-agnostic way a package continues when its CLI's session
  is not there: a turn killed by a stop or an interrupt, a fork onto this harness from another, a CLI without a
  session. hgkit reads it (`req.history()`) and renders it as text for a CLI (`req.transcript()`); the built-in
  builder starts a fresh CLI session from it when its own cannot be resumed.
- A session may change harness: `previous_response_id` with another harness in `model` is a fork onto it, a new
  session on that harness restored from the checkpoint's workspace only (not the old package's `$HG_STATE` or
  `persist` paths), with the conversation as its history. The chat's harness picker is no longer locked once a chat
  has started; picking another harness continues the chat on it.
- The builder's skill is rewritten: shorter, harness-agnostic, staged (research, then install and record, then write
  and validate, one turn each), with a build journal in the package folder, egress observed through a logging proxy
  rather than guessed, `defaults.model` set to a model the organization's provider serves, and a continuity checklist
  every package must pass (the session id saved as soon as the CLI reports it, a killed turn resumed, a first turn
  continued from the history).

### Fixed

- A fork from a turn that saved nothing of its own (cancelled before it ran, lost with its node) started from the
  package's seeds; it now starts from the last checkpoint before that turn.
- `harness_mismatch` (409) is gone: a different harness is a fork onto it.

### Removed

- `install.sh` and `run.sh`: a package's executables are `install` and `run`, and nothing else is run.

## [2.2.1]

### Changed

- Providers: the console's **Replace key** is **Edit**, and `PUT /v1/providers/{name}` on an existing provider may
  leave `key` out to keep the stored one; a new provider still needs it.
- API keys and initial passwords, shown once at creation, have a copy-to-clipboard button beside them.
- The Playground is a one-off request: the harness / provider / model picker the chat has, the request on the
  left, the response on the right; the curl, Python and TypeScript snippets are gone.

## [2.2.0]

### Added

- Rewind, with fork semantics, as the Responses API means it: `previous_response_id` naming an earlier response of a
  session starts a new session restored from the state saved after that response (workspace, `$HG_STATE`, the
  package's `persist` paths, so the CLI's own memory rewinds with it), pinned to the same package; the original
  session is untouched, and the container's `metadata.forked_from` names the response. Every turn's snapshot is kept
  for its session's life (its checkpoint) instead of the superseded one being deleted; a fork copies its checkpoint
  under the new session. No harness is involved or told: from inside the sandbox a fork is a continuation on a fresh
  box. Migration 0004.
- The chat: **Rewind** on one of your messages takes it back to edit and resend, and **Branch** on a reply opens a new
  chat that continues from it; both name the earlier reply in `previous_response_id`. Interrupt is as before. The
  console's session page names what a session was forked from.

## [2.1.0]

### Changed

- A package is three files: `harness.json` beside two executables, `install` and `run`, each starting with a
  shebang line and run as it is. The platform knows no language: `run` may be Python, bash, node or a binary.
  `install.sh` and `run.sh` (run with bash) are still accepted. The manifest's `version` reaches both as
  `HG_PACKAGE_VERSION`, so a CLI's version is written in one place. The template (`shell`) and the built-in builder
  follow the layout; `map.py` is gone.
- hgkit is the Responses contract in Python, harness-agnostic: `Request` now also carries the turn's environment
  (`model`, `provider`, `state`, ids, the package version); `Emitter` names events by what the agent did (`say`,
  `think`, `ran`, `wrote`, `edited`, `deleted`, `called`, `used`, `done`, `stopped`, `failed`), streams thinking as
  it streams text, and reports a tool's result when it comes (`out.ran(cmd)` → `run.done(...)`,
  `out.called(...)` → `call.done(...)`). The old names (`text`, `message`, `reasoning`, `shell`, `patch`, `mcp`,
  `function`, `completed`, `incomplete`) are gone.

### Added

- hgkit validates: `Emitter(validate=True)` checks each event as it is printed, `hgkit.check(text)` reads a whole
  output, and `python3 -m hgkit check FILE` does so in the sandbox. It is the platform's own reader
  (`harnessgateway.domain.contract` imports it), on the same pinned OpenAI SDK, which the sandbox image now carries.
- The builder's skill is self-contained under `seeds/home`: its standing instructions (`~/.claude/CLAUDE.md`), the
  template (`assets/template`, a copy of `packages/shell`) and the packages guide (`reference/packages.md`, a copy of
  `docs/packages.md`) all live in the skill; `scripts/sync-builder-seeds` refreshes the copies and a unit test fails
  when they drift, so every deployment's builder ships the current skill. A builder session's workspace holds only
  the packages it delivers.
- The builder's skill covers tailored packages: told what an agent is for, the builder asks what changes what it
  will build (purpose, callers, what it may reach and must never do, model, the operator's knobs), proposes a plan,
  and builds the organization's agent on a vanilla base: the CLI's own permission rules and hooks in `seeds/home`,
  an `mcp` catalog with secrets bound to hosts, declared `settings` and `secrets`, narrowed egress and timeouts, and
  a recorded forbidden run that proves the fence (`reference/tailoring.md`).

## [2.0.1]

### Fixed

- The chat's harness / provider / model picker no longer runs under the workspace panel when the panel opens beside
  a narrow chat: the pickers shrink with an ellipsis (the model field first) and the panel leaves the header the room
  it needs.

### Changed

- The README demo is re-recorded on a fresh deploy with the fix, and highlights each click.

## [2.0.0]

### Changed

- Harnesses are written in the console. `init` creates the organization, the admin key and the built-in harness
  package builder; every other harness starts as a copy of one template, the `shell` package, edited, installed and upgraded
  in the console's editor (or installed with `POST /v1/harnesses` as a package archive). The template's README says,
  file by file, what to change for a CLI.

### Added

- `harness-package-builder`, the one built-in harness, installed by `init` (`HG_BUILDER_DIR`): Claude Code seeded with the
  harness-package-builder skill, the template and the packages guide. Ask it in the chat for a harness for a CLI; it
  writes the package in its workspace, and **New harness from workspace**, on a builder session's page, copies that
  folder into a draft. Every other harness is one someone wrote, from the template or with the builder.

### Removed

- The built-in packages (`claude-code`, `codex`, `gemini-cli`, `hermes`, `opencode`, `pi`), `{"builtin": name}`
  installs and `HG_PACKAGES_DIR`; installing from git (`{"git": …}`); overlays (`extends`): a harness is one
  package. A harness reports one `digest`; `extends`, `base_digest`, `overlay_digest` and `source` are gone, as are
  the console's Fork, Install a built-in and Install from git. Migration 0003 drops the columns.

### Fixed

- A sandbox's door kept running when its counters file could not be written (a full disk); it used to exit, which
  cut the box off from the network mid-turn.

## [1.1.2]

### Fixed

- The chat's panel-only mode opened the newest file when a turn's save landed; it now opens the best kind of file
  first (a page before a table or an image), as the chat does, and the newest among those.

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
