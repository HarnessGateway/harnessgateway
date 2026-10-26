# Changelog

Every release is a section here, newest first; the newest section's heading is the version. Versions start over at
1.0.0 with the rebuild: what came before it is in the git history.

## [1.0.0]

The first release of the rebuilt platform: a multi-tenant **agent harness runtime**. An organization calls any coding
agent (Claude Code, Codex, Hermes, Pi, or a harness of its own) with the OpenAI Responses API; the platform runs it in
a sandbox it owns, with the organization's own model provider, and bills the infrastructure the run used. One
platform serves managed SaaS, self-hosted and hybrid deployments.

### What the platform is

- **The contract is the OpenAI Responses API, in and out.** A client uses any OpenAI SDK, unchanged. The request goes
  to the harness untouched: the platform reads only `model`, `previous_response_id`, `stream`, `background`, `store`
  and the `file_id`s it must deliver. What the harness prints is Responses stream events, each validated against the
  OpenAI SDK's own models; the platform owns the envelope (`response.created`, `response.in_progress`, the sequence
  numbers) and the final `Response`.
- **A harness package decides everything harness-specific.** The platform has no code for any harness. A package is
  `harness.json` (contract 1: name, version, provider shapes, default model, install and run egress, timeouts, stop
  grace, settings), `install.sh` (installs the CLI, pinned, into `$HG_TOOLS`), `request.sh` (maps the request onto
  the CLI and its output onto events) and optional `seeds/workspace` and `seeds/home` (instructions, skills, MCP
  config, CLI settings). Packages are content-addressed by digest and come from the built-in catalog, any public git
  repository (the open, free marketplace) or a private upload. An organization's harness is an organization's asset.
- **`model` routes the call**: `harness`, `harness/provider` or `harness/provider/model`.
- **A session is a sandbox with memory.** A response's `metadata.session_id` is an OpenAI container
  (`/v1/containers`). `previous_response_id` continues it: the same workspace, the agent's home (its own conversation
  history, settings, skills) and the package's state, turn after turn. The box stays warm between turns and is
  snapshotted after each one, so any node can restore it. `store: false` runs one turn and discards the session.

### Execution

- **Sandbox**: every session runs in its own gVisor (`runsc`) container as uid 10001, with no Linux capabilities,
  `no-new-privileges`, a read-only root, a size-limited `/tmp` and per-organization CPU, memory and process limits.
  The node refuses any other runtime unless the operator sets `HG_INSECURE_RUNTIME=1` (development only).
- **Network**: each sandbox sits alone on an internal Docker network. Its only way out is its egress door: a CONNECT
  proxy to the package's allowed hosts (resolved and checked by the door, private addresses refused) and a forwarder
  to the provider gateway. Every byte through the door is counted.
- **Bring your own key**: providers (`anthropic` or `openai` shaped, any base URL) are stored encrypted (AES-256-GCM
  envelope encryption; a key derived from `HG_SECRET_KEY` or AWS KMS). A turn gets a short-lived token; the gateway
  swaps it for the real key on each call, allows only the provider API's paths, and revokes it when the turn ends.
  The key never enters a sandbox or a node.
- **Turn lifecycle**: `queued → in_progress → completed | incomplete | failed | cancelled`. Streaming (SSE),
  background turns, retrieval with replay (`starting_after`), cancel (SIGTERM to the package, then SIGKILL to every
  process in the sandbox after its grace), `Idempotency-Key`, and a foreground turn stopped when its client leaves.
  Cancelled and timed-out turns keep the output items they had finished. Every platform-decided ending names its
  reason in `metadata.hg_reason`.
- **Inputs and files**: `/v1/files` are delivered into the sandbox (`$HG_FILES`); files written to a container are
  staged and land in the workspace when its next turn starts; a container's files are read from its last snapshot.
- **Scheduling**: Postgres is the queue (`FOR UPDATE SKIP LOCKED`). Nodes pull work; organizations with the fewest
  running turns go first; a session runs one turn at a time, in order, and returns to the node holding its warm box.
  Nodes heartbeat; a silent node's turns fail cleanly and its sessions restore elsewhere. Nodes can be drained or
  removed; a node rejoins a control plane that forgot it.

### Multi-tenancy

