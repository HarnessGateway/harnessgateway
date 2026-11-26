# HarnessGateway

Agent infrastructure. Every request to the OpenAI Responses API is run by an agent harness CLI in a sandbox of its
own, and answered as a Responses response.

You build an agent against the Responses API, unmodified. Which agent CLI runs it, where, and with which model is
one field of the request:

    model = environment / harness / provider / model          dev/my-agent/deepseek/deepseek-flash

Change any part and the agent built on the API does not change: the same request, the same session, the same
files. A harness that calls no model is `environment/harness/none/none`.

This repository is the open-source core: one host, Docker, one organization, API keys. It is an API, and it comes
with a console for it: a client of that API, which nothing in the API needs.

## Run it

The image is the whole distribution. A host needs Docker with Compose v2, and nothing from this repository:

```sh
image=ghcr.io/harnessgateway/harnessgateway:1.1.0
docker run --rm "$image" harnessgateway compose > compose.yaml    # how it is started, for this version
docker run --rm "$image" harnessgateway secrets > .env            # once: the installation's secrets. Keep it
docker compose up --detach --wait                                  # the database, the platform, the egress proxy
docker compose exec serve harnessgateway init                      # the defaults, and the first API key, shown once
```

Then open `http://127.0.0.1:8000` and sign in with the key: that is the console. Or use the API alone. `.env`
also holds the port, the address it listens on, and a namespace, for a second installation on one host.

**To update**, run the first command with the new image and `docker compose up --detach --wait`. The schema is
brought up to date before anything is served, the data stays, and a version started by mistake on a database a
later one changed refuses to start.

It is one image run twice (`serve`: the API and the turn runner; `proxy`: the egress proxy) and PostgreSQL. Each
turn is one more container, started through the Docker socket and gone when the turn ends. With gVisor (`runsc`)
installed, sandboxes run under it; without it, isolation is `none`, and the log says so at every start.

## What it starts with

| | |
|---|---|
| `default` | An environment on a common image (`node:22`), with the default limits and an open network: a sandbox reaches any public host, through the proxy |
| `shell` | The smallest harness: it runs a request's input as a command. `default/shell/none/none` tries an environment before any agent is set up |
| `harness-package-builder` | Builds every other harness package. Name an agent CLI, or describe the agent you want on one; it researches the CLI, writes `install` and `run`, and ends with a link that adds it. It carries the package contract and the Responses API as schemas. It needs a provider that speaks the Anthropic API |

The two packages are the platform's: an update of the platform updates them, and they are not changed in place (a
copy under another name is yours). The environment is given once, at setup, and is yours from then on. Nothing else
ships: no provider, and no other agent.

## The three things you define

| | | |
|---|---|---|
| **Provider** | `PUT /v1/providers/{name}` | A model endpoint: each API shape it speaks at a base URL, and a key |
| **Harness** | `PUT /v1/harnesses/{name}` | An agent CLI as a package you own, and nothing else: two programs, `install` and a `run` that reads the Responses request and prints Responses output ([the example package](packages/harness-package-builder/example/README.md)) |
| **Environment** | `PUT /v1/environments/{name}` | Where turns run, and the only door: image, limits, network, variables, secrets, a startup script, and the harnesses it carries. Everything a package may reach or hold is said here |

```sh
curl -X PUT $HG/v1/providers/deepseek -H "Authorization: Bearer $KEY" \
     -d '{"apis": {"openai-responses": "https://api.deepseek.com/v1"}, "key": "sk-…"}'

tar -czf my-agent.tar.gz -C my-agent . &&
curl -X PUT $HG/v1/harnesses/my-agent -H "Authorization: Bearer $KEY" --data-binary @my-agent.tar.gz

curl -X PUT $HG/v1/environments/dev -H "Authorization: Bearer $KEY" -d '{
  "image": "node:22",
  "harnesses": ["my-agent"],
  "cpus": 2, "memory_mb": 4096, "disk_mb": 2048, "timeout_s": 900,
  "network": {"mode": "limited", "allow": ["api.github.com"]},
  "variables": {"TEAM": "demo"},
  "secrets": {"GITHUB_TOKEN": {"hosts": ["api.github.com"], "header": "Authorization", "prefix": "Bearer ", "value": "…"}},
  "scripts": {"startup": "git -C repo pull --ff-only || git clone https://github.com/acme/repo.git repo"}
}'
```

`PUT` makes or replaces. A secret is write-only: it is sealed at rest, never returned, and never enters a
sandbox; the sandbox holds a placeholder, and the egress proxy puts the value in, on the hosts it is bound to.

## Then it is the Responses API

```python
from openai import OpenAI
client = OpenAI(base_url=f"{HG}/v1", api_key=KEY)

first = client.responses.create(model="dev/my-agent/deepseek/deepseek-flash", input="Write hello.py and run it.")
again = client.responses.create(model="dev/my-agent/deepseek/deepseek-flash", input="Now add a test.",
                                previous_response_id=first.id)        # the same session: its files and the agent's memory
```

Blocking, `stream`, `background`, `store`, cancel, delete and input items work as the API describes them.
`previous_response_id` of a session's latest response continues it; of an earlier one, forks it: that is rewind.

Nothing is ignored unsaid. A request the API would not take is refused, naming the field; a field a harness cannot
honour fails the response, naming the field.

## A session, and changing what runs it

