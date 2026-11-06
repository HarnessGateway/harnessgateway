# HarnessGateway 3.0 — Specification

This document is the platform. It is written so that someone with no context can build, run, operate and extend
HarnessGateway from it alone. The code is checked against it; when the design changes, this file changes first.

Read it in order once. Afterwards, §15 (build order) and §16 (checks) are where work starts.

---

## 1. What HarnessGateway is

**One sentence.** A sandbox that saves the agent's home directory and workspace, with a gateway that turns an OpenAI
Responses request into a run of that sandbox, and a ledger that says what it cost.

**The primitive.** A container holds a coding-agent CLI (a *harness*). A request arrives on its standard input. It runs
the task. Its home directory and workspace are saved at the end and restored before the next request, so the next
request continues the same session. Anyone can do this by hand with a script and a tarball.

**What the platform adds.** It does that for any harness, the same way, over the OpenAI Responses API, in hardened
ephemeral sandboxes, with egress control, with every secret kept out of the sandbox, with partial output,
interruption, forking and rewinding, with a usage ledger that bills **agent-seconds** and **stored bytes**, and with
nothing of a session living anywhere but an object store.

**Who defines what.**

| Thing | Defined by |
|---|---|
| The task, the instructions, the tools offered, the output format | the request (OpenAI Responses) |
| The harness: which CLI, how it is installed, how it is driven, what of its home persists | the harness package |
| Sandboxing, egress, secrets, sessions, snapshots, streaming, metering | the platform |

The platform knows no CLI. Nothing CLI-specific is ever added to it.

**Two deployments, one code.** The open-source core is a complete single-organization platform anyone self-hosts:
sandboxes, egress door, snapshots, SSO, keys and roles, audit, metering, autoscaled roles, console and chat. The SaaS
is the same image plus a private extension loaded through §13, adding what only many organizations need. Nothing is
held back from the core.

---

## 2. Vocabulary

| Term | Meaning |
|---|---|
| **Organization** | The tenant. Self-hosted has one; the schema supports many (row-level security on every tenant table). |
| **Harness** | A package installed in an organization under a name. `model: <name>` in a request selects it. |
| **Package** | A folder: `harness.json`, `install`, `run`, optional `seeds/`. Content-addressed by digest. |
| **Provider** | A model API endpoint of the organization: an API shape (`anthropic`, `openai`, `google`), a base URL and a sealed key. |
| **Session** | A sequence of turns sharing a home and a workspace. Identified as an OpenAI *container* (`cntr_…`). |
| **Turn** | One request and its response (`resp_…`). One run of the package's `run`. |
| **Snapshot** | The session's state after a turn: a manifest of paths and content hashes, plus the blobs it names. Every turn that changed something has one; it is that turn's checkpoint. |
| **Box** | One sandbox container running one session, for one or more consecutive turns, then gone. |
| **Runner** | The small program inside the box that drives a turn: fetch, restore, run, stream, snapshot, report. |
| **Door** | The egress proxy. A box's only way out. Enforces the allow list and swaps secret placeholders for values. |
| **Launcher** | The adapter that starts a box: Docker self-hosted, a container service in the SaaS. |
| **Worker** | The process that consumes the job queue (launching boxes, installs, stops, deletions) and runs scheduled duties. |
| **Placeholder** | A random token (`hgs_…`) a box holds where a secret would be; the door replaces it on the way out, only toward the hosts it is bound to. |
| **Agent-seconds** | Seconds a turn was running, measured by the API's clock from the box's first report to its finish. The unit customers are billed in. |

---

## 3. Principles

1. **The contract is the OpenAI Responses schema, both directions.** Requests are validated against the OpenAI SDK's
   request type; every event and the final `Response` are validated against the SDK's models. No HarnessGateway
   request or response schema exists. Platform facts ride in fields the schema has: `model`,
   `previous_response_id`, `metadata`, `tools` of type `mcp`.
2. **The platform knows no CLI.** A harness that cannot work inside the contract is a package problem; the platform
   never changes for it.
3. **Nothing on compute is state.** An API replica, a worker, a door replica, a box: each can die at any moment and
   the platform recovers from Postgres and the object store without losing a turn's record. Redis holds moments,
   not records (§7).
4. **No secret ever enters a sandbox.** Provider keys, harness secrets and caller credentials are placeholders
   inside; the door makes them real only toward the hosts they are bound to. No secret in any file, log, row,
   snapshot, event or error message.
5. **Include-only, allow-only.** What persists is what the package names (default: the whole home, and the
   workspace). What a box may reach is what the organization and the package allow. Nothing is excluded, because
   nothing else is included.
6. **Negative space.** Every boundary refuses what it does not allow with a message naming what it does. Invariants
   are asserted, not handled. There are no silent fallbacks.
7. **One implementation per idea.** One validator (shared with packages as `hgkit`). One snapshot format. One
   job shape. One secret mechanism (placeholders at the door). One image, several roles.
8. **Ports and adapters.** Vendor SDKs only in `adapters/`. Postgres, Redis, HTTP, OCI images and the OpenAI schema
   are standards the core is written against, not ports.

---

## 4. The Responses contract

### 4.1 Routing fields the platform reads

- `model`: `harness`, `harness/provider` or `harness/provider/model`. The model part may contain `/`.
- `previous_response_id`: a `resp_…` this platform returned. Resolves to its session (§9.3).
- `stream`, `background`, `store` (default true; `store: false` sessions are deleted ten minutes after their last turn).
- `input`: `file_id` references (`input_file`, `input_image`) are delivered into the box; at most 64 per request.
- `tools` of `type: "mcp"`: select or narrow the package's MCP catalog, or add a server with the caller's own
  credentials (replaced by placeholders before storage).
