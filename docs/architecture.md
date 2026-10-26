# Architecture

HarnessGateway runs any coding-agent harness in a sandbox it owns, behind the OpenAI Responses API, for many
organizations at once. It has exactly three jobs:

1. **The tenant boundary**: organizations, members, API keys, providers, installed packages, ceilings.
2. **Sandboxed execution**: the client's Responses request goes, verbatim, to a harness package running in a gVisor
   sandbox; the package's Responses events come back.
3. **Metering**: every infrastructure cost (compute, network, storage) is measured per organization and billed at unit
   cost times the markup.

There is no HarnessGateway request or response schema: the Responses API is the contract in both directions, and the
package decides how its harness uses it (see `skills/harness-package-builder/reference/contract.md`).

## Processes

One image (`harnessgateway/platform`), three roles. SaaS, self-hosted and hybrid run the same roles in different places.

| Role | Does | Holds |
|---|---|---|
| `api` | HTTP (OpenAI routes and the platform's own), tenancy, admission, scheduling, event delivery, housekeeping | Postgres and object-storage credentials |
| `gateway` | Swaps a turn token for the organization's provider key and relays the call; captures exchanges when observability is on | the only process that decrypts provider keys |
| `node` | Runs sandboxes on one machine: create, run a turn, checkpoint, evict; meters compute and network | a node credential only |

```
client ──HTTPS──▶ api ◀──pull, HTTPS── node ──runs──▶ sandbox (gVisor) ──▶ egress door ──▶ allowed hosts
                   │                                                          │
              Postgres + S3                                                   └──turn token──▶ gateway ──key──▶ provider
```

- **SaaS:** we run all three (api and gateway replicated; nodes autoscaled).
- **Self-hosted:** the customer runs all three (one compose file, or the same Kubernetes/VM layout).
- **Hybrid:** we run api and gateway; the customer runs nodes in their own network with a join token dedicated to their
  organization (`dedicated_nodes`). Nodes only make outbound connections.

## Data

- **Postgres** is the one system of record and the queue (no broker): `turns` move `queued → in_progress → final` by
  compare-and-set; events are rows; `LISTEN/NOTIFY` wakes streams across api replicas. Every tenant table has
  row-level security keyed on the transaction's declared organization; the application role cannot bypass it.
- **Object storage** (any S3 API) holds requests, workspace snapshots, files, package archives, logs and captures. The
  `objects` ledger records each object's size for storage metering. Nodes never hold storage credentials: blob transfers
  use short-lived HMAC-signed URLs served by the api.

## A turn, end to end

1. **Request** (`api`): the bearer key is authenticated before the body is read; the body is checked against
   `ResponseCreateParams` and kept verbatim. Only `model` (harness/provider/model), `previous_response_id`, `stream`,
   `background` and `store` are read.
2. **Admission**: the harness is installed, the provider's shape matches the package, the organization is under its
   rate, storage and monthly ceilings. A session is created (or the previous turn's session continued), the turn is
   queued, and `response.created` is emitted.
3. **Scheduling**: a node with a free slot pulls the next turn: organizations are served by how few turns they have
   running, each session's turns in order, a session's warm sandbox kept on the node that holds it; the organization's
   concurrency and sandbox ceilings are checked in the same transaction. The job carries signed blob URLs, the
   package, the organization's settings for it, and a per-turn provider token.
4. **Sandbox** (`node`): the package is fetched and verified against its digest; the tools layer (`install.sh`) is
   installed once per tools key. A new session gets a fresh gVisor box (seeded from the package) or one restored from its
   snapshot; a continuing session reuses its warm box. Inputs are written read-only to `/run/hg`.
5. **Run**: `request.sh` reads the request and runs the harness. Provider calls go to the egress door, which relays them
   to the gateway; the gateway swaps the token for the real key. Other outbound traffic reaches only the package's
   allowed hosts.
6. **Delivery**: stdout is streamed to the api in numbered batches; each Responses event is validated against the SDK
   model for its type, numbered and fanned out to every follower. An invalid event stops the turn.
7. **End**: the package's terminal event (or the platform's reason: cancel, timeout, crash) becomes the final
   `Response`; the token is revoked; the workspace is snapshotted (unless `store: false`); compute, network and storage
   usage is recorded. The box stays warm until the organization's idle time passes.

## Code layout

```
harnessgateway/
  errors.py ids.py limits.py config.py     vocabulary, bounds, settings
  domain/      package.py wire.py turn.py pricing.py orglimits.py     pure rules
  crypto.py db.py blobs.py netguard.py     infrastructure adapters
  migrations/  0001_init.sql               schema and row-level security
  services/    one use case per class: tenancy, providers, packages, turns (+delivery, bus), scheduler, storage,
               metering, traces, housekeeping; platform.py is the composition root
  api/         thin routers: responses, storage (files, containers, models), org, admin, internal
  gateway/     the provider gateway
  node/        agent.py (loops and turns), sandbox.py (boxes), door.py (egress), docker.py, client.py, runtime/turn.sh
sandbox/       the sandbox image and hgkit
packages/      built-in harness packages
skills/        the Harness Package Builder skill
deploy/        selfhosted (compose), node (gVisor host setup), saas (reference layout)
```
