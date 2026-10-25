# Deploy

One image, three roles, two shapes. One box is one command; the fleet shape uses the same image
and the same variables.

## What you need

Docker. A Linux server, or Docker Desktop on a Mac; the image is built for amd64 and arm64.
Every turn runs in a container the platform starts through the Docker socket, so the socket is
mounted. Nothing else: no cloud account, no directory to prepare, no second port.

## One box

```bash
curl -fsSL https://raw.githubusercontent.com/HarnessGateway/harnessgateway/main/deploy/install.sh | bash
```

The script installs Docker if the machine has none, starts the platform as one container and
prints the keys the first boot made. By hand, it is:

```bash
docker run -d --name harnessgateway --restart unless-stopped -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock -v hg:/data \
  -e HG_PUBLIC_URL=http://hg.example.com:8000 ghcr.io/harnessgateway/harnessgateway:10
```

That runs `serve --role all`: the API on 8000, the gateway, and a worker that pulls the sandbox
base image and then takes turns. The first boot makes the deployer key and, with sign-up closed,
an organization and its owner's key, and prints them once: `docker logs harnessgateway`. This
documentation is at `/docs/` and the OpenAPI reference at `/reference`.

Everything the platform keeps is on the `hg` volume at `/data`: the master key, the records (SQLite),
the blobs, the harness repositories, the worker's caches. The sandboxes are started by the host's
daemon, so what they mount has to be a path the daemon knows. The platform does not ask you for
one: at start it inspects its own container, learns where the daemon keeps the volume, and
translates every mount it makes. The scripts it runs inside a sandbox and each turn's token are
copied under `/data/worker` first, so nothing of the platform container's own filesystem is ever
shared. The same applies to the gateway: a sandbox's door reaches it at the platform container's
own address on its own network. No port is published for it. This is why the same command works
on a Mac, where the volume lives inside Docker Desktop's VM.

## With compose: Postgres, S3 and TLS

```bash
git clone https://github.com/HarnessGateway/harnessgateway && cd harnessgateway/deploy
HG_DOMAIN=hg.example.com HG_JOIN_TOKEN=$(openssl rand -hex 24) docker compose up -d
docker compose run --rm api doctor        # every backend, one line each
```

The default compose shape is the one that grows: two `api,gateway` replicas behind Caddy
(terminating TLS for `HG_DOMAIN`, a certificate from Let's Encrypt or self-signed for `localhost`),
Postgres for the records, MinIO for the blobs, and a node that joins with the token. Point
`HG_RECORDS` at any Postgres and `HG_BLOBS` at any S3 bucket (with `HG_S3_ENDPOINT` and AWS
credentials in the environment) to use managed services instead; add nodes on other machines as
below. `--profile one-box` is the small shape instead: every role in one container over SQLite and
a blob directory on the `hg-data` volume, still behind Caddy. Set `HG_SIGNUP=open` to let anyone
create an organization from the sign-up page; leave it closed to create organizations yourself or
by invitation.

## The roles

| Role | Does | Holds | Scales |
|---|---|---|---|
| `api` | the Responses API, the organization and deployer routes, the documentation, the nodes' routes | the master key | any number behind a load balancer |
| `gateway` | relays sandboxes' provider calls, swaps the session token for the key, meters, captures | the master key | any number behind a load balancer |
| `worker` | a node: joins with a token, pulls turns, runs them in sandboxes on its Docker host, pushes snapshots | a node credential and a cache | one per machine, any number of machines |
| `all` | the three in one process | | one box |

`harnessgateway serve --role api,gateway` and `--role worker` run roles apart.

## More nodes

Nodes on other machines run the image with `--role worker`, `HG_CONTROL_URL` set to the API,
`HG_JOIN_TOKEN`, and `HG_GATEWAY_URL` set to the gateway as that machine's sandboxes reach it;
`deploy/worker.cloud-init.yml` is a node on any cloud, with gVisor as the sandbox runtime
(`HG_RUNTIME=runsc`). See [Nodes and scale](workers.md).

## The managed shape

The managed platform is this compose file with other names in the environment: `HG_KMS=aws` with
`HG_KMS_KEY_ID` so the master key is wrapped by a KMS, a managed Postgres and an S3 bucket, and
`HG_SIGNUP=open`. Nothing in the code tells the two apart; [Configuration](configuration.md) lists
every name.

## Without Docker, for development

```bash
uv run harnessgateway serve --dev
```

`--dev` runs every turn as a plain process on this machine with no sandbox at all, so you can
develop a package on a laptop without Docker. It prints a warning: only for packages you trust.
The `hg harness run DIR` command goes further and runs one package's scripts directly, without the
platform; see [Write a package](packages.md).

## Upgrading

Pull the new image and restart the container; a data directory from the previous major boots as
it is (`tests/test_upgrade.py` proves it on 7.1's). `harnessgateway doctor` afterwards says every
backend is reachable. A release is
tagged `vX.Y.Z` and pushed as `:X.Y.Z`, `:X` and `:latest`; pin the major (`:10`) to take fixes
without a contract change. The package contract is versioned in `harness.json` (`"contract": 8`;
a contract-7 package still runs) and a new major of the platform is the only thing that changes it.
