# Operations

How to run HarnessGateway yourself: one host, then many. The design is in [`SPEC.md`](../SPEC.md); this page is the
how-to and the configuration reference.

## One host

Requirements: Linux, Docker with the Compose plugin, root. gVisor is installed by the installer if absent.

```bash
git clone https://github.com/harnessgateway/harnessgateway && cd harnessgateway
sudo ./deploy/install.sh
```

The installer:

1. checks Docker; pulls `ghcr.io/harnessgateway/harnessgateway` and `ghcr.io/harnessgateway/sandbox`, or builds them
   from the checkout;
2. installs gVisor (`runsc`) from gVisor's official repository (apt) or release binaries and registers it with
   Docker; adds `default-address-pools` to `/etc/docker/daemon.json` if absent (each box gets a network of its own, and
   Docker's default pools hold only about 30);
3. writes `/opt/harnessgateway/.env` with fresh secrets (never overwritten on a re-run);
4. asks for the first admin's email (`HG_ADMIN_EMAIL`), starts the compose project (`postgres`, `redis`, `api`,
   `worker`, `door`, `console`) and runs `harnessgateway init`, which creates the organization with the built-in
   harness package builder and the admin account, and prints **once** the invitation link that activates the account
   (the console's `/setup` page; mailed too when `HG_SMTP_URL` is set) and an admin API key for automation. See
   [`console.md`](console.md).

The API listens on `127.0.0.1:8000` and the console on `127.0.0.1:8800` (`HG_BIND`, `HG_HTTP_PORT`,
`HG_CONSOLE_PORT`). Put a TLS-terminating proxy in front of them before exposing them beyond the host.

Day to day, from `/opt/harnessgateway`:

| Task | Command |
|---|---|
| Status, logs | `docker compose ps`, `docker compose logs -f api worker door` |
| Stop, start | `docker compose stop`, `docker compose up -d --wait` |
| Upgrade | set `HG_PLATFORM_IMAGE` / `HG_SANDBOX_IMAGE` in `.env`, then `docker compose up -d --wait` (migrations run at the api's start; a new sandbox image re-runs every package's `install` once) |
| Postgres major | `HG_POSTGRES_IMAGE` in `.env` names it; new installations start on the current major, an existing one stays on its own. To move: `docker compose exec postgres pg_dumpall -U postgres > all.sql`, `docker compose down`, remove the `postgres` volume, set the new image, `docker compose up -d postgres --wait`, restore with `psql -U postgres < all.sql`, then `up -d --wait`. Redis (`HG_REDIS_IMAGE`) holds nothing durable and changes freely |
| Back up | `docker compose exec postgres pg_dump -U postgres harnessgateway`, plus the `objects` volume (or your bucket), plus `.env` |
| A box's containers | `docker ps --filter label=harnessgateway.box`; the worker removes exited ones every minute |

What a session saves is content-addressed (SPEC §9.5): a turn uploads only the blobs the store does not hold, a fork
copies nothing, and the object store grows with what actually changed. Deleting a session releases its blobs;
`retention_days` deletes idle sessions; `gc` removes blobs nothing references any more.

## Many hosts

Every process is stateless except Postgres, Redis and the object store, so scale is a matter of pointing more
processes at them:

1. **Postgres** 16 or later (18 for a new installation; managed is fine), **Redis** 8 (managed is fine; event
   streams, the job queue, nothing durable beyond a delay), and an **S3-compatible bucket**: `HG_DATABASE_URL`, `HG_REDIS_URL`, `HG_BLOB_URL=s3://…`. Connect
   as an ordinary role that owns the database (`deploy/postgres-init.sh` shows the grants): the api refuses a
   superuser or a `BYPASSRLS` role.
2. **api** replicas anywhere, behind a load balancer: `harnessgateway api`. Each needs the three URLs, `HG_SECRET_KEY`
   (the same everywhere), `HG_DOOR_KEY` and `HG_API_URL` (the api as boxes and doors reach it).
3. **worker** replicas: `harnessgateway worker`, as many as the queue needs. Self-hosted, each runs on a Docker +
   gVisor host with the socket mounted and `HG_BOX_DIR` at the same path inside and out; it launches boxes on its own
   host and needs the door on that host too (`HG_DOOR_CONTAINER`). On Kubernetes (`HG_LAUNCHER=kubernetes`) the worker
   starts each box as a pod and runs anywhere in the cluster: [`kubernetes.md`](kubernetes.md) and the Helm chart in
   `charts/harnessgateway` set it all up. In a container service (the SaaS), an extension's launcher starts boxes as
   tasks and the worker runs anywhere.
4. **door** replicas behind one address boxes reach as `HG_DOOR`: `harnessgateway door`. A door holds a policy cache
   and the CA derived from the secret key, nothing else; a box's next connection may land on any door.
5. **console** anywhere: `python3 console/server.py --api https://hg.internal`.

Redis is where the moments live; losing it costs a delay. The `reconcile` duty re-queues what Postgres says is
pending, and a follower of a stream reconnects with `starting_after`.

## Networks that proxy or re-sign TLS

Set `HG_UPSTREAM_PROXY` (the door reaches the internet through it with `CONNECT`) and `HG_CA_FILE` (a PEM the door
trusts when verifying servers; mount it into the door container). The api's own outbound calls (the identity provider's
discovery, JWKS and token endpoints) honour the standard `HTTPS_PROXY` / `SSL_CERT_FILE` variables; a provider's base
URL is only resolved, never fetched, when it is set.

