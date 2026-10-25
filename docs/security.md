# Security

What the platform promises, and the test that proves each promise. A change that breaks one fails
CI.

| # | Invariant | Proof |
|---|---|---|
| 1 | A provider's key is in the clear only inside the gateway process, for one upstream request at a time. Never on a command line, in a sandbox's environment, on a worker's disk, in a log, a trace, a capture or a response. | `test_docker.py::test_a_turn` (the sandbox's environment, token file and mounts hold the token, not the key), `test_api.py` (the capture never holds the key) |
| 2 | A sandbox reaches only its door; the door tunnels only to the package's egress hosts and forwards provider calls to the gateway. Private, loopback, link-local and multicast addresses are refused after resolution, and the connection is made to the address that was checked. | `test_door.py`, `test_docker.py::test_a_turn` (the cloud metadata address is refused) |
| 3 | A sandbox runs as a uid unique to its organization, with every capability dropped, no new privileges, a read-only root, and the organization's cpu, memory and pid ceilings; under a sandboxed runtime where the node has one. | `test_docker.py::test_a_turn`, `test_sandboxes.py` |
| 4 | The platform never writes through a path a sandbox can create. Every write into a workspace, home, run or tools directory goes through one helper that refuses links at every component, and every read of a sandbox's output refuses anything but a plain file. | `test_files.py`, `test_harness.py` |
| 5 | A node can see only the turns it was handed: its credential claims, reports and posts output; the blob URLs it receives name one object each for a short while. It holds no database, bucket or cloud credential. | `test_api.py`, `conformance/test_blobs.py` |
| 6 | Reach is organization, then member: a member sees their own sessions, an owner the organization's, nobody another organization's; every miss is a 404. Owners change providers and harnesses; members run them. | `test_api.py::test_harness_repository_members_and_the_deployer`, `test_sessions.py` |
| 7 | Every number an organization could influence has a deployer ceiling on the organization record: cpus, memory, pids, timeouts, runs at once, disk, output, upload. | `test_store.py`, `test_api.py` |
| 8 | A package's output is validated against the Responses schema before a client sees it; a client never receives an item the SDK cannot parse. | `test_wire.py`, `test_api.py::test_response_sh_shapes` |
| 9 | The gateway relays only the API shape's own endpoints under the provider's path, and refuses a mixed or missing credential. | `test_gateway.py`, the gateway's `allowed` |
| 10 | No vendor SDK is imported anywhere but the backend module that wraps it (S3, KMS, Postgres) and the wire module (the OpenAI schema), and a module imports only its layer or below. | `test_layers.py` |
| 11 | An account's password is kept only as a salted scrypt hash; its session is a signed, HttpOnly, SameSite=Strict cookie that names one member of one organization and dies with the organization's suspension; sign-up is open to everyone only when the deployer says so, or to an invited email. | `test_accounts.py`, `test_api.py` (accounts) |
| 12 | A harness commit is made against the head the editor opened; two editors cannot silently overwrite each other, and a session runs the commit it started on. | `test_gitstore.py`, `test_store.py` |
| 13 | A turn belongs to the request that made it: a client that closes a foreground request (its stream, or a blocking call it stops waiting on) stops the turn gracefully, with no endpoint called; a background turn outlives its request. | `test_disconnect.py`, `test_sessions.py` |
| 14 | A session runs one turn at a time: a new request on a session that is running a turn stops that turn gracefully and runs when it has ended, on the workspace it left; the stopped response says it was superseded. | `test_sessions.py::test_a_new_request_supersedes_the_running_turn`, `test_api.py::test_background_cancel_and_supersede` |
| 15 | A package that names no `provider_env` runs with no token and no gateway: nothing in the sandbox can reach a provider, and the turn's only reach is the package's egress list. | `test_catalog.py::test_the_shell_package_runs_any_command_without_a_provider` |
| 16 | A request past the organization's rate ceiling is refused before anything is queued; a request refused for its own faults does not count; an `Idempotency-Key` returns its sender's first turn and never another member's. | `test_intake.py`, `test_api.py::test_refusals` |
| 17 | A data directory written by an earlier major boots unchanged: the same master key opens what it sealed, every record reads as it was, and a contract-7 package still runs. | `test_upgrade.py` |

## How a turn is contained

- **A fresh container per turn** from the platform's base image. Organizations choose neither
  the image nor the limits; only a package's egress list is theirs.
- **No capabilities, no privilege escalation, a read-only root** with a writable `/tmp`, and the
  unprivileged uid of the organization, derived from its name so it is the same on every node.
  The writable mounts are handed to that uid before the turn and nothing else on the host belongs
  to it.
- **One route out.** The sandbox has no DNS and no default route. Its only neighbour is a door on
  a private network. The door tunnels to the package's egress hosts, resolves names itself,
  refuses private answers, and connects to the address it checked. Provider calls go through the
  door's forwarder to the gateway.
- **A token, not a key.** The sandbox gets a per-turn session token in a file readable only by
  its uid. The gateway swaps it for the real key, which it decrypts in memory for that one call.
  The token expires two minutes after the turn's timeout.
- **The package's scripts run unprivileged**, including `install.sh`, with its own egress list.
- **Secrets never cross a command line.** Environment variables reach the container by name; the
  Docker client inherits only an allowlist of host variables.
- **Everything is torn down**: the container, the door, the network, and the token file, which is
  overwritten before it is deleted.

## What the deployer can and cannot see

The deployer's routes return names and counts: organizations, members, harness and provider names,
sessions, responses, what runs and what queues, the fleet. They never return an organization's
prompts, output, packages, keys or files. An organization's content is its own; a deployer who
wants it has the data directory, which is why the data directory is the thing to protect.
`tests/test_invariants.py` checks that every row of the table above names a test that exists.

## Reporting

A vulnerability goes to the maintainers privately, through GitHub's security advisories on the
repository, not to the issue tracker.
