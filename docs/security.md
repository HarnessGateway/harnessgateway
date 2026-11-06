# Security

What protects whom, in one page. The rules are [`SPEC.md`](../SPEC.md) §8; this is the map.

## Isolation

A turn runs in a **box**: a container from the sandbox image under gVisor (`runsc`; `HG_SANDBOX_RUNTIME=runc` is
allowed and logged as unisolated), with a read-only root, a size-limited tmpfs `/tmp`, no capabilities,
`no-new-privileges`, uid 10001, cpu, memory and pid limits, no DNS, and bind mounts only for its own directories. Its
network has one other member: the door. A box lives for one session while it has work and standby, then it is gone;
after every turn the runner kills every process it did not start. In the SaaS the container service's micro-VM is the
isolation and the security group allows only the door.

## Egress

Every byte out of a box goes through the **door**, a CONNECT proxy that authenticates the box by its token and asks the
api what that box may reach now: the organization's egress entries, the package's run entries, the turn's provider
host, and the api itself. Anything else is refused with 403. The door resolves names itself and connects only to
public addresses unless `HG_ALLOW_PRIVATE_EGRESS` is set. Between turns a box reaches the api and nothing else. Every
connection is recorded (host, port, verdict, bytes) into the turn's trace and the byte meters.

## Secrets

No secret enters a box. Provider keys, harness secrets and a caller's MCP credentials are sealed at rest with the
organization's data key (wrapped by `HG_SECRET_KEY` or AWS KMS) and never returned by the API. A turn sees
**placeholders** (`hgs_…`) in its environment, its MCP file and its request; the door replaces a placeholder with its
value in request headers, on the hosts the secret is bound to, by intercepting TLS with a certificate from a CA every
box trusts (derived from the secret key, so every door and every api agree on it). A placeholder sent anywhere else
travels as it is. Bodies, URLs and responses are never touched.

Credentials the platform itself issues: API keys (`hg_`, hashed with a pepper), sign-ins (`hgl_`, expiring,
revocable), invitations (`hgi_`, one-time), box tokens (`hgb_`, one per box, leased), the door key (`hgd_`), the
operator key (`hgo_`). Each is shown once and stored as a digest. A box's token lets it read its own session's
objects, its organization's install archives and uploaded files, and the shared package archives; nothing else.

## Access

Every request runs as a `Principal`: an organization, an actor, a role (`admin`, `developer`), whether it is a
run-only key, whether it holds the platform scope. One table (`domain/access.py`) says who may do what; a refusal names
the role that may. Row-level security in Postgres keeps every organization's rows apart, and the api refuses to run as
a database role that could bypass it. People sign in with a password (scrypt, lockout after ten failures) or through
the organization's OIDC provider (PKCE, nonce, JWKS verification, domain allow list); `required` turns passwords off
for everyone but platform admins. Every change of standing is an audit row.

## Limits

Bodies are bounded per route (`limits.py`): a Responses request, an upload, a package archive, a box's reports. What a
box uploads of its own state (blobs, archives, logs) is not bounded by the platform. An organization's limits bound
each box (`vcpus`, `memory_mb`, `tmp_mb`, `pids`) and how many turns run at once; a turn is bounded by the package's
timeout and the organization's.

## Reporting

Open a private security advisory on GitHub rather than a public issue.
