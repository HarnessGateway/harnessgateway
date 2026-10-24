# Nodes and scale

A node is a machine with a Docker daemon that joins the control plane, claims turns and runs them
in sandboxes; the `worker` role is its process. One box runs one node inside `serve --role all`; a
fleet runs any number, anywhere.

## Joining

A node presents `HG_JOIN_TOKEN` to `HG_CONTROL_URL` and receives a credential of its own, which it
keeps under its data directory and uses from then on. It heartbeats every few seconds with its
slots and its running turns; one that stops heartbeating for 45 seconds is lost, its turns fail
with a message that says so, and its sessions go on with the next node from their last snapshot.
`GET /v1/workers` lists them; `DELETE /v1/workers/{id}` forgets one. The deployer's console shows
them under Nodes.

## Claiming and affinity

`claim` hands a node the oldest queued turn whose organization has a free slot (`max_runs`), with
URLs good for that turn's blobs (the package at its commit, the session's snapshot, the member's
home, the request's files) and, when the package names a provider, the session token good for that
provider. A turn of a session whose trees are on another live node waits three seconds for that
node first, so a conversation stays where its workspace already is; past that, any node takes it.
The node materializes the turn, runs it, posts the output as it arrives, snapshots the workspace
and state, and reports the result. Cancels and steers come back on the heartbeat.

## Caches

Each node caches the base image, each package commit, and each commit's installed tools layer
(`install.sh` runs once per commit per node). A session's snapshot is fetched when the session
moves to a node that does not have it.

## The gateway's address

A sandbox's door reaches the gateway at `HG_GATEWAY_URL`. On one box the platform finds its own
container's address and no setting is needed. A node on another machine needs the gateway as that
machine's sandboxes reach it: a TLS address behind a load balancer in front of the `gateway` role
(the compose file puts it on the `api` service), set on the node. With `HG_CA_FILE`, a private CA
is trusted for it. A package without `provider_env` gets no gateway at all.

## Sizing

A turn is a container with the organization's cpu and memory ceilings; `HG_WORKER_SLOTS` is how
many run at once on a node. The sandboxes' disk is the daemon's; snapshots are bounded by `disk_mb`
per session. The control plane (`api,gateway`) is stateless over Postgres and S3 and scales by
replicas; followers of a running turn are woken by Postgres `NOTIFY` across replicas, with no
polling. The gateway relays provider traffic, so size it for that bandwidth. Housekeeping (lost
turns, the daily storage sample) runs in every `api` and is safe to repeat.

## A sandboxed runtime

With gVisor installed on the node (`runsc`), `HG_RUNTIME=runsc` runs every sandbox under it,
adding a kernel boundary to the container's. `deploy/worker.cloud-init.yml` installs it.
