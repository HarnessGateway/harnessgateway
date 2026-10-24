# Security

What the platform promises, and the test that proves each promise. A change that breaks one fails
CI.

| # | Invariant | Proof |
|---|---|---|
| 1 | A provider's key is in the clear only inside the gateway process, for one upstream request at a time. Never on a command line, in a sandbox's environment, on a worker's disk, in a log, a trace, a capture or a response. | `test_docker.py::test_a_turn` (the sandbox's environment, token file and mounts hold the token, not the key), `test_api.py` (the capture never holds the key) |
| 2 | A sandbox reaches only its door; the door tunnels only to the package's egress hosts and forwards provider calls to the gateway. Private, loopback, link-local and multicast addresses are refused after resolution, and the connection is made to the address that was checked. | `test_door.py`, `test_docker.py::test_a_turn` (the cloud metadata address is refused) |
| 3 | A sandbox runs as a uid unique to its scope, the organization or the team whose harness runs, with every capability dropped, no new privileges, a read-only root, and the organization's cpu, memory and pid ceilings; under a sandboxed runtime where the node has one. Two teams of one organization are as apart as two organizations. | `test_docker.py::test_a_turn`, `test_sandboxes.py` |
| 4 | The platform never writes through a path a sandbox can create. Every write into a workspace, home, run or tools directory goes through one helper that refuses links at every component, and every read of a sandbox's output refuses anything but a plain file. | `test_files.py`, `test_harness.py` |
| 5 | A worker can see only the turns it was handed: its credential claims, reports and posts output; the blob URLs it receives name one object each for a short while. It holds no database, bucket or cloud credential. | `test_api.py`, `test_blobs.py` |
| 6 | Reach is organization, then team, then member: a member sees their own sessions and the sessions and harnesses of the teams they are in, an owner the organization's, nobody another organization's; every miss is a 404. | `test_api.py::test_harness_repository_and_teams`, `test_sessions.py` |
| 13 | A turn belongs to the request that made it: a client that closes a foreground request (its stream, or a blocking call it stops waiting on) stops the turn gracefully, with no endpoint called; a background turn outlives its request and stops only on `cancel`. | `test_disconnect.py`, `test_sessions.py` |
| 7 | Every number an organization could influence has a deployer ceiling on the organization record: cpus, memory, pids, timeouts, runs at once, disk, output, upload. | `test_store.py`, `test_api.py` |
| 8 | A package's output is validated against the Responses schema before a client sees it; a client never receives an item the SDK cannot parse. | `test_wire.py`, `test_api.py::test_response_sh_shapes` |
| 9 | The gateway relays only the API shape's own endpoints under the provider's path, and refuses a mixed or missing credential. | `test_api.py`, the gateway's `allowed` |
| 10 | No cloud SDK is imported anywhere but the two backend modules, and a module imports only its layer or below. | `test_layers.py` |
| 11 | An account's password is kept only as a salted scrypt hash; its session is a signed, HttpOnly, SameSite=Strict cookie that names one member of one organization and dies with the organization's suspension; sign-up is open to everyone only when the deployer says so, or to an invited email; an identity provider's email counts only when that provider says it is verified. | `test_api.py` (accounts) |
| 12 | A harness commit is made against the head the editor opened; two editors cannot silently overwrite each other, and a session runs the commit it started on. | `test_gitstore.py`, `test_store.py` |

## How a turn is contained

- **A fresh container per turn** from the platform's base image. Organizations choose neither
  the image nor the limits; only a package's egress list is theirs.
- **No capabilities, no privilege escalation, a read-only root** with a writable `/tmp`, and the
  unprivileged uid of the run's scope: the organization's, or the team's when the harness is a
  team's, derived from the name so it is the same on every node.
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

The deployer's routes return counts and sizes: turns, sandbox seconds, provider calls, bytes
relayed, by organization, harness and day. They never return an organization's prompts, output,
packages, keys or files. An organization's content is its own; a deployer who wants it has the
data directory, which is why the data directory is the thing to protect.

## Reporting

A vulnerability goes to the maintainers privately, through GitHub's security advisories on the
repository, not to the issue tracker.
