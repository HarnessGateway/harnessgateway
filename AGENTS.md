# AGENTS.md

How to work in this repository. It is written for coding agents and applies to people the same way.

## What this is

HarnessGateway runs coding-agent CLIs as an API. A caller sends an OpenAI Responses request; the platform runs it
through a **harness package** (a CLI with its configuration) in a gVisor sandbox, a **box** that lives for one
session, and streams Responses events back, keeping the session so the next request continues it. The request serves
the task; the package defines the agent. Self-hosted is one organization; the extension seam is where more is built,
outside this repository.

A **harness package project** is a folder: `harness.json` (the manifest), `install` (the CLI, pinned, into `$HG_TOOLS`),
`run` (one turn: the request in, Responses events out), optional `seeds/home` and `seeds/workspace`, a README and
`tests/` on recorded output. `docs/packages.md` is the guide to building one; `packages/shell` is the template every
package starts from; `packages/harness-package-builder` is the built-in agent that writes them.

**`SPEC.md` is the specification.** Read it before changing behaviour, and change it first when the design changes.
The code is checked against it; a change the spec does not describe is a spec change.

## Layout

| Path | What it holds |
|---|---|
| `harnessgateway/domain/` | pure rules, no I/O: names, egress entries, manifests, packages (digest, archive), MCP resolution, access, limits, meters, turn states |
| `harnessgateway/contract/` | the Responses reader and writer (re-exporting hgkit's validator) and request routing |
| `harnessgateway/ports/`, `adapters/` | interfaces (BlobStore, KeyWrapper, Mailer, Launcher) and their implementations (file and S3 stores, local and KMS key wrapping, SMTP, Docker with gVisor). Vendor SDKs live only in `adapters/` |
| `harnessgateway/store/` | `db.py` (Postgres, scoped transactions, migrations), `moments.py` (Redis: streams, queue, wake, SSO state), `objects.py` (the ledger over the blob store) |
| `harnessgateway/services/` | one class per responsibility: orgs, accounts, sso, keys, audit, vault, providers, harnesses, installs, files, containers, turns (the caller's side), boxes (the box's and the door's side), snapshots, traces, usage, operations. `platform.py` is the composition root |
| `harnessgateway/api/` | FastAPI routers: OpenAI-shaped (`responses.py`, `storage.py`), management (`manage.py`), identity and SSO (`identity.py`), the boxes', doors' and operator's routes (`internal.py`); `http.py` is authentication, body limits and the error envelope |
| `harnessgateway/worker/` | jobs from the queue (launch, install, stop, delete_session) and duties on a clock |
| `harnessgateway/door/` | the egress proxy: CONNECT with box tokens, policies from the api, interception of bound hosts |
| `harnessgateway/migrations/` | SQL migrations, applied in order under an advisory lock |
| `sandbox/` | the sandbox image, `runner.py` (what runs in a box; standard library only), `hgkit` (the Responses contract in Python for packages, whose validator the platform reads output with) and `hgsnapshot` (manifests, walk, restore; shared by the runner and the api) |
| `packages/` | the two shipped packages: `shell`, the template the console copies for every new harness, and `harness-package-builder`, the built-in `init` installs |
| `console/` | the organization and platform consoles (`server.py`, `static/`) and the chat (`chat/`): applications on the public API, vanilla JS, no build step |
| `deploy/` | `compose.yaml`, `install.sh`, the Postgres init script, `.env.example` |
| `docs/` | operations, api, security, console, packages, extensions |
| `tests/unit`, `tests/runner`, `tests/services`, `tests/e2e`, `tests/integration` | unit tests (the door included, on local servers); the runner against a fake api; the services and the API on a real Postgres and Redis (`HG_TEST_DATABASE_URL`, `HG_TEST_REDIS_URL`) with a fake runner; the console in a browser on a real API; tests against a live stack (`HG_TEST_STACK=1`) |
| `scripts/version-check`, `scripts/sync-builder-seeds`, `scripts/sync-cdn-libs` | the one version, everywhere it is written; the builder's copies of the template and the packages guide, kept current; the chat's CDN libraries written into its scripts at the versions `console/chat/package.json` pins, with fresh integrity hashes |

## Checks

CI runs these on every push; run them before every commit, the whole suite, not only the part you changed:

```bash
uv sync                                                        # once: .venv from uv.lock, Python 3.14, dev group included
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pytest -q tests/unit tests/runner packages
scripts/version-check
# with a scratch Postgres (a superuser URL; databases are created and dropped), a Redis, and Playwright's Chromium:
HG_TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres HG_TEST_REDIS_URL=redis://localhost:6379/15 \
  .venv/bin/pytest -q tests/services
HG_TEST_DATABASE_URL=... HG_TEST_REDIS_URL=... .venv/bin/pytest -q tests/e2e     # its own process (Playwright's sync API)
```

Integration tests need a running stack: `HG_TEST_STACK=1 HG_TEST_URL=http://localhost:8000 HG_TEST_KEY=hg_...
.venv/bin/pytest -q tests/integration` (an admin key; the operator tests also need `HG_TEST_OPERATOR_KEY`). They
install `shell` from `packages/shell`, install and remove a seeded test package and a test provider, and set the
organization's egress. CI runs them too (`integration.yml`): the compose stack from both images built on the runner,
boxes under runc. The other workflows: `ci.yml` (the checks above, the services and browser suites, the image
builds), `security.yml` (Trivy on both images and a dependency audit, on change and weekly), `release.yml`
(§Releasing), `freshness.yml` (weekly: is the built-in harness's CLI the current release), and Dependabot for the
Python lock (`uv`), the base image digests, the compose services, the chat's CDN libraries and the actions.

## Running it locally

`sudo ./deploy/install.sh` on a Linux host with Docker is the supported way and takes a few minutes. For development
without compose, run the processes from the checkout against a Postgres, a Redis and a directory object store:
`harnessgateway api` (`HG_DATABASE_URL`, `HG_REDIS_URL`, `HG_BLOB_URL=file:///...`, `HG_SECRET_KEY`, `HG_DOOR_KEY`),
then `harnessgateway init` for the organization and admin key, then `harnessgateway worker` as root with Docker and the
`runsc` runtime (`HG_SANDBOX_RUNTIME=runc` only where gVisor is unavailable; nothing is isolated then) and
`harnessgateway door`. The console is `python3 console/server.py --api http://localhost:8000`. Every setting is in
`docs/operations.md`.

## Rules

- **The contract is the OpenAI Responses schema, in both directions** (SPEC §4). No HarnessGateway request or
  response schema; output is validated with the OpenAI SDK's own types.
- **The platform knows no CLI.** Anything CLI-specific belongs in a package. Never change the platform for a harness:
  if a harness cannot work within the contract, stop and report the exact blocker and what it would need. Nothing
  CLI-specific is ever baked into an image: a package's `install` runs in the organization's own install box.
- **The request serves the task; the package defines the agent** (SPEC §3). A request may select or narrow what a
  package offers, never widen it.
- **No secret ever enters a box** (SPEC §8): placeholders inside, values injected at the door. No secret in any file,
  log, commit, test fixture or error message either.
- **Nothing holds state but the stores.** The api, the worker and the door keep caches at most; a box keeps its
  session's files only while it lives. Anything that must survive is in Postgres, Redis or the object store.
- **Negative space.** Every boundary (request, manifest, settings, egress entry, path, archive, a box's report) refuses
  what is not allowed with a message that names what is. Invariants are asserted, not handled.
- **Ports and adapters.** Vendor SDKs only in `harnessgateway/adapters/`; standards (Postgres, Redis, OCI images,
  HTTP, the Responses wire) are used directly, not wrapped.
- **The platform knows no language either.** A package is `harness.json` plus two executables, `install` and `run`,
  run as they are; the platform never looks inside them, never treats a file name specially, and the console never
  assumes Python. hgkit is an optional SDK for packages that happen to be Python, never a platform requirement.
- **Packages** follow SPEC §5: `harness.json`, `install`, `run`, `README.md`, `tests/` with fixtures recorded from
  real runs. Install each CLI by its official, documented method, pinned to the manifest's version
  (`HG_PACKAGE_VERSION`), and verify the version; never name a CPU in a script; no post-install edits of installed
  files. The template seeds nothing an organization would want to own, and neither does a package meant for others to
  start from. After changing `packages/shell` or `docs/packages.md`, run `scripts/sync-builder-seeds` (a unit test
  holds the builder skill's copies to them).
- **Versions are pinned where they are built from, and moved by Dependabot.** `uv.lock` is the Python environment of
  both CI and the platform image (`uv sync --frozen`; never `pip install` a range); the base images are digests; the
  compose services, the actions and the chat's CDN libraries (`console/chat/package.json`, then
  `scripts/sync-cdn-libs`) are pinned too. The built-in's CLI is `harness.json`'s `version`; `freshness.yml` says
  weekly whether it is current. Runtimes (Python, Node, Postgres, Redis) follow the current stable or LTS major;
  Postgres moves for new installations only, with the operator's own data migration (docs/operations.md).
- **Never mention "harnessrouter"** (any spelling) in any file. CI greps for it.

## Working style

- Think before coding: state assumptions, surface tradeoffs, ask when two readings lead to different work.
- Minimum code that solves the problem. No speculative features, abstractions for single-use code, or configurability
  nobody asked for. If 200 lines could be 50, rewrite.
- Surgical changes: touch only what the task needs, match the surrounding style, remove only what your change made
  unused. Mention unrelated dead code; do not delete it.
- Define the check before the change (a failing test, a reproduced bug, a command that must pass), then loop until
  it passes. Report outcomes as they are.
- Prose in code and docs says what a thing is and why, in plain sentences; no marketing, no filler.

## Releasing

`CHANGELOG.md`'s top entry is the version. `scripts/version-check` holds `pyproject.toml`, `harnessgateway/__init__.py`,
`deploy/compose.yaml`, `deploy/install.sh`, `deploy/.env.example`, `docs/operations.md` and both Dockerfiles to it. A push to `main` whose
version has no tag builds both images for amd64 and arm64, pushes them to GHCR, tags `v<version>` and publishes the
entry as the release. Both images are versioned together with the platform; a package revision bumps the package's
own version in its manifest.