## Configuration

Environment variables only, validated at start with every problem reported at once.

| Variable | Used by | Default | Meaning |
|---|---|---|---|
| `HG_DATABASE_URL` | api, worker | required | Postgres URL (an ordinary role owning the database) |
| `HG_REDIS_URL` | api, worker | `redis://localhost:6379/0` | Redis: turn streams, the job queue, wake signals, SSO state |
| `HG_BLOB_URL` | api, worker | required | `file:///path` or `s3://bucket[/prefix][?endpoint=…&region=…]` |
| `HG_SECRET_KEY` | api, worker, door | required | ≥32 random bytes, base64 (`harnessgateway keygen`). Wraps every org's data key, peppers token digests, derives the door's CA. Keep it |
| `HG_KMS_KEY` | api, worker | — | an AWS KMS key to wrap org data keys instead of `HG_SECRET_KEY` |
| `HG_OPERATOR_KEY` | api | — | `hgo_…`: the platform scope's key for automation and break-glass |
| `HG_DOOR_KEY` | api, door | required | `hgd_…`: doors authenticate to the api with it |
| `HG_API_URL` | api, worker, door | `http://localhost:8000` | the api as boxes and doors reach it (its host is always allowed through the door) |
| `HG_DOOR` | worker, door | `hg-door:3128` | `host:port` of the door as boxes reach it; the door listens on that port |
| `HG_HOST`, `HG_PORT` | api | `0.0.0.0`, `8000` | the api's listen address |
| `HG_SANDBOX_IMAGE` | api, worker | `ghcr.io/harnessgateway/sandbox:3.4.0` | the image every box runs (installs are keyed by it) |
| `HG_PLATFORM_IMAGE` | api | `ghcr.io/harnessgateway/harnessgateway:3.4.0` | reported under `GET /internal/versions` |
| `HG_SANDBOX_RUNTIME` | worker | `runsc` | gVisor for boxes (Docker's runtime, or the RuntimeClass on Kubernetes); `runc` runs them unisolated (logged) |
| `HG_LAUNCHER` | worker | `docker` | `docker` (boxes are containers on the worker's host) or `kubernetes` (boxes are pods; [kubernetes.md](kubernetes.md)) |
| `HG_K8S_NAMESPACE` | worker | required with `kubernetes` | the namespace box pods run in |
| `HG_K8S_DOOR_SERVICE` | worker | required with `kubernetes` | the door's Service, `namespace/name`: its cluster IP is each box's `hg-door` |
| `HG_K8S_RUNTIME_CLASS` | worker | `gvisor` | the RuntimeClass whose handler is `runsc` (unused with `HG_SANDBOX_RUNTIME=runc`) |
| `HG_K8S_PULL_POLICY` | worker | `IfNotPresent` | the sandbox image's pull policy in box pods |
| `HG_K8S_PROBE` | worker | `1` | prove at start-up that a box pod reaches the door and not the api; `0` skips it |
| `HG_DOOR_CONTAINER` | worker | `hg-door` | the door container the Docker launcher joins to each box's network |
| `HG_BOX_DIR` | worker | `/var/lib/harnessgateway/boxes` | each box's writable directories, mounted at the same path inside and out |
| `HG_UPSTREAM_PROXY`, `HG_CA_FILE` | door, api | — | see above |
| `HG_ALLOW_PRIVATE_EGRESS` | api, door | `0` | let providers, identity providers and box egress reach private addresses |
| `HG_CONSOLE_URL` | api | `http://localhost:8800` | where people open the console: invitation and SSO links are built on it |
| `HG_SMTP_URL`, `HG_MAIL_FROM` | api | — | `smtp://[user:pass@]host[:port]` (STARTTLS when offered) or `smtps://…`, and the sender: invitations are mailed; unset, they are shown once |
| `HG_EXTENSIONS` | all | — | `module:Class,…` ([extensions](extensions.md)) |
| `HG_BUILDER_DIR` | api | the image's `harness-package-builder` package | the built-in harness package builder `init` installs |
| `HG_TEMPLATE_DIR` | console | the image's `shell` package | the template every new harness starts from |
| `HG_CONSOLE_PAGES` | console | — | a directory of page modules of your own, loaded beside the console's ([console](console.md#pages-of-your-own)) |
| `HG_LOG_LEVEL` | all | `info` | `debug`, `info`, `warning`, `error`; logs are JSON lines on stderr |
| `HG_ADMIN_EMAIL` | `init`, `install.sh` | — | the first admin's login; `init --admin-email` names it too |
| `HG_CONSOLE_PORT` | compose `console` | `8800` | the console's port (it holds no key: people sign in with their account) |
| `HG_POSTGRES_IMAGE`, `HG_REDIS_IMAGE` | compose `postgres`, `redis` | `postgres:16-alpine` (install.sh writes `postgres:18-alpine` for a new installation), `redis:8-alpine` | the store images; see Postgres major above |
| `HG_DEFAULT_<FIELD>` | api, worker | see below | an organization's limits unless the platform scope sets its own |

Limit fields (`HG_DEFAULT_VCPUS=2`, …): `vcpus` (2.0), `memory_mb` (4096: the box's processes), `tmp_mb` (4096: its
`/tmp`, a tmpfs charged to the box on top of `memory_mb`), `pids` (1024), `max_concurrent_turns` (0: no ceiling),
`standby_s` (120: how long a box waits for the session's next turn), `turn_timeout_s` (0: none), `retention_days`
(0: sessions are kept until deleted), `observability` (`metadata`: a turn's trace keeps the last 16 KiB of its log lines
and stderr; `full`: the whole stdout and stderr of every turn, stored as objects under the turn).

## Commands

`harnessgateway api`, `worker`, `door`, `migrate`, `init --admin-email EMAIL [--name NAME]`, `keygen`.

## Releasing

The changelog is the release. `CHANGELOG.md`'s top entry names the version; `scripts/version-check` (run by CI) holds
`pyproject.toml`, `harnessgateway/__init__.py`, compose, the installer, `.env.example`, both Dockerfiles, the Helm chart and this page
to the same one.
When `main` carries a version that has no tag yet, the release workflow builds both images for amd64 and arm64, pushes
them to GHCR as `:<version>` and `:latest` with a build provenance attestation beside each (`gh attestation verify
oci://ghcr.io/harnessgateway/harnessgateway:<version> --owner harnessgateway` checks it), tags `v<version>` and
publishes the entry as the GitHub release. To release:
add the entry, bump the version everywhere the check names, merge to `main`. A manual run of the workflow with
`reissue` rebuilds a version that is already tagged and moves its tag to the current commit: for a release that must
be redone, never for a change (that is a new version).

## Troubleshooting

| Symptom | Look at |
|---|---|
| a turn stays `queued` | the harness's install is pending or failed (`GET /v1/harnesses/{name}/install`, the card in the console); the organization is at `max_concurrent_turns`; or no worker runs (`docker compose logs worker`, `GET /internal/health` → `redis.jobs`) |
| `hg_reason: install_failed` | the install's log tail on the harness (`GET /v1/harnesses/{name}/install`); the package's `install.egress` hosts must be reachable; `POST …/install` retries |
| `hg_reason: box_lost` | the box stopped answering for a minute: `docker compose logs worker`, `docker ps -a --filter label=harnessgateway.box`, the host's memory |
| `hg_reason: sandbox_failed` with `launch failed` | the worker could not start the container: Docker, the `runsc` runtime, the sandbox image, `HG_BOX_DIR` |
| `hg_reason: exited_without_result` right away | the package's `run` could not start: its shebang names an interpreter the sandbox image lacks (it has bash, python3 and node), or the file is not executable |
| a host is refused inside the box | the door answers `403` to `CONNECT` for hosts outside the org's and the harness's egress; the trace lists each connection and its verdict |
| `hg_reason: invalid_output` | the package printed an event the OpenAI schema rejects; the error names the field |
| the api refuses to start | `HG_DOOR_KEY is required`, `HG_SECRET_KEY is at least 32 random bytes`: `harnessgateway keygen` prints both |
| a lost invitation link, a locked or forgotten password | an admin: `POST /v1/accounts/{id}/reset` (console: Access → Accounts → Reset password) prints a new link; with no admin left, the operator invites a new one: `POST /internal/orgs/{id}/accounts` with the operator key (console: Platform → Organizations → Invite admin) |
| SSO sign-in fails | console: Access → Single sign-on (or `GET /v1/sso`): issuer, client id, domains; the identity provider's redirect URI must be `HG_CONSOLE_URL/sso/callback`; the provider's refusal comes back in the api's error message, which the console shows on the sign-in page |
| `403 … an admin or a developer may …` | the key or account lacks the role the route needs (SPEC §8.4) |
| an invitation never arrives | `HG_SMTP_URL` unset (the link is in the API's response and the console's dialog instead), or `docker compose logs api` shows `sending the invitation … failed` with the SMTP error |
