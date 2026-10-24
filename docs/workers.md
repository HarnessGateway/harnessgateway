# Workers and scale

A worker is a process with a Docker daemon that joins the control plane, claims turns and runs
them in sandboxes. One box runs one worker inside `serve --role all`; a fleet runs any number,
anywhere.

## Joining

A worker presents `HG_JOIN_TOKEN` to `HG_CONTROL_URL` and receives a credential of its own, which
it keeps under its data directory and uses from then on. It heartbeats every few seconds with its
slots and its running turns; one that stops heartbeating for 45 seconds is lost, its turns fail
with a message that says so, and its sessions go on with the next worker from their last snapshot.
`GET /v1/workers` lists them; `DELETE /v1/workers/{id}` forgets one.

## Claiming

`claim` hands a worker the oldest queued turn whose organization has a free slot (`max_runs`),
with URLs good for that turn's blobs (the package at its commit, the session's snapshot, the
member's home, the request's files) and the session token good for that turn's provider. The
worker materializes the turn, runs it, posts the output as it arrives, snapshots the workspace
and state, and reports the result. Cancels and steers come back on the heartbeat.

## Caches

Each worker caches the base image, each package commit, and each commit's installed tools layer
(`install.sh` runs once per commit per worker). A session's snapshot is fetched when the session
moves to a worker that does not have it.

## The gateway's address

A sandbox's door reaches the gateway at `HG_GATEWAY_URL`. On one box the platform finds its own
container's address and no setting is needed. A worker on another machine needs the gateway as
that machine's sandboxes reach it: a TLS address behind a load balancer in front of the `gateway`
role (the compose file's `ha` profile puts it on the `api` service), set on the worker. With
`HG_CA_FILE`, a private CA is trusted for it.

## Sizing

A turn is a container with the organization's cpu and memory ceilings; `HG_WORKER_SLOTS` is how
many run at once on a node. The sandboxes' disk is the daemon's; snapshots are bounded by
`disk_mb` per session. The control plane (`api,gateway`) is stateless over Postgres and S3 and
scales by replicas; the gateway relays provider traffic, so size it for that bandwidth.

## A sandboxed runtime

With gVisor installed on the node (`runsc`), `HG_RUNTIME=runsc` runs every sandbox under it,
adding a kernel boundary to the container's. `deploy/worker.cloud-init.yml` installs it.