- **Organizations** with members, roles (owner, admin, member), API keys (`sk-hg-…`, stored as HMAC digests, shown
  once, optional expiry) and invite-only sign-up (`POST /v1/signup` with an operator's invite code).
- **Isolation at every layer**: row-level security on every table, forced for the table owner, and a server that
  refuses a database role able to bypass it; every object under `orgs/<org>/` in storage, reached by nodes only through
  signed, expiring, single-object URLs; one sandbox per session.
- **Per-organization ceilings**, set by the operator: sandbox size, running turns, sandboxes held, turn timeout, idle
  time, session retention, workspace size, storage, requests per minute, a monthly spending cap, observability, and
  dedicated nodes (an organization's turns run only on nodes joined for it: the hybrid deployment).

### Billing and observability

- **Every infrastructure cost is metered**: `compute.vcpu_seconds` and `compute.memory_gb_seconds` (allocated size ×
  time a sandbox or install exists), `network.egress_bytes` and `network.ingress_bytes` (the egress door and the API),
  and `storage.gb_hours` (accrued hourly from the object ledger). `/v1/usage` prices them at the deployment's unit
  costs (`HG_PRICES`) times the markup (`HG_MARKUP`, default 3). Model tokens are the organization's own provider bill.
- **Traces** (with observability on): `/v1/traces/{id}` returns the request the harness received, every event, its
  full stdout and stderr, its exit code, and each exchange with the provider.

### Built-in harnesses

| Harness | CLI | Provider shapes |
|---|---|---|
| `claude-code` | Claude Code 2.1.310 | anthropic |
| `codex` | OpenAI Codex 0.164.0 | openai (Responses API) |
| `hermes` | Hermes Agent 0.19.0 | anthropic, openai |
| `pi` | Pi 1.4.0 | anthropic, openai |
| `shell` | none (the reference package: runs the input as a bash command) | none |

Each maps instructions, reasoning effort, input files, MCP tools and structured output (natively where the CLI has
it; Hermes asks for the JSON in its prompt) to its CLI, streams text,
reasoning, shell calls, file edits, MCP and function calls as Responses items, reports usage, resumes the CLI's own
session on continuation, and ships offline tests over streams recorded from real runs. Each installs from its
published npm or PyPI package and names no CPU, so it installs on x86_64 and ARM64 workers alike.

### The Harness Package Builder skill

`skills/harness-package-builder/` teaches an organization to build its own package: the contract, the Responses
event cookbook, which request fields to map, seeding, and verification. It ships a minimal example, `hgkit` (the
helper library in every sandbox: `Request`, `Emitter`, `State`, settings and input files) and `validate.py`, which
checks a package statically (including that no script names a CPU) and runs one turn locally against a recorded or
live provider, validating every event as the platform does.

### API

- **OpenAI-compatible** (`/v1`): `responses` (create, retrieve, cancel, delete, `input_items`), `files`, `containers`
  and their files, `models`.
- **Organization** (`/v1`): `signup`, `organization`, `keys`, `members`, `providers`, `catalog`, `harnesses`
  (install from built-in, git or upload; settings, secret ones encrypted; default provider and model), `usage`,
  `traces`.
- **Operator** (`/admin/v1`, operator key): invites, organizations and their limits, status and usage, node join
  tokens (shared or dedicated), nodes (list, drain, remove), prices.
- **Nodes** (`/internal/v1`, node credential): join, heartbeat, claim, events, finish, usage, signed blob transfers.
- **CLI**: `harnessgateway serve --role api,gateway,node`, `migrate`, `keygen`, `create-org`, `invite`, `node-token`.

### Deployment

- **One image, three roles**: `api` (the API, stateless, scales horizontally), `gateway` (the provider proxy) and
  `node` (Docker and gVisor; runs sandboxes). Postgres is the only database; any S3-compatible service holds objects.
- **Self-hosted**: `deploy/selfhosted/` runs the whole platform with Docker Compose (Postgres, RustFS, api and
  gateway, a node) on one Linux host.
- **SaaS and hybrid**: `deploy/saas/` covers the control plane, a node pool (`cloud-init.yaml`), hybrid nodes in a
  customer's network, and the invite-only pilot. `deploy/node/install-gvisor.sh` prepares any node host, including
  Docker address pools sized for one network per session.
- **Verification**: `deploy/verify-harnesses.py` confirms a deployment end to end as a pilot organization meets it
  (invite, sign-up, providers, the built-in harnesses, eight checks each).
- **Images**: the platform and sandbox images are built on Chainguard's Wolfi base, pinned by digest, with no package
  manager, BusyBox, `su`/`sudo` or setuid files; the sandbox provides bash, the common command-line tools, git,
  Python 3.12 and Node 24. `.github/workflows/images.yml` builds both, checks their hardening and fails on any
  fixable HIGH or CRITICAL Trivy finding, on every change and weekly, keeping the report and an SBOM.

### Not in 1.0.0

- A web console: the platform is the API and the CLI for now.
- Payment collection: usage and charges are metered and reported; invoicing is outside the platform.
- Automatic node scaling: nodes join and drain on their own, but adding and removing machines is the operator's.
- VM-level isolation (Kata/Firecracker) as an alternative to gVisor.
