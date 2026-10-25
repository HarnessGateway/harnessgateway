# Security

## Threat model

Packages and the harnesses they run are untrusted code from many organizations, steered by untrusted input. The
platform must keep each organization's data, credentials and compute separate from every other's and from the
platform itself.

## Sandbox (node)

- **gVisor** (`runsc`) for every sandbox and every install; the node refuses another runtime unless the operator sets
  `HG_INSECURE_RUNTIME=1` (development).
- No Linux capabilities, `no-new-privileges`, a read-only root, a size-limited `/tmp`, CPU, memory and PID limits,
  uid 10001. One sandbox per session; sandboxes are never reused across sessions or organizations.
- Writable paths are the session's own bind mounts (workspace, home, state). Turn inputs are mounted read-only. The
  node never reads or writes a sandbox's writable files as root: seeding, restoring, checkpointing and staged file
  writes run inside the sandbox as its user.
- **Network**: each sandbox sits alone on an internal Docker network whose only peer is its egress door. The door
  tunnels (CONNECT) only to the package's allowed hosts, resolves names itself and connects to the address it checked,
  refusing private addresses. The sandbox has no DNS. Provider calls go through the door to the gateway.
- Packages are verified against their digest on the node; archives are refused if they hold links, devices or paths
  outside the root.

## Credentials

- **Provider keys** are encrypted the moment they arrive (AES-256-GCM, a data key per value, wrapped by the master key
  in KMS or derived from `HG_SECRET_KEY`, bound to the record). Only the gateway decrypts them, per request. A sandbox
  gets a per-turn token, revoked when the turn ends; the gateway relays only the provider API's own paths.
- **API keys, node credentials, join tokens and invite codes** are high-entropy secrets kept only as HMAC-SHA256 digests
  under a server pepper; shown once.
- **Package settings** marked secret are encrypted like provider keys and reach only the turn (`$HG_SETTINGS`, mode 0640).
- Nodes hold no database or storage credentials; blob transfers use signed, expiring, single-object URLs.

## Tenancy

- Every query names its organization, and Postgres row-level security enforces it again (`FORCE ROW LEVEL SECURITY`;
  the application role may not be a superuser or have BYPASSRLS). Platform-only tables are invisible to tenant
  transactions.
- Objects outside the caller's organization answer 404. Ids carry their kind and are checked by shape first.
- Admin actions (keys, members, providers, harness installs and settings, limits) are written to `audit_log`.

## API

- Authentication happens before a body is read; every route has a body limit (Content-Length and streamed count).
- Rate limit per organization (`requests_per_minute`), spending and storage caps, idempotency keys.
- Errors never echo secrets; logs never include tokens, keys or bodies.
- Provider URLs and git package sources must resolve to public addresses (unless a self-hosted operator allows private
  ones).

## Reporting

Report vulnerabilities privately to security@harnessgateway.ai.
