# Operations

How to run HarnessGateway yourself: one host, then many. The design is in [`SPEC.md`](../SPEC.md) §10; this page is
the how-to and the configuration reference.

## One host

Requirements: Linux, Docker with the Compose plugin, root. gVisor is installed by the installer if absent.

```bash
git clone https://github.com/harnessgateway/harnessgateway && cd harnessgateway
sudo ./deploy/install.sh
```

The installer:

1. checks Docker; installs gVisor (`runsc`) from gVisor's official repository (apt) or release binaries and registers
   it with Docker; adds `default-address-pools` to `/etc/docker/daemon.json` if absent (each sandbox gets a network of
   its own, and Docker's default pools hold only about 30);
2. pulls `ghcr.io/harnessgateway/harnessgateway` and `ghcr.io/harnessgateway/sandbox`, or builds them from the checkout;
3. writes `/opt/harnessgateway/.env` with fresh secrets (never overwritten on a re-run);
4. starts the compose project (`postgres`, `harnessgateway`, `node`) and runs `harnessgateway init`, which creates the
   organization, installs the built-in harnesses and prints the **admin key once**;
5. starts the `console` service with that key and prints the two console sign-ins once (`admin`, `deployer`; each
   changed at first sign-in). See [`console.md`](console.md).

The API listens on `127.0.0.1:8000` and the consoles on `127.0.0.1:8800` (`HG_BIND`, `HG_HTTP_PORT`,
`HG_CONSOLE_PORT`). Put a TLS-terminating proxy in front of them before exposing them beyond the host.

Day to day, from `/opt/harnessgateway`:

| Task | Command |
|---|---|
| Status, logs | `docker compose ps`, `docker compose logs -f harnessgateway node` |
| Stop, start | `docker compose stop`, `docker compose up -d --wait` |
| Upgrade | set `HG_PLATFORM_IMAGE` / `HG_SANDBOX_IMAGE` in `.env`, then `docker compose up -d --wait` (migrations run at start) |
| Back up | `docker compose exec postgres pg_dump -U postgres harnessgateway`, plus the `objects` volume (or your bucket), plus `.env` |

The node finishes saves under way and removes its sandboxes when it stops (compose gives it two minutes). A session
whose sandbox is gone restores from its last save on its next turn, on any node.

## Many hosts

Every process is stateless except Postgres and the object store, so scale is a matter of pointing more processes at
them:

1. **Postgres** 16+ (managed is fine) and an **S3-compatible bucket**: `HG_DATABASE_URL`, `HG_BLOB_URL=s3://…`.
   Connect as an ordinary role that owns the database (`deploy/postgres-init.sh` shows the grants): the API refuses
   a superuser or a `BYPASSRLS` role.
2. **API and gateway** replicas anywhere, behind a load balancer: `harnessgateway serve --role api,gateway` (or
   separate `--role api` and `--role gateway` fleets). Each needs `HG_DATABASE_URL`, `HG_BLOB_URL`, `HG_SECRET_KEY`
   (the same on all), and `HG_API_URL` (the URL nodes use to reach the API).
3. **Nodes**, one per Docker + gVisor host:

   ```bash
   docker run -d --name harnessgateway-node --restart unless-stopped --user 0:0 \
     -e HG_API_URL=https://hg.internal -e HG_GATEWAY_URL=https://hg.internal/gateway \
     -e HG_NODE_JOIN_TOKEN=hgj_… -e HG_UPLINK_NETWORK=bridge -e HG_NODE_DIR=/var/lib/harnessgateway \
     -v /var/run/docker.sock:/var/run/docker.sock -v /var/lib/harnessgateway:/var/lib/harnessgateway \
     ghcr.io/harnessgateway/harnessgateway:1.0.0 node
   ```

   The node directory is mounted at the same path inside and out: sandboxes are sibling containers that bind-mount
   their session directories from the host. A node joins with a join token, receives its own credential (kept in
   `$HG_NODE_DIR/node.json`) and starts claiming turns. `HG_GATEWAY_URL` must be reachable from containers on
   `HG_UPLINK_NETWORK` (each sandbox's egress door joins it).

Join tokens: `HG_NODE_JOIN_TOKEN` on the API is the default pool's shared token. For other pools or single-use tokens:
`harnessgateway node-token create --pool gpu --uses 5` (operator: runs against the database). Draining a node:
`POST /internal/nodes/{id}/drain` with the operator key (`HG_OPERATOR_KEY`); it finishes its turns and takes no new
ones.

Postgres is the queue and the bus (`SKIP LOCKED` claims, `LISTEN/NOTIFY` for streams); a sweeper on any API replica,
under an advisory lock, fails the turns of a node silent for 45 s and releases its sessions.

## Networks that proxy or re-sign TLS

Set `HG_UPSTREAM_PROXY` (doors reach the internet through it with `CONNECT`) and `HG_CA_FILE` (a PEM the doors trust
when verifying servers, and sandboxes trust alongside the node's own CA). With compose, put the CA file under
`HG_NODE_DIR`: the node sees that directory at the same path as the host. The API's own outbound calls (providers,
`git` installs) honour the standard `HTTPS_PROXY` / `SSL_CERT_FILE` variables.

## Configuration

Environment variables only, validated at start with every problem reported at once.

| Variable | Used by | Default | Meaning |
|---|---|---|---|
| `HG_DATABASE_URL` | api, gateway | required | Postgres URL (an ordinary role owning the database) |
| `HG_BLOB_URL` | api, gateway | required | `file:///path` (one host; the API serves signed links) or `s3://bucket[/prefix][?endpoint=…&region=…]` |
| `HG_SECRET_KEY` | api, gateway | required | ≥32 random bytes, base64 (`harnessgateway keygen`). Wraps every org's data key and peppers key digests. Keep it |
| `HG_KMS_KEY` | api, gateway | — | an AWS KMS key to wrap org data keys instead of `HG_SECRET_KEY` |
| `HG_OPERATOR_KEY` | api | — | the operator's key for `/internal/node-tokens` and draining |
| `HG_NODE_JOIN_TOKEN` | api, node | — | the default pool's join token (`hgj_…`); a node needs it for its first start |
| `HG_API_URL` | api, node | `http://localhost:8000` | where nodes reach the API; signed object links are built on it |
| `HG_GATEWAY_URL` | node | `http://localhost:8000/gateway` | where doors reach the provider gateway |
| `HG_HOST`, `HG_PORT` | api, gateway | `0.0.0.0`, `8000` | listen address |
| `HG_NODE_NAME`, `HG_NODE_POOL` | node | hostname, `""` | the node's name and pool |
| `HG_NODE_SLOTS` | node | `4` | turns run at once |
| `HG_NODE_WARM_MAX` | node | `32` | idle sandboxes kept warm |
| `HG_NODE_DIR` | node | `/var/lib/harnessgateway` | sessions, tools, the node's CA and credential |
| `HG_SANDBOX_IMAGE` | node | `ghcr.io/harnessgateway/sandbox:1.0.0` | the image every sandbox runs |
| `HG_PLATFORM_IMAGE` | node | `ghcr.io/harnessgateway/harnessgateway:1.0.0` | the image doors run |
| `HG_UPLINK_NETWORK` | node | `hg-uplink` | the Docker network doors join to reach the gateway and the internet |
| `HG_UPSTREAM_PROXY`, `HG_CA_FILE` | node | — | see above |
| `HG_ALLOW_PRIVATE_EGRESS` | all | `0` | let providers and sandbox egress reach private addresses |
| `HG_UNSAFE_RUNTIME` | node | — | a Docker runtime other than `runsc` (development only: no gVisor isolation) |
| `HG_EXTENSIONS` | api, gateway | — | `module:Class,…` ([extensions](extensions.md)) |
| `HG_PACKAGES_DIR` | api | the image's built-ins | where built-in packages are read from |
| `HG_LOG_LEVEL` | all | `info` | `debug`, `info`, `warning`, `error`; logs are JSON lines on stderr |
| `HG_CONSOLE_KEY`, `HG_CONSOLE_PORT` | compose `console` | —, `8800` | the admin key the consoles use (written by `install.sh`); their port |
| `HG_DEFAULT_<FIELD>` | api | see below | an organization's capacity unless an extension sets its own |

Capacity fields (`HG_DEFAULT_VCPUS=2`, …): `vcpus` (2.0), `memory_mb` (4096), `pids` (1024), `tmp_mb` (4096),
`max_sandboxes` (0: none), `max_running_turns` (0: none), `idle_ttl_s` (300: a warm sandbox's idle life),
`turn_timeout_s` (0: none), `retention_days` (0: sessions are kept until deleted), `node_pool` (""),
`observability` (`metadata` or `full`: whether traces keep bodies).

## Commands

`harnessgateway serve --role api,gateway | api | gateway`, `node`, `migrate`, `init [--name NAME]`,
`node-token create [--pool P] [--uses N] [--ttl-days D]`, `keygen`, and `door` (started by the node, never by hand).

## Releasing

The changelog is the release. `CHANGELOG.md`'s top entry names the version; `scripts/version-check` (run by CI) holds
`pyproject.toml`, `harnessgateway/__init__.py`, compose, the installer and this page to the same one. When `main`
carries a version that has no tag yet, the release workflow builds both images for amd64 and arm64, pushes them to
GHCR as `:<version>` and `:latest`, tags `v<version>` and publishes the entry as the GitHub release. To release: add
the entry, bump the version everywhere the check names, merge to `main`.
A manual run of the workflow with `reissue` rebuilds a version that is already tagged and moves its tag to the
current commit: for a release that must be redone, never for a change (that is a new version).

## Troubleshooting

| Symptom | Look at |
|---|---|
| a turn stays `queued` | `docker compose logs node`: no node joined (join token), the node is draining, or the org is at `max_running_turns` |
| `hg_reason: install_failed` | the turn's trace (`GET /v1/traces/{id}`): the install log tail; the package's `install.egress` hosts must be reachable |
| a host is refused inside the sandbox | the door answers `403` to `CONNECT` for hosts outside the org's and the harness's egress; the trace lists each connection and its verdict |
| `hg_reason: invalid_output` | the package printed an event the OpenAI schema rejects; the error names the field |
| the node exits at start | `HG_CA_FILE names no file` (mount it), `the API refused the join token` (the API needs the same `HG_NODE_JOIN_TOKEN`) |
