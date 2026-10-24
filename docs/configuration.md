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
| `HG_OIDC_ISSUER`, `HG_OIDC_CLIENT_ID`, `HG_OIDC_CLIENT_SECRET`, `HG_OIDC_NAME` | none, `sso` | single sign-on with any OpenID Connect issuer (Okta, Entra ID, Keycloak, Auth0, …); the callback is `HG_PUBLIC_URL/auth/<name>/callback` |
| `HG_SANDBOX` | `docker` | `process`: turns run as plain processes with no isolation (development only; `serve --dev` sets it) |
| `HG_KMS` | `local` | `aws`: the master key is a data key wrapped by the KMS key `HG_KMS_KEY_ID` (region and credentials from the usual AWS variables) |
| `HG_BILLING` | `none` | `stripe`: every meter reading is sent as a meter event for the organization's `billing_id`, with `HG_STRIPE_KEY` |
| `HG_TRACE_SHARING` | `0` | `1`: organizations that sign up on the free plan share their traces until an owner turns it off ([Data](data.md)) |

Every backend is named here, and `harnessgateway doctor` tries each one the environment names and
prints a line per backend: records, key management, blobs, events, mail, identity providers,
billing, the sandbox runtime. A managed deployment and a self-hosted one differ in these names and
in nothing else.

The `hg` command line reads `HG_URL` and `HG_KEY`, or `~/.config/hg/config.json` written by
`hg login`; `HG_CONFIG_FILE` moves the file. `hg harness run` reads `HG_PROVIDER_URL`,
`HG_PROVIDER_KEY` and `HG_PROVIDER_API`.