- `user`: the application's end user, kept on the session and turn for grouping.
- `metadata`: echoed; the platform adds `session_id`, `hg_reason` (why the platform ended a turn), `hg_error_code`
  (a package error code outside OpenAI's set), `hg_ignored_fields` (what the package reported it did not honour).
- `conversation` is refused (continue with `previous_response_id`). `connector_id` MCP tools are refused.

Everything else goes to the package untouched, in the request file.

### 4.2 Output

Each stdout line of `run` is one of:

- a Responses stream event (checked against the SDK model its `type` names; `sequence_number` assigned by the
  platform; `response.created`, `response.in_progress`, `response.queued` are dropped: the platform owns the envelope);
- the terminal event: `response.completed`, `response.incomplete` or `response.failed`, with `response.output`
  (or the completed `output_item.done` items when absent), `usage`, `error`, `incomplete_details`, `metadata`;
- anything else: a log line, kept in the turn's log tail (and the full log when observability is `full`).

A line the schema refuses fails the turn (`hg_reason: invalid_output`) and stops the process. Nothing may follow
the terminal event. A process that exits without one ends the turn as `failed` with `hg_reason:
exited_without_result`, keeping the completed items.

The final `Response` echoes the request's parameters, carries the package's output and usage, and validates
against the SDK's `Response` before a client sees it. A cancelled turn is `status: cancelled` with terminal event
`response.failed`.

### 4.3 Errors

OpenAI's envelope: `{"error": {"type", "code", "message", "param"}}`. Types: `invalid_request_error` (400, 413),
`authentication_error` (401, 423), `permission_error` (403), `not_found_error` (404), `conflict_error` (409),
`rate_limit_error` (429, an extension's refusal), `server_error` (500, 502).

### 4.4 Streaming

`POST /v1/responses` with `stream: true` is a server-sent event stream of the turn's events from `response.created`
to the terminal event, keep-alive comments every 15 s. `GET /v1/responses/{id}?stream=true&starting_after=N`
replays from event N+1 and follows live. A foreground turn (not `background`) is cancelled when its client
disconnects; a background turn outlives its request.

---

## 5. The package contract

A package is a folder. The platform reads `harness.json`, runs `install` and `run`, copies `seeds/home` and
`seeds/workspace`. Everything else is the package's own (README, tests, assets).

### 5.1 `harness.json`

```json
{
  "contract": 1,
  "name": "claude-code",
  "version": "2.1.310-1",
  "description": "…",
  "provider": {"apis": ["anthropic"]},
  "defaults": {"model": "claude-sonnet-5-5"},
  "install": {"egress": ["registry.npmjs.org"], "timeout_s": 900},
  "run": {"egress": [], "timeout_s": null, "stop_grace_s": 20},
  "persist": ["."],
  "settings": {"NAME": {"description": "", "default": null, "required": false}},
  "secrets": {"NAME": {"description": "", "hosts": ["api.example.com"], "required": false}},
  "mcp": {"label": {"url": "https://…", "transport": "http", "headers": {"Authorization": "Bearer ${NAME}"}, "allowed_tools": null}}
}
```

- `name`: `[a-z0-9][a-z0-9._-]{0,62}`. `version`: `<CLI version>-<package revision>`, 1–64 characters.
- `provider`: absent for a harness that calls no model. `apis` among `anthropic`, `openai`, `google`.
- `install.egress`, `run.egress`: egress entries (§8.2). `install.timeout_s` 1–3600 (default 900). `run.timeout_s`
  null (none) or ≥ 1; the organization's `turn_timeout_s` also bounds a turn. `run.stop_grace_s` 1–300 (20): the time
  the process gets after SIGTERM, and after its terminal event, before SIGKILL.
- `persist`: paths relative to `$HOME` kept across turns, at most 64, normalized, no `..`. `"."` means the whole
  home. Default `["."]`. The workspace and `$HG_STATE` always persist.
- `settings`: environment variables for `run`, named `[A-Z][A-Z0-9_]*`, not `HG_*`, not a reserved name (`HOME`,
  `PATH`, proxy and CA variables). At most 64; a value at most 16 KiB.
- `secrets`: same names; each bound to allow entries (`hosts`, no `!`). The organization sets values; `run` sees
  placeholders. A `${NAME}` in an MCP header must name a declared secret.
- `mcp`: the catalog. A request may select labels (narrowing `allowed_tools`) or add servers.

### 5.2 `install`

An executable (shebang script or Linux binary). Runs once per package digest and sandbox image, in an install box
with the install egress, as the sandbox user, with `HG_TOOLS` (writable, where the CLI goes), `HG_PACKAGE_DIR`,
`HG_PACKAGE_VERSION`, `HG_CONTRACT=1`, `HOME=/tmp/home`. Exit 0 within `install.timeout_s`. Its output tail is kept.
The result is archived (§9.6) and never placed in an image.

### 5.3 `run`

An executable. Runs once per turn, in the session's box, as the sandbox user, in `/workspace`, with the request on
standard input and these variables:

| Variable | Value |
|---|---|
| `HG_CONTRACT` | `1` |
| `HG_REQUEST` | `/run/hg/request.json`: the Responses request as the caller sent it, credentials replaced by placeholders |
| `HG_HISTORY` | `/run/hg/history.json`: the session's earlier turns in order through every fork, each `{id, status, input, output}` in Responses form, file bytes left out |
| `HG_FILES` | `/run/hg/files/`: `index.json` maps `file_id` to `{filename, path, bytes}`; each file at `<id>/<filename>` |
| `HG_MCP` | `/run/hg/mcp.json`: the turn's MCP servers `[{label, url, transport, headers, allowed_tools}]`, headers holding placeholders |
| `HG_WORKSPACE` | `/workspace` |
| `HG_STATE` | `/state`: small key/value files the package keeps (e.g. the CLI's own session id) |
| `HOME` | `/home/hg` |
| `HG_PACKAGE_DIR` | `/opt/hg/package` (read-only) |
| `HG_TOOLS` | `/opt/hg/tools` (the install's output, read-only) |
| `HG_SESSION`, `HG_RESPONSE`, `HG_TURN` | the session id, the response id, the turn's sequence number in the session |
| `HG_PACKAGE_VERSION` | the manifest's `version` |
| `HG_MODEL` | the model name the CLI is given: the request's, the session's, or the harness's default |
| `HG_PROVIDER_API` | `anthropic`, `openai` or `google`; empty without a provider |
| `HG_PROVIDER_URL` | the provider's base URL (e.g. `https://api.anthropic.com`), without a version segment |
| `HG_PROVIDER_TOKEN` | a placeholder the door replaces with the organization's key, on the provider's host only |
| settings and secret names | the organization's setting values; placeholders for secrets |
| `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO`, `NODE_EXTRA_CA_CERTS`, `PIP_CERT` | set by the runner: the door, and the trust bundle including the door's CA |

`run` prints §4.2 lines on stdout; stderr is kept as a tail (and whole when observability is `full`). It runs in
its own process group; a stop is SIGTERM to the group, `stop_grace_s`, then SIGKILL to everything in the box.

### 5.4 `seeds/`

`seeds/home/` is copied into `$HOME` before every restore (the session's own files win). `seeds/workspace/` is
copied into the workspace of a new session only. Nothing else may be under `seeds/`.

### 5.5 Archive, digest, validation

A package archive is a tar (gzipped or not) of the folder; one top-level directory wrapping it is stripped. Limits:
5,000 files, 256 MiB unpacked, 64 MiB uploaded. Regular files and directories only, nothing outside the root.
The digest is SHA-256 over every file's path, executable bit and bytes. The canonical archive (sorted, root-owned,
mtime 0) is stored once, shared by every organization that installs it: a package's files are the author's scripts,
never a CLI.

Validation: `harness.json` present and valid; `install` and `run` present, starting `#!` or ELF; `seeds/` holds only
`home` and `workspace`; every `${NAME}` reference declared.

### 5.6 `hgkit`

An optional Python library in the sandbox image (`/opt/hg/lib`, on `PYTHONPATH`): `Request` (the request and the
environment with readers), `Emitter` (a valid event stream with streamed text and reasoning, tool items, the
terminal event), `State`, and `check(text)`, the platform's own reader for package tests. Packages in other
languages print the same lines and use nothing of it. `python3 -m hgkit check FILE` reads an output the way the
platform does.

### 5.7 The two shipped packages

`packages/shell`: no model; the input text runs as one bash command; the template every new harness starts from and
the one the platform's tests use. `packages/harness-package-builder`: Claude Code seeded with the builder skill;
the built-in that `init` installs. Both carry a README and tests on recorded output.

---

## 6. Architecture

```
          clients (OpenAI SDK)            people (browser)
                 │                              │
                 ▼                              ▼
           ┌──────────┐                   ┌───────────┐
           │   api    │◄──────────────────│  console  │  (and chat; stateless apps on the public API)
           │ replicas │                   └───────────┘
           └────┬─────┘
    records     │  moments & jobs                   bytes
   ┌────────┐   │  ┌────────┐                   ┌──────────────┐
   │Postgres│◄──┼──│ Redis  │                   │ object store │
   └────────┘   │  └───┬────┘                   └──────┬───────┘
                │      │ queue                          │ (through the api)
                │  ┌───▼────┐   launcher      ┌─────────▼────────┐
                │  │ worker │───────────────► │   box (runner)   │  one per session, ephemeral
                │  └────────┘                 └─────────┬────────┘
                │                                       │ the only exit
                │   policy, records          ┌──────────▼───────┐
                └───────────────────────────►│      door        │────► providers, allowed hosts
                                             └──────────────────┘
```

One image, four roles of the same package: `api`, `worker`, `door`, plus `runner` inside the sandbox image. Admin
commands: `migrate`, `init`, `keygen`. The console and chat are two stdlib HTTP servers in `console/`, run from the
same image.

| Unit | Holds | Scales on | Loses nothing when it dies because |
|---|---|---|---|
| api | nothing (caches) | requests, open streams | records are in Postgres; streams are Redis; followers reconnect with `starting_after` |
| worker | nothing | queue depth | jobs are acknowledged only when done; duties are idempotent and re-run on the next tick |
| door | a policy cache per box token; the CA derived from the secret key | connections, bytes | the box's next connection lands on another door, which fetches the same policy |
| box | the session's files while alive | running sessions | the turn fails as lost; the session resumes from its last snapshot anywhere |
| Postgres | records, ledger, blob reference counts | the one thing sized | managed, backed up |
| Redis | event streams, the job queue, SSO state | memory | a lost Redis costs a delay: a reconciler re-queues what Postgres says is pending |
| object store | blobs, manifests, requests, responses, traces, install archives, files | itself | itself |

### 6.1 Who talks to whom

- Clients and the console talk to the **api** only.
- The **api** talks to Postgres, Redis and the object store. It never contacts a box, a door or a worker.
- The **worker** reads the queue and talks to Postgres, Redis, the object store and the launcher's substrate (the
  Docker socket self-hosted; a container service API in the SaaS).
- A **box** talks to the api only, through the door (every byte out of a box goes through the door, the api
  included). It authenticates with its box token.
- The **door** talks to the api (policy, records) with the door key, and to the internet on a box's behalf.

### 6.2 Process layout inside a box

```
/opt/hg/runner.py     the runner (in the sandbox image; Python standard library only)
/opt/hg/lib/hgkit     the package library
/opt/hg/package       the package folder (read-only, from the archive)
/opt/hg/tools         the install archive, extracted (read-only to run)
/run/hg               the turn's inputs (request, history, files, mcp)
/workspace            persisted
/home/hg              persisted per the manifest
/state                persisted
/tmp                  tmpfs, the box's own
```

The runner is PID 1's child; `run` is started under `setsid` in its own process group.

---

## 7. State and where it lives

### 7.1 Postgres: records

Every tenant table carries `org_id`; row-level security confines a transaction to the organization it declared
(`SET LOCAL hg.org`, `hg.scope = tenant`) unless it declared the platform scope. The database role is never a
superuser and never `BYPASSRLS`; the api refuses to start otherwise.

Tables (`migrations/0001_core.sql`):

| Table | Holds |
|---|---|
| `orgs` | id, name, status (`active`, `suspended`), `limits` jsonb (§7.4), `egress` jsonb, `data_key` (wrapped), created_at |
| `accounts`, `memberships`, `invitations`, `sign_ins` | people (§10) |
| `identity_providers` | one row per organization: the OIDC configuration (§10.3) |
| `api_keys` | keys with role, `run_only`, label, hint, expiry, revocation |
| `audit` | every change of standing |
| `providers` | name, api, base_url, sealed_key |
| `packages` | digest, name, version, manifest, object_key, bytes (shared, content-addressed) |
| `harnesses` | org, name, digest, manifest, settings, sealed secrets, default_provider, default_model |
| `installs` | org, digest, image id, status (`pending`, `ready`, `failed`), object_key, bytes, log tail |
| `files` | uploads |
| `sessions` | id, org, created_by, end_user, harness, digest, manifest, provider_id, model, store, status, turns, `snapshot_id`, forked_from, created_at, last_active_at |
| `turns` | id, org, session, seq, status, actor, end_user, model, provider_id, provider_model, stream, background, request_key, mcp, previous_id, cancel, hg_reason, hg_message, exit_code, response, events, last_batch, log_tail, stderr_tail, `snapshot_id`, created_at, started_at, ended_at, exited_at |
| `turn_events` | the durable event log: every non-delta event, `(turn_id, seq)` |
| `turn_bindings` | a turn's caller credentials, sealed |
| `boxes` | id, org, session, `token_digest`, status (`starting`, `running`, `standby`, `gone`), turn_id, launcher_ref, launched_at, last_seen_at, expires_at. **Unique on `session_id` where status ≠ `gone`**: one live box per session. |
| `snapshots` | id, org, session, turn, manifest_key, bytes (logical), files (count), created_at |
| `blobs` | (org, hash) pk, bytes, refs, created_at, updated_at. `refs` = how many snapshots name the blob. |
| `objects` | key pk, org, kind, bytes, created_at, deleted_at: every non-blob object, for storage accounting |
| `usage` | id, org, meter, quantity, kind, harness, session_id, response_id, start_at, end_at |
| `storage_accruals` | org, accrued_until |
| `traces` | id, org, response_id, kind (`harness`, `door`, `provider`), object_key, at |
| `idempotency` | (org, key) → request digest, turn id |
| `schema_migrations` | package, version |

Platform-only tables (no `org_id` policy): `packages` (readable by all), `boxes` is tenant-scoped but written on the
platform path, `objects`, `storage_accruals`.

### 7.2 Redis: moments

| Key | Type | Holds | Lifetime |
|---|---|---|---|
| `hg:turn:<rid>` | stream | every event of the turn, deltas included, as `{seq, type, json}` | trimmed to 20,000 entries; expires 10 minutes after the terminal event |
| `hg:jobs` | stream, consumer group `workers` | jobs (§7.5) | acknowledged when done; pending entries older than 5 minutes are reclaimed |
| `hg:wake:<sid>` | pub/sub channel | "a turn is queued for this session" | none |
| `hg:sso:<state>` | string | an OIDC exchange in flight (nonce, code verifier, redirect) | 10 minutes |

Rules: nothing in Redis is a record. A lost Redis loses in-flight deltas (clients reconnect and replay the durable
log) and queued jobs (the reconciler duty re-queues every box in `starting` with no launcher reference and every
pending install).

### 7.3 Object store: bytes

Keys under `orgs/<org>/…`:

| Key | What |
|---|---|
| `turns/<rid>/request.json`, `history.json` | the turn's request (placeholders, never values) and history |
| `turns/<rid>/traces/<ms>-<kind>-<id>.json` | observability records |
| `turns/<rid>/stdout.log`, `stderr.log` | full logs when observability is `full` |
| `sessions/<sid>/snapshots/<snapshot_id>.jsonl` | a manifest (§9.5) |
| `sessions/<sid>/ops/<op_id>` | a file staged through the Containers API |
| `blobs/<sha256>` | content, shared by every snapshot of the organization |
| `installs/<digest>-<image>.tar.zst` | an install archive |
| `files/<file_id>` | an upload |

Shared: `packages/<digest>.tgz`. The store is `file:///path` (one host; served by the api) or `s3://bucket/prefix`.
Boxes never hold store credentials: every byte a box reads or writes goes through the api with the box token.

### 7.4 Organization limits

`orgs.limits` over configured defaults (`HG_DEFAULT_*`):

| Field | Default | Meaning |
|---|---|---|
| `vcpus` | 2.0 | a box's CPU cap |
| `memory_mb` | 4096 | a box's memory cap for processes; `/tmp` is on top |
| `tmp_mb` | 4096 | the box's tmpfs size |
| `pids` | 1024 | process limit |
| `max_concurrent_turns` | 0 (none) | turns in progress at once |
| `standby_s` | 120 | how long a box waits for the session's next turn after one ends |
| `turn_timeout_s` | 0 (none) | the bound on a turn, with the package's own |
| `retention_days` | 0 (keep) | sessions idle longer are deleted with their snapshots |
| `observability` | `metadata` | `full` keeps bodies and whole logs |

### 7.5 Jobs

`{"kind": …, …}` on `hg:jobs`, each idempotent (mail is sent by the api process that creates an invitation, not queued):

| Kind | Payload | Done when |
|---|---|---|
| `launch` | box_id | the launcher started the box and recorded its reference, or the box is `gone` and its turn failed |
| `install` | install_id | an install box ran and the archive is recorded, or the install is `failed` |
| `stop` | box_id | the launcher stopped the box (a lost lease, a suspended organization) |
| `delete_session` | session_id | the session's objects are gone and its snapshots' blob references decremented |

### 7.6 Duties

Run by every worker on a tick, each under a Postgres advisory lock so one runs at a time:

| Duty | Every | Does |
|---|---|---|
| `lost` | 15 s | boxes whose `expires_at` passed: status `gone`; their running turn fails `node_lost`→ `box_lost`; a `stop` job for the launcher |
| `reconcile` | 30 s | boxes `starting` without a launcher reference for over a minute, installs `pending` without a job: re-queued |
| `retention` | 5 min | sessions past their organization's retention: `delete_session` jobs |
| `purge` | 5 min | unstored sessions idle 10 minutes; spent idempotency keys and sign-in tokens after a day; `objects` rows deleted over a day |
| `gc` | 10 min | blobs with `refs = 0` untouched for an hour: object deleted, row deleted |
| `accrue` | 1 h | storage GiB-hours per organization since its last accrual (§12.2) |
| `reap` (Docker launcher only) | 1 min | exited box containers and their networks removed |

---

## 8. Security

### 8.1 Isolation

A box is a container from the sandbox image with: a read-only root, a tmpfs `/tmp` of `tmp_mb` with `exec`, no
capabilities, `no-new-privileges`, uid 10001, `--cpus`, `--memory` (`memory_mb + tmp_mb`), `--pids-limit`, no DNS,
one hosts entry (`hg-door`), and bind mounts only for its own directories. Self-hosted, Docker runs it under gVisor
(`runsc`) when installed; `HG_SANDBOX_RUNTIME=runc` is allowed and logged as unisolated. In the SaaS the container
service's micro-VM is the isolation.

### 8.2 Egress

An egress entry is `host`, `*.domain` or `*`, with an optional `:port` (443 by default); `!` in front denies.
`*.domain` matches subdomains at any depth, never the domain. A policy allows a host when some allow entry matches
and no deny entry does. Levels compose by union of entries: the organization's, the package's (`run.egress`, the
catalog's MCP hosts, the secrets' hosts, the provider's host for the turn), and the platform hosts (the api).
Install boxes get the organization's entries plus `install.egress` plus the platform hosts.

The box's only route is the door. Self-hosted: a per-box internal Docker network whose only other member is the
door container. SaaS: sandboxes in private subnets with no internet route and a security group permitting only the
door. A process that ignores its proxy variables reaches nothing.

### 8.3 Secrets

- Each organization has a data key, wrapped by the key wrapper (local AES-GCM under `HG_SECRET_KEY`, or AWS KMS).
  Provider keys, harness secret values, the OIDC client secret and a turn's caller credentials are sealed under it
  with the owning record as associated data.
- A box holds **placeholders**. The door holds, per box token and only while a turn runs, the bindings
  `{placeholder, value, hosts}`: the provider key bound to the provider's host (`HG_PROVIDER_TOKEN`), each harness
  secret bound to its declared hosts and its catalog MCP hosts, each caller MCP credential bound to its server's
  host. The door intercepts TLS only for bound hosts, with a certificate from its CA, verifies the real server, and
  replaces placeholders in request header values. Bodies, URLs and responses are never touched; a placeholder sent
  anywhere else is sent as the random string it is.
- The door's CA is derived deterministically from `HG_SECRET_KEY`, so every door replica has the same CA with no
  shared file, and the api can hand its certificate to boxes.
- The **box token** (`hgb_…`) authenticates a box to the api and to the door (as the proxy credential). It can do
  nothing but report for its own session and fetch its own session's objects. It is useless outside the sandbox
  network, where the door is the only listener. The api keeps its digest.
- The **door key** (`HG_DOOR_KEY`) authenticates doors to the api's door routes.
- The api keeps **digests** of every secret token it issues (keys, sign-ins, invitations, box tokens).

### 8.4 Access

Two roles, `admin` and `developer`, plus `run_only` keys. One table decides every action
(`domain/access.py`); a refusal names who may. Run-only keys see only the sessions, files, usage and traces they
made. A platform scope (the operator key or a platform-admin account) sees the organization's health and
configuration. Every change of standing is audited.

### 8.5 Platform limits

Request body 32 MiB; a file upload 512 MiB; a package upload 64 MiB; any other JSON body 1 MiB; one stdout line
64 MiB; a box report 8 MiB; a blob 4 GiB; a manifest 64 MiB. None of these limits what a harness does.

---

## 9. Lifecycle of a turn

### 9.1 Create (`POST /v1/responses`)

1. Authenticate (§10.5). Validate the body against the SDK's request type. Reject `conversation`.
2. `Idempotency-Key`: replay the stored turn for the same body digest; 409 for a different body.
3. Resolve the harness by `model`'s first segment. Check required settings and secrets are set.
4. Resolve the session (§9.3). Resolve the provider (§9.4) and the model name.
5. Resolve MCP: the catalog narrowed or extended by the request; caller credentials become placeholders, the values
   sealed into `turn_bindings`. The stored request holds placeholders.
6. `extensions.admit(principal, admission)` may refuse.
7. Write the request object, the turn row (`queued`), `response.created` to the durable log and the stream.
8. Ensure a box: if the session has a live box, publish on `hg:wake:<sid>`; else insert a `starting` box with a
   fresh token and queue `launch`. Check `max_concurrent_turns` here: a turn over the limit stays queued and the
   box is ensured when a turn ends.
9. Reply: the stream, the queued `Response` (background), or wait for the terminal event (foreground; cancel on
   disconnect).

### 9.2 Run (the box)

1. The runner starts with `HG_BOX_ID`, `HG_BOX_TOKEN`, `HG_API_URL`, `HG_DOOR`. It sets its proxy variables to the
   door, fetches `GET /internal/boxes/{id}` (session, package digest, limits, CA certificate, install archive id),
   downloads the package archive and the install archive through the api, extracts them, writes the trust bundle,
   seeds the home, restores the session's snapshot (§9.5), and reports `ready`.
2. `GET /internal/boxes/{id}/next?wait=…` long-polls for the session's next turn (the api waits on `hg:wake:<sid>`
   and re-checks Postgres). The reply is the **job**: turn id, seq, model, timeout, grace, the request, history,
   files and staged ops to download, the environment, the MCP list, the provider, the log links, and whether to
   persist. The api marks the turn `in_progress` with `started_at`, emits `response.in_progress`, and gives the
   door the turn's policy version.
3. The runner writes `/run/hg`, applies staged file operations to the workspace, and starts `run` under `setsid`
   with the request on stdin and §5.3's environment. It pumps stdout in batches (about 1 MiB or every 200 ms,
   whole lines) to `POST /internal/boxes/{id}/turns/{rid}/events`; the reply says whether the turn has concluded
   and whether to stop. It heartbeats every 3 s (`POST …/heartbeat`), which renews the box lease and returns
   `cancel`. Stderr is tailed (and written whole when observability is `full`).
4. The turn ends when: stdout reaches EOF (exited); the api says stop (invalid output); the heartbeat says cancel;
   the terminal event was seen and `stop_grace_s` passed; or the timeout passed. In every case but a plain exit the
   runner SIGTERMs the group, waits the grace, then SIGKILLs every process in the box but itself. **After every
   turn, whatever its outcome, the runner kills every process it did not start**, so standby holds nothing.
5. The runner reports `POST …/finish` with the outcome, exit code, stderr tail, byte counts, the box's cgroup CPU
   seconds and memory high-water mark, and whether it holds state to persist. The api concludes the turn if no
   terminal event did (§4.2), records usage, revokes the policy version (the door drops the bindings), writes the
   harness trace.
6. Snapshot (§9.5), then `POST …/saved`. Then back to step 2. If no turn arrives within the organization's
   `standby_s`, the runner reports `bye` and exits. The box is `gone`.

### 9.3 Sessions, forks, switches

- No `previous_response_id`: a new session on the harness's current package.
- `previous_response_id` naming the session's last turn, same harness: the next turn of that session, serial after
  any turn still running.
- Naming an earlier turn of the session: a **fork**: a new session whose `snapshot_id` is that turn's checkpoint
  and whose `forked_from` is that turn. No bytes copy. The parent keeps its own line.
- Naming a turn of another harness: a **switch**: a fork whose first snapshot is the checkpoint reduced to its
  workspace entries (a new manifest listing only `workspace/…` paths, sharing the blobs), on the new harness's
  package, starting from its home seeds. The conversation carries as the history every turn receives.
- A turn whose checkpoint was never saved (lost before saving) cannot be forked from: 409 naming the reason.
- A session pins the package digest it started on; reinstalling a harness changes only new sessions.

Rewind and branch in the chat are forks; nothing in the platform is specific to them.

### 9.4 Providers

Named in `model`, else the session's, else the harness's `default_provider`, else the organization's only provider
of an API the package speaks. The provider's API must be among the package's `apis`. The turn gets
`HG_PROVIDER_URL` = the provider's base URL and `HG_PROVIDER_TOKEN` = a placeholder bound to the provider's host
with the sealed key as value.

### 9.5 Snapshots: content-addressed, incremental

**Manifest.** JSON Lines, one object per entry, sorted by path, paths relative to the box root
(`workspace/…`, `home/hg/…`, `state/…`):

```
{"p": "workspace/src/a.py", "t": "f", "m": 420, "mt": 1730000000, "s": 1234, "h": ["<sha256>"]}
{"p": "workspace/big.bin",  "t": "f", "m": 420, "mt": 1730000000, "s": 9000000, "h": ["<sha256 of chunk 1>", "<chunk 2>", "<chunk 3>"]}
{"p": "workspace/empty",    "t": "d", "m": 493}
{"p": "home/hg/.config/x",  "t": "l", "target": "../y"}
```

Files over 4 MiB are split into 4 MiB chunks; each chunk is a blob. Blobs are stored raw under their SHA-256.
Sockets, devices and anything else are not persisted. Included roots: `workspace`, `state`, and `home/hg/<p>` for
each `persist` entry (`.` is the whole home).

**Save (the runner, after a turn).** Walk the included roots; compare each entry with the restored manifest by
type, size and mtime; hash only what changed; build the new manifest; if it equals the restored one, report
`unchanged`. Otherwise `POST …/blobs` with the set of hashes new to this manifest → the api answers which it lacks;
upload each missing blob with `PUT …/blobs/<hash>` (the api verifies the hash of what it receives, refuses a
mismatch, stores it, records its size); then `PUT …/turns/{rid}/manifest` with the manifest itself. The api checks
that it holds every hash the manifest names, increments `refs` for each, stores the manifest, inserts the snapshot,
sets `turns.snapshot_id` and advances `sessions.snapshot_id`. A turn whose state was unchanged reports
`POST …/turns/{rid}/saved` and gets the session's current snapshot as its checkpoint; a save that failed reports the
same with an `error`, logged, and the session's snapshot stays as it was.

**Restore (the runner, at box start or on a switch).** Fetch the manifest, create directories, fetch every blob
not already present in the box's local cache (`/tmp/hg/blobs`, kept for the box's life), write files and symlinks,
set modes and mtimes. Fetches run with a small concurrency.

**Delete.** Deleting a session decrements `refs` for every hash in each of its manifests (read from the store)
and deletes the manifests. `gc` removes blobs at zero.

**Metering.** Live bytes for an organization = `sum(bytes) of blobs with refs > 0` + `sum(bytes) of live objects`.

**Why not an archive, git or a backup tool.** An archive re-uploads everything each turn and makes a fork a copy.
Git keeps no mtimes, handles binaries and the home badly, and collides with the agent's own git use. A backup tool
would need store credentials in the box. The manifest is three hundred lines and gives deduplication across every
session of an organization.

### 9.6 Installs

The first time a harness is installed in an organization, and whenever the sandbox image changes, an `installs`
row is `pending` and an `install` job is queued. The worker launches an install box (no session, no snapshot, the
install egress); the runner downloads the package, runs `install` into the tools directory, archives it
(`tar | zstd` into `installs/<digest>-<image>.tar.zst` through the api), and reports. A turn whose install is
`pending` waits in `queued`; a `failed` install fails the turn with `hg_reason: install_failed` and the tail. Installs
are metered as agent-seconds of kind `install`.

### 9.7 Cancel, delete

`POST /v1/responses/{id}/cancel`: a queued turn ends now as `cancelled`; a running one is marked, the runner learns
on its next heartbeat or batch. `DELETE /v1/responses/{id}` removes an ended turn's record and objects (its
snapshot stays if it is the session's current one; its blob references are released otherwise). `DELETE
/v1/containers/{id}` ends a session: refuses while a turn runs; queues `delete_session`.

---

## 10. Identity

### 10.1 Accounts

A person has one account (email is the login), memberships in organizations with a role, invitations that activate
the account by setting a password (one-time, seven days), sign-ins (opaque bearer tokens, twelve hours, revoked by
logout, password change, disabling). Passwords are scrypt; ten failures lock fifteen minutes. The last active admin
of an organization cannot be demoted or disabled. `init` creates the first admin (a platform admin) with its
invitation and an admin key, printed once.

### 10.2 Keys

`hg_` + 40 characters, kept as an HMAC digest. A key is named after the application it is for, has a role, may be
`run_only`, may expire. A developer creates developer keys; an admin any key.

### 10.3 SSO (OIDC)

One identity provider per organization, configured by an admin at `PUT /v1/sso`:

| Field | Meaning |
|---|---|
| `issuer` | the OpenID issuer; discovery at `<issuer>/.well-known/openid-configuration` |
| `client_id`, `client_secret` | the registered client; the secret is sealed |
| `domains` | email domains allowed to sign in through it |
| `auto_provision` | create an account on first sign-in (else the person must be invited first) |
| `default_role` | `developer` or `admin` for auto-provisioned accounts |
| `role_claim`, `admin_values` | a claim (e.g. `groups`) whose values grant `admin`; others are `developer` |
| `required` | password sign-in disabled for everyone but platform admins |

Flow: `GET /v1/auth/sso/start?redirect=<console url>` → the api stores state, nonce and a PKCE verifier in Redis and
redirects to the provider → the provider redirects to the console's `/sso/callback?code&state` → the console posts
them to `POST /v1/auth/sso/exchange` → the api exchanges the code, verifies the ID token (signature via JWKS,
issuer, audience, nonce, expiry), maps claims (email, name, role), provisions or finds the account, updates the
membership role when the claim says so, and returns a sign-in. Library: Authlib. Local accounts remain for the first
admin and break-glass.

### 10.4 Audit

Every change of standing (accounts, keys, providers, egress, harnesses, SSO, sign-ins and their failures) is a row
with actor, action, target and detail, never a secret value. Admins read their organization's; the platform scope
reads all.

### 10.5 Principals

A request runs as a principal: organization, actor (`account:<id>` or `key:<id>`), role, `run_only`,
`platform_admin`. Resolved from an extension (`authenticate`), an API key, or a sign-in token, in that order.

---

## 11. The door

A single program with one listener, `:3128`, a CONNECT proxy with proxy authentication: `Proxy-Authorization:
Basic base64("box:<box token>")`.

Per connection: read the CONNECT line and the credential; fetch the policy for the token from the api
(`POST /internal/door/policy`, door key; cached by token for 30 s and dropped when the policy version changes);
refuse (403) a host the policy does not allow or a credential the api does not know; resolve the host itself and
connect only to an address it checked (public only unless `HG_ALLOW_PRIVATE_EGRESS`; platform hosts always);
tunnel untouched unless the host is bound, in which case intercept: TLS with the box as the server for that host
(a certificate from the door's CA, HTTP/1.1 only), TLS to the real server verified normally, one request head read
and rewritten (placeholders replaced in header values, `Connection: close`), everything after spliced until either
side closes. Count bytes each way. Record one line per connection (host, port, verdict, intercepted, bytes,
duration; for intercepted hosts also method, path and the response status), batched to
`POST /internal/door/records` every few seconds, with the box token's digest.

`HG_UPSTREAM_PROXY` sends outbound connections through an operator's proxy; `HG_CA_FILE` adds an operator CA to
what the door trusts upstream.

The policy the api returns: `{version, org_id, turn_id, allow: [entries], bindings: [{placeholder, value, hosts}],
platform: [host:port, …]}`. Between turns (standby) the policy has no bindings and allows only platform hosts.

---

## 12. Metering

### 12.1 Meters

| Meter | Unit | Kind | Source |
|---|---|---|---|
| `agent_seconds` | seconds | `turn`, `standby`, `install` | the api: `started_at` to `ended_at` of a turn; standby from the box's `finish` to its next `next` or `bye`; install from launch to report |
| `cpu_seconds` | seconds of CPU used | `turn`, `install` | the runner's cgroup reading, reported at finish |
| `memory_gib_seconds` | GiB × seconds allocated | `turn`, `standby`, `install` | `memory_mb + tmp_mb` × duration, the api |
| `ingress_bytes`, `egress_bytes` | bytes through the door | `turn`, `standby` | the door's records |
| `storage_gib_hours` | GiB × hours held | `storage` | the `accrue` duty |

Ids are generated where measured; the ledger is append-only and idempotent by id. `extensions.meter` sees every
batch after it is stored. The customer-facing figures are `agent_seconds` (turn) and `storage_gib_hours`; the rest
is the operator's cost record and the extension's to rate.

### 12.2 Storage accrual

Hourly, per organization: `(live blob bytes + live object bytes) / GiB × hours since the last accrual`.

### 12.3 Summary

`GET /v1/usage?start&end&group_by=harness|session|day|created_by|user` sums the organization's usage; a run-only
key sees its own sessions'. The platform scope sums across organizations by organization or day.

---

## 13. The extension seam

`HG_EXTENSIONS=module:Class,…`. Each class is instantiated once per process and may implement any of:

```python
class Extension:
    migrations: str | None = None                         # an importable package of NNNN_*.sql, applied after the core's
    def bind(self, platform: Platform) -> None: ...      # the composition root, once
    def router(self) -> APIRouter | None: ...            # mounted on the api
    async def authenticate(self, request) -> Principal | None: ...   # before keys and sign-ins
    async def admit(self, principal, turn: Admission) -> None: ...    # raise Refused (429) or any ApiError
    async def meter(self, records: list[UsageRecord]) -> None: ...    # after the core stores them; never fails a turn
    async def observe(self, record: TraceRecord) -> None: ...
    async def turn_ended(self, summary: TurnSummary) -> None: ...
    def launcher(self, settings) -> Launcher | None: ...               # a substrate of its own (a container service)
```

Services the extension may call through `Platform`: `orgs` (create, get, update limits and status, egress),
`accounts`, `keys`, `sso`, `usage.summary`, `harnesses`, `providers`. The SaaS adds organizations and their
identity providers, plans as limits, admission, rating of the ledger, billing, an operator surface, and its own
launcher. It never reads a secret value, never bypasses row-level security, never changes a turn's behaviour.

---

## 14. The HTTP API

All routes answer JSON in OpenAI's shapes; lists are `{"object": "list", "data", "first_id", "last_id", "has_more"}`.

### 14.1 OpenAI-shaped (any key or sign-in; run-only keys see their own)

| Route | Does |
|---|---|
| `POST /v1/responses` | create a turn (§9.1) |
| `GET /v1/responses/{id}` | the Response; `?stream=true&starting_after=N` follows |
| `POST /v1/responses/{id}/cancel`, `DELETE /v1/responses/{id}` | §9.7 |
| `GET /v1/responses/{id}/input_items` | the request's input items |
| `POST /v1/files`, `GET /v1/files`, `GET /v1/files/{id}`, `GET /v1/files/{id}/content`, `DELETE /v1/files/{id}` | uploads for `file_id` references |
| `GET /v1/containers`, `GET /v1/containers/{id}`, `DELETE /v1/containers/{id}` | sessions; `?user=` filters by end user |
| `GET /v1/containers/{id}/responses` | the session's turns in order |
| `GET /v1/containers/{id}/files`, `POST …/files`, `GET …/files/{fid}`, `GET …/files/{fid}/content`, `DELETE …/files/{fid}` | the workspace as the current snapshot lists it, plus staged operations applied before the next turn |
| `GET /v1/containers/{id}/traces`, `GET /v1/traces/{response_id}` | observability records |
| `GET /v1/models`, `GET /v1/models/{id}` | every `harness/provider[/model]` the organization can send |

### 14.2 Management

| Route | Who | Does |
|---|---|---|
| `GET /v1/platform` | anyone | version, extensions |
| `GET /v1/organization` | any | the organization, its limits, the caller's principal |
| `GET/POST /v1/keys`, `DELETE /v1/keys/{id}` | developer+ | keys |
| `GET /v1/providers`, `PUT/GET/DELETE /v1/providers/{name}` | read: any; write: admin | providers |
| `GET/PUT /v1/egress` | read: developer; write: admin | the organization's entries |
| `GET/POST /v1/harnesses`, `GET/PATCH/DELETE /v1/harnesses/{name}`, `GET …/files`, `GET …/files/{path}`, `GET …/install` | developer+ | install (multipart `package`), configure settings, secrets and defaults, read the package, the install's status |
| `GET /v1/usage` | any | §12.3 |
| `POST /v1/auth/login`, `/accept`, `/logout`, `GET /v1/auth/me`, `POST /v1/auth/password` | people | §10.1 |
| `GET /v1/auth/sso`, `GET /v1/auth/sso/start`, `POST /v1/auth/sso/exchange` | anyone / people | §10.3 (`GET /v1/auth/sso` says whether SSO is configured and its display name) |
| `GET/PUT/DELETE /v1/sso` | admin | the identity provider |
| `GET/POST /v1/accounts`, `GET/PATCH /v1/accounts/{id}`, `POST …/reset` | admin | people |
| `GET /v1/audit` | admin | the organization's audit log |

### 14.3 Internal

Boxes (box token): `GET /internal/boxes/{id}`, `POST …/ready`, `GET …/next?wait=`, `POST …/turns/{rid}/events`,
`POST …/turns/{rid}/heartbeat`, `POST …/turns/{rid}/finish`, `GET …/objects/{key}` (package archive, install
archive, request, history, files, ops, manifests), `GET …/blobs/{hash}`, `POST …/blobs` (which are missing),
`PUT …/blobs/{hash}`, `PUT …/turns/{rid}/manifest` (the snapshot after the turn), `POST …/turns/{rid}/saved`
(unchanged, or the save failed), `PUT …/turns/{rid}/logs/{stdout|stderr}`, `POST …/installed` (an install box's
result, with the archive streamed to `PUT …/archive`), `POST …/bye`.

`GET …/objects/{key}` serves a session box only its own session's objects, its own session's turns' objects, its
organization's install archives and uploaded files, and the shared package archives; an install box, package archives
only. Anything else is 403.

Doors (door key): `POST /internal/door/policy`, `POST /internal/door/records`.

Platform scope (operator key or platform admin): `GET /internal/health`, `GET /internal/metrics` (Prometheus text),
`GET /internal/boxes`, `GET /internal/config`, `GET /internal/versions`, `GET /internal/audit`, `GET /internal/usage`,
`GET/PATCH /internal/orgs/{id}` (limits, status, egress), `POST /internal/orgs/{id}/accounts` (invite an admin),
`POST /internal/boxes/{id}/stop`.

### 14.4 Health

`GET /healthz` (process up), `GET /readyz` (Postgres and Redis reachable).

---

## 15. Repository layout and build order

```
harnessgateway/
  __main__.py            the command: api | worker | door | migrate | init | keygen
  config.py              HG_* settings, validated at start, every problem reported at once
  errors.py  ids.py  limits.py  crypto.py  netguard.py
  contract/              the Responses reader and writer (re-exports hgkit's validator) and routing
  domain/                names, egress, manifest, package, mcp, access, limits, meters, turn states — pure
  ports/                 blob store, key wrapper, mailer, launcher
  adapters/              blob_fs, blob_s3, keys_local, keys_kms, mail_smtp, launch_docker
  store/                 db.py (Postgres, scoped transactions, migrations), redis.py (streams, queue, pubsub), objects.py (the ledger over the blob store)
  services/              orgs, accounts, sso, keys, audit, vault, providers, harnesses, installs, files, containers, turns, boxes, snapshots, traces, usage, operations, platform (composition root)
  api/                   http.py (principals, body limits, envelope), responses, storage, manage, identity, internal, app.py
  worker/                jobs (launch, install, stop, delete_session), duties, main loop
  door/                  the proxy
  extensions.py
  migrations/0001_core.sql
sandbox/
  Dockerfile             the sandbox image: runtimes, hgkit, the runner
  runner.py              standard library only
  hgkit/                 __init__.py, validate.py, __main__.py
packages/shell, packages/harness-package-builder
console/                 server.py, static/, chat/
deploy/                  compose.yaml, install.sh, postgres-init.sh, .env.example
docs/                    operations.md, api.md, packages.md, console.md, security.md, extensions.md
tests/unit, tests/services, tests/runner, tests/integration, tests/e2e
scripts/version-check, scripts/sync-builder-seeds
Dockerfile, pyproject.toml, README.md, CHANGELOG.md, AGENTS.md, LICENSE
.github/workflows/ci.yml, release.yml
```

Build in this order; each step is shippable and tested on its own:

1. **Foundations**: `pyproject`, `config`, `errors`, `ids`, `limits`, `crypto`, `netguard`, `contract`, `domain`,
   `hgkit`. Unit tests.
2. **Snapshots**: the manifest format, walk and diff, hashing and chunking, negotiation, restore — as a module
   usable by both the runner (stdlib) and the api. Unit tests over a directory tree with a fake store.
3. **Store and services**: schema with RLS, Redis streams and queue, the ledger, every service, the composition
   root. Service tests on a real Postgres and Redis with the directory store, driving the box routes with a fake
   runner.
4. **API**: routers, principals, SSO. Service tests through the ASGI app.
5. **Runner**: run by hand in a container against a local api; `tests/runner` runs it against an in-process fake
   api with the shell package.
6. **Door**: unit tests with local TLS echo servers.
7. **Worker and Docker launcher**: the self-hosted stack from compose; integration tests against it.
8. **Console and chat**: ported onto the API; SSO login; the boxes page replaces nodes. Browser tests.
9. **Packages, docs, deploy, CI, release.**

---

## 16. Checks

CI on every push: `ruff check .`, `mypy`, `pytest tests/unit packages tests/runner`, `scripts/version-check`, no
forbidden words; then `tests/services tests/e2e` on a real Postgres and Redis; then the two images build.
Integration (`HG_TEST_STACK=1`) runs against a live stack: a turn in a sandbox, continuation, fork, switch, stream,
cancel, egress allow and deny, secrets never leaving, container files, the operator view.

Done means: `sudo ./deploy/install.sh` on a Linux host with Docker brings up Postgres, Redis, api, worker, door and
console; `init` prints the first admin's invitation and a key; the console installs `shell` from the template and
the builder is already there; a turn runs, continues, forks, switches, streams and cancels through the chat; usage
shows agent-seconds and storage; SSO signs a person in against a real provider.

---

## 17. Settings

Every setting is an environment variable with the `HG_` prefix. Secrets never appear in logs or the config route.

| Variable | Role | Meaning |
|---|---|---|
| `HG_DATABASE_URL` | api, worker | Postgres, an ordinary role |
| `HG_REDIS_URL` | api, worker | `redis://host:6379/0` |
| `HG_BLOB_URL` | api, worker | `file:///path` or `s3://bucket/prefix?region=&endpoint=` |
| `HG_SECRET_KEY` | api, worker, door | 32+ random bytes, base64. Derives the local master key, the token pepper, the door CA |
| `HG_KMS_KEY` | api, worker | AWS KMS key for wrapping instead of the local master key |
| `HG_OPERATOR_KEY` | api | `hgo_…`: the platform scope's key |
| `HG_DOOR_KEY` | api, door | `hgd_…`: doors to the api |
| `HG_API_URL` | worker, door, boxes | the api as boxes and doors reach it |
| `HG_DOOR` | worker | `host:port` of the door as boxes reach it (self-hosted: `hg-door:3128`) |
| `HG_SANDBOX_IMAGE`, `HG_PLATFORM_IMAGE` | worker | images |
| `HG_SANDBOX_RUNTIME` | worker | `runsc` (default) or `runc` |
| `HG_LAUNCHER` | worker | `docker` (default); an extension may register another |
| `HG_DOOR_CONTAINER` | worker | the door container's name, joined to each box's network (Docker launcher) |
| `HG_BOX_DIR` | worker | where the Docker launcher keeps each box's writable directories, mounted at the same path inside and out |
| `HG_UPSTREAM_PROXY`, `HG_CA_FILE`, `HG_ALLOW_PRIVATE_EGRESS` | door, api | an operator's proxy and CA; private addresses allowed |
| `HG_CONSOLE_URL` | api | where people open the console; invitation and SSO links |
| `HG_SMTP_URL`, `HG_MAIL_FROM` | api | mail: invitations are sent by the api that creates them |
| `HG_DEFAULT_*` | api | §7.4 defaults |
| `HG_EXTENSIONS` | all | §13 |
| `HG_HOST`, `HG_PORT`, `HG_LOG_LEVEL` | all | listening and logging |

---

## 18. What this version removes from the previous platform

Nodes, pools, join tokens, slots, warm limits, node-local caches, the gateway as a separate role and its second
token, the per-node CA files, tar snapshots and copied forks, the `vcpu_seconds` meter, `capacity.node_pool` and
`max_sandboxes`, the chat's own key. Replaced by boxes, the worker and launcher, the door as the one secret
mechanism, content-addressed snapshots, `agent_seconds`, `standby_s`, `max_concurrent_turns`, and SSO.
