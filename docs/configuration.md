# Configuration

Every role reads the same variables; each has a default.

| Variable | Default | Meaning |
|---|---|---|
| `HG_HOST`, `HG_PORT` | `127.0.0.1`, `8000` | where the API listens (the image sets `HG_HOST=0.0.0.0`) |
| `HG_PUBLIC_URL` | `http://127.0.0.1:8000` | how browsers, workers and signed blob URLs reach the API; the cookie and OAuth callbacks use it |
| `HG_DATA_DIR` | `~/.harnessgateway` (`/data` in the image) | the master key, the records, the blobs, the harness repositories, the worker's caches |
| `HG_MASTER_KEY`, `HG_MASTER_KEY_FILE` | `<data>/master.key` | the key everything sealed is sealed with |
| `HG_RECORDS` | `sqlite:///<data>/records.db` | or `postgresql://…` |
| `HG_BLOBS` | `dir:///<data>/blobs` | or `s3://bucket`, with `HG_S3_ENDPOINT`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` |
| `HG_SIGNUP` | `closed` | `open`: anyone may create an organization from the sign-up page |
| `HG_ORG`, `HG_OWNER` | `main`, `owner` | the organization and owner the first boot creates when sign-up is closed |
| `HG_DEPLOYER_KEY` | stored | overrides the stored deployer key |
| `HG_JOIN_TOKEN` | none | what a worker presents to join; required for workers on other machines |
| `HG_GATEWAY_HOST`, `HG_GATEWAY_PORT` | `0.0.0.0`, `8081` | where the gateway listens |
| `HG_GATEWAY_URL` | the platform container's own address | how a sandbox's door reaches the gateway; set it on workers that run elsewhere |
| `HG_CONTROL_URL` | `HG_PUBLIC_URL` | the API, for a worker on another machine |
| `HG_WORKER_NAME`, `HG_WORKER_SLOTS` | the hostname, `4` | the worker's name; turns at once on this node |
| `HG_BASE_IMAGE` | `ghcr.io/harnessgateway/harnessgateway-base` | the sandbox image; a node pulls it, or builds the shipped Dockerfile when it cannot |
| `HG_RUNTIME` | the daemon's default | a sandboxed Docker runtime, e.g. `runsc` (gVisor) |
| `HG_ALLOW_PRIVATE` | `0` | `1`: egress may name private addresses (a self-hosted model server on the LAN) |
| `HG_CA_FILE` | none | a CA bundle, where the network re-signs TLS |
| `HG_SMTP_URL`, `HG_MAIL_FROM` | none | `smtp(s)://user:pass@host:port` and the sender; set, sign-ups verify their email and passwords can be reset by mail |
| `HG_GITHUB_CLIENT_ID`, `HG_GITHUB_CLIENT_SECRET` | none | sign in with GitHub; the callback is `HG_PUBLIC_URL/auth/github/callback` |
| `HG_GOOGLE_CLIENT_ID`, `HG_GOOGLE_CLIENT_SECRET` | none | sign in with Google; the callback is `HG_PUBLIC_URL/auth/google/callback` |

The `hg` command line reads `HG_URL` and `HG_KEY`, or `~/.config/hg/config.json` written by
`hg login`; `HG_CONFIG_FILE` moves the file. `hg harness run` reads `HG_PROVIDER_URL`,
`HG_PROVIDER_KEY` and `HG_PROVIDER_API`.