A session's whole state is two folders: the mind (`/mind`, the agent's home, where a harness keeps its own memory)
and the work (`/work`). Both are saved after every turn, as a snapshot that costs only what changed, so any
response can be returned to.

Any part of the model line may change from one turn to the next. When the harness changes, the new one is handed
nothing that was said: it is told who worked on the session and where each keeps its memory, and reads up by
itself, in the mind they share.

A caller may keep the conversation instead, as the API allows: send the whole of it in `input`, with
`store: false`, and nothing is held here.

## The whole API

| | |
|---|---|
| `POST /v1/responses`, `GET` `DELETE /v1/responses/{id}`, `POST …/cancel`, `GET …/input_items` | The Responses API |
| `POST` `GET /v1/files`, `GET` `DELETE /v1/files/{id}`, `GET …/content` | The Files API: what a request's `input_file` names |
| `GET /v1/containers/{id}`, `…/files`, `…/files/{id}`, `…/content` | The Containers API: what an agent made in `/work` |
| `POST /v1/containers/{id}/view` | A link to those files that a browser opens by itself, for an hour: `{url}/{path}` shows an image, plays a video, runs a page an agent built, sandboxed |
| `GET` `PUT` `DELETE /v1/{providers,environments,harnesses}/{name}`, `GET /v1/harnesses/{name}/package` | The three definitions |
| `GET /v1/sessions`, `GET` `DELETE /v1/sessions/{id}`, `GET …/turns` | Every session the caller may read, as the requests and responses it is made of, each with who started it (`owner`) |
| `GET /v1/usage` | What the organization used: CPU, memory, network, storage. Its lifetime, or `?start=&end=` |
| `POST` `GET /v1/keys`, `DELETE /v1/keys/{id}` | API keys |
| `GET /v1/platform`, `/healthz`, `/readyz` | What this installation is, and whether it can work |

A response's `metadata.hg_session` names its session; `cntr_` and the rest of that id is its container. Every
answer names its caller in the header `hg-subject`, which is how a client tells which sessions are its own. A refusal
is the OpenAI error envelope, and names the field that was wrong.

## The console

Chat, the three definitions, sessions and usage, at `/console` on the API's own address. It calls only the routes
above, with the key you sign in with, so anything it does a program can do the same way. `HG_CONSOLE=off` serves
the API without it. [docs/walkthrough.mp4](docs/walkthrough.mp4) is a captioned walkthrough of it, recorded on
1.0.0.

The chat in it is an application built on the Responses API alone, and shows what one can be: replies streamed as
markdown with the agent's thinking and steps, files given with a message, a message that interrupts a running turn
and steers it, rewind and branch (the API's fork), and a panel that previews what the agent made: pages that run,
images, video, PDF, documents, tables, notebooks, diagrams and source. Code is highlighted, mathematics typeset
and diagrams, Word documents and spreadsheets drawn by libraries loaded from a CDN, each pinned to a version and
checked by an integrity hash; without them everything still reads.

## Editions

The core is complete by itself, and it is also what every other edition is built on. An edition is the core's
image with one or more packages added, which the core loads in order (`HG_EXTENSIONS`, set by the image). It is
never a fork: an extension adds its own at a seam, or narrows what the core allows, and takes the place of nothing.
[docs/EXTENDING.md](docs/EXTENDING.md) lists the seams.

## Where things are

| | |
|---|---|
| `src/harnessgateway/domain/` | The rules, pure: no I/O. Definitions, the model line, validation against the OpenAI SDK's own types, outcomes, a sandbox as a plan |
| `src/harnessgateway/store/` | PostgreSQL and the blob store: one module for each group of tables |
| `src/harnessgateway/driver/` | Where sandboxes run: the seam, and Docker on one host |
| `src/harnessgateway/runner/` | Takes turns from the queue and runs each: install, restore, sandbox, save, conclude |
| `src/harnessgateway/proxy/` | The egress proxy: a process of its own with no database and no sealing key |
| `src/harnessgateway/api/` | The routes |
| `src/harnessgateway/extensions.py`, `edition.py`, `testing.py` | The seams an edition builds on, everything of the core it may import, and what its tests are written with |
| `src/harnessgateway/compose.yaml` | How a released image is run: what `harnessgateway compose` prints |
| `packages/` | The packages that ship: `shell`, and `harness-package-builder`, which holds what every package is written to: the Responses API as three schemas generated from the pinned SDK (`contract/`), a whole example package (`example/`), and a session of a developer's requests (`requests/`) |
| `environments/` | What an organization starts with: the `default` environment, which carries both |
| `console/` | The console: plain JavaScript modules, served as they are. `lib/` (the API, the DOM, files), `components/` (what pages are made of), `pages/` (one for each), `styles/` |

[SPEC.md](SPEC.md) is the specification: everything needed to build the platform, and what the code is held to.
[docs/IN-BRIEF.md](docs/IN-BRIEF.md) is the platform in five short answers, and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) says how the parts work together.

## Checks

```sh
uv run ruff check . && uv run mypy          # lint, and strict types
uv run pytest tests/unit tests/shipped      # the rules, and what ships held to the contract: no database, no Docker
HG_TEST_DATABASE_URL=postgresql://… uv run pytest tests/integration     # on a database the tests may empty
tests/e2e/run.sh                            # the image built from this tree, installed as above, checked, and removed
```

Apache-2.0. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
