# Working on HarnessGateway

`SPEC.md` is the design and the authority. Read the section you are about to change. When code and
specification disagree, decide which is right, then change both in the same commit.

## What this is

A platform that runs any agent harness CLI in a sandbox, behind the unmodified OpenAI Responses API. This
repository is the open-source core: one host, Docker, one organization, API keys. Kubernetes, single sign-on
and multi-tenancy are other editions, built on the extension points in `harnessgateway/extensions.py`. They
are not in this repository and nothing here knows of them.

## The shape

A turn is: restore step, then the sandbox, then save step, with a watch step beside the sandbox.

| Where | What |
|---|---|
| `harnessgateway/domain/` | The rules, pure: no I/O, no clock, no settings. A turn's plan is plain data built here |
| `harnessgateway/drivers/` | Renders a plan as a `docker run` command and runs it. Nothing else touches Docker |
| `harnessgateway/store/` | PostgreSQL and the blob store. One module per group of tables |
| `harnessgateway/runner/` | Takes turns from the queue and runs each through its steps |
| `harnessgateway/steps/` | Restore, save, watch, unpack: short trusted containers that call the internal API |
| `harnessgateway/proxy/` | The egress proxy: the sandbox's only way out, and where secrets are put in |
| `harnessgateway/api/` | Public (Responses, Files, Containers), management, and internal routes |
| `harnessgateway/kit/hgkit.py` | What packages import. Standard library only, Python 3.9 or later |
| `harnessgateway/console/` | The console: static files, plain JavaScript modules, no build step. `app.js` is the shell, `pages.js` and `session.js` the pages, `chat/` the chat (`chat/backend.js` is its only way to the platform). `api.js`, `ui.js` and `extensions.js` are what other editions' pages build on: keep their exports |
| `packages/shell/` | The template package, and what the tests run |

## Rules

- Tests first. A change starts with a failing test: unit for anything pure, integration for stores, steps,
  the proxy and the API, end to end for what only real containers show.
- Keep it simple. The least code that meets the specification. No option the specification does not call for.
- Say it once. One definition of each rule.
- One responsibility per class and per module. Collaborators are passed in, never reached for.
- Validate at every boundary. Refuse with a message that names what was wrong and what is allowed.
- The platform knows no harness. Anything about one CLI belongs in its package, in the packages repository.
- The Responses API is not extended. No added request fields, no added endpoints under its paths.
- No secret in any file, log, test fixture, commit or error message.
- No backward compatibility and no dead code: change the thing, and everything that used the old thing.
- The schema is one file, `store/migrations/0001_schema.sql`, edited in place until the first release.
- Comments say what a thing is and why, in plain sentences.

## Checks

```sh
uv run ruff check .
uv run mypy
uv run pytest                                                       # unit
HG_TEST_DATABASE_URL=postgresql://... uv run pytest                 # + integration
HG_TEST_DATABASE_URL=... HG_TEST_DOCKER=1 uv run pytest tests/e2e   # real containers
HG_TEST_URL=http://localhost:8000 HG_TEST_KEY=hg_... node tests/browser/console.mjs   # the console
```

The end-to-end tests need the Docker daemon to themselves: a platform removes labelled containers it does
not own, so a second platform on the same daemon breaks both. A level that is not enabled is skipped, never
passed; say which levels ran when you report.

## Releases

The version is in `pyproject.toml` and `deploy/install.sh`. A push to `main` with a new version publishes
the images and a release. Nothing is pushed to `main` without its owner saying so.
