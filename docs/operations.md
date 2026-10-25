# Operations

## Commands

```
harnessgateway serve [--role api,gateway,node]   run roles (all three by default)
harnessgateway migrate                           apply migrations (the api also does this on start)
harnessgateway keygen                            fresh HG_SECRET_KEY, HG_OPERATOR_KEY, HG_NODE_JOIN_TOKEN
harnessgateway create-org NAME EMAIL             an organization and its owner's admin key
harnessgateway invite [--uses N] [--label L]     an invite code for sign-up
harnessgateway node-token [--org ORG_ID]         a node join token (shared, or dedicated to an organization)
```

## Settings (environment)

| Variable | Roles | Meaning |
|---|---|---|
| `HG_DATABASE_URL` | api, gateway | Postgres URL. The role must own the database and must not be a superuser or have BYPASSRLS (the platform refuses to start otherwise). |
| `HG_SECRET_KEY` | api, gateway | 64 hex digits. Derives the key-digest pepper, the blob-URL signing key and (without KMS) the master key for secrets. Keep it safe and constant. |
| `HG_KMS_KEY_ID` | api, gateway | AWS KMS key for envelope encryption of secrets (SaaS). |
| `HG_OPERATOR_KEY` | api | Bearer key for `/admin/v1`. Unset disables the operator API. |
| `HG_SIGNUP` | api | `invite` (default) or `closed`. |
| `HG_S3_BUCKET`, `HG_S3_ENDPOINT`, `HG_S3_REGION`, `HG_S3_ACCESS_KEY`, `HG_S3_SECRET_KEY` | api, gateway | Object storage. Leave the endpoint empty for AWS S3 and the keys empty to use the instance role. |
| `HG_HOST`, `HG_PORT` | api | Bind address (0.0.0.0:8000). |
| `HG_GATEWAY_HOST`, `HG_GATEWAY_PORT` | gateway | Bind address (0.0.0.0:8081). |
| `HG_NODE_JOIN_TOKEN` | api, node | On the api: registers this pre-shared token for the shared node pool. On a node: the token it joins with. |
| `HG_DEFAULT_PACKAGES` | api | Built-ins installed for new organizations (default: all). |
| `HG_PRICES`, `HG_PRICES_FILE`, `HG_MARKUP` | api | Unit costs per meter (JSON) and the markup (3). See billing.md. |
| `HG_API_URL` | node | The control plane. |
| `HG_GATEWAY_URL` | node | How sandboxes' egress doors reach the gateway (from the node's uplink network). |
| `HG_NODE_DIR` | node | Host directory for sessions, packages and tools (`/var/lib/harnessgateway`). When the node runs in a container, mount it at the same path. |
| `HG_NODE_SLOTS` | node | Concurrent turns on this node (4). |
| `HG_NODE_NAME` | node | Defaults to the hostname. |
| `HG_SANDBOX_IMAGE`, `HG_PLATFORM_IMAGE` | node | `harnessgateway/sandbox:1`, `harnessgateway/platform:1` (the door runs from the platform image). |
| `HG_RUNTIME` | node | `runsc` (required). Anything else needs `HG_INSECURE_RUNTIME=1` and is for development only. |
| `HG_UPLINK_NETWORK` | node | The Docker network egress doors join to reach the internet and the gateway (`bridge`; `harnessgateway` in compose). |
| `HG_UPSTREAM_PROXY` | node | Chain egress through an HTTP CONNECT proxy. |
| `HG_CA_FILE` | node | A CA bundle mounted into sandboxes and doors, for networks that re-sign TLS. |
| `HG_ALLOW_PRIVATE_EGRESS` | all | Self-hosted only: allow providers and egress to private addresses. |
| `HG_BUILTIN_DIR` | api | Override the built-in packages directory. |
| `HG_LOG_LEVEL` | all | `info`. Logs are JSON lines on stderr. |

## Images

```bash
docker build -t harnessgateway/platform:1 .
docker build -t harnessgateway/sandbox:1 sandbox/
# behind a TLS-intercepting proxy: add --secret id=ca,src=/path/ca.pem (the CA is never baked into the image)
```

## Running

- **One machine**: `deploy/selfhosted/` (compose).
- **SaaS / many machines**: `deploy/saas/` (api and gateway replicas; nodes on VMs with `deploy/node/`).
- **Hybrid**: a dedicated join token per customer; see `deploy/saas/README.md`.

## Observing

- `GET /readyz` on the api; `GET /admin/v1/nodes` for slots, running turns and warm sandboxes per node.
- Queue depth: `SELECT count(*) FROM turns WHERE status = 'queued'`.
- A stuck node is detected after 45 s without heartbeats: its running turns fail with `hg_reason: node_lost`, and its
  sessions restore from their snapshots elsewhere. A node that restarts fails the turns it no longer runs.
- Housekeeping (in the api, one runner at a time via advisory locks): lost nodes every 15 s, expired sessions every
  5 min, event purge every 10 min (events kept 24 h after a turn ends), storage accrual hourly.

## Backups and keys

Back up Postgres and the bucket. Without `HG_SECRET_KEY` (or the KMS key) stored provider keys and secret settings
cannot be decrypted. Rotating `HG_SECRET_KEY` invalidates every API key and stored secret: plan it as a re-issue.
