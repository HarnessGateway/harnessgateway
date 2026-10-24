# Contributing and releases

```bash
git clone https://github.com/HarnessGateway/harnessgateway && cd harnessgateway
uv sync --extra dev
uv run pytest -q                     # the suite without Docker (SQLite, a directory, moto for S3)
uv run pytest -q -m docker           # the Docker tests, against a real daemon
uv run harnessgateway serve --dev    # the platform without Docker, for the console and packages
uvx --from mkdocs-material mkdocs serve   # this documentation at :8000/docs/
```

## The layers

The package is layered and `tests/test_layers.py` holds the rule: a module imports only its own
layer or below.

| Layer | Modules | Knows about |
|---|---|---|
| 0 | `errors`, `execution`, `secrets`, `files` | nothing of ours |
| 1 | `runtimes/process`, `runtimes/docker`, `runtimes/door`, `runtimes/turn.sh` | running a command, in a container, behind a door |
| 2 | `harness`, `gitstore`, `wire` | a package's contents; a git repository; the Responses schema |
| 3 | `records`, `blobs`, `limits`, `store`, `catalog`, `accounts` | what the platform keeps |
| 4 | `sandboxes`, `gateway` | a node's Docker; the provider relay |
| 5 | `sessions`, `worker` | turns and sessions; running them |
| 6 | `api/*`, `__main__`, `cli` | HTTP, settings, the command line |

`wire.py` is the only module that imports the OpenAI SDK; `harness.py` the only one that knows
what is in a package; `gateway.py` the only process where a provider's key is in the clear.

## Tests

`tests/fakes.py` has a fake node that runs packages as processes, so the API suite exercises
whole turns, streams, continuations, steers, cancels, `response.sh` shaping, the harness
repository, teams and rodeos without Docker. `tests/test_docker.py` runs a real sandbox: uid,
capabilities, the token, the door, a steer, a stop. CI runs both, on SQLite and Postgres, plus
lint, the console's scripts and a strict docs build.

## Packages

A catalog package is `catalog/<name>/`. Change one, run it with `hg harness run`, record the
result in its `catalog.json` (`verified`), run `python scripts/catalog_docs.py` so its page
follows, and commit. The [harness-mapping skill](https://github.com/HarnessGateway/harnessgateway/blob/main/.agents/skills/harness-mapping/SKILL.md)
is the research procedure; the [harness-package skill](https://github.com/HarnessGateway/harnessgateway/blob/main/.agents/skills/harness-package/SKILL.md)
is the writing procedure. `scripts/live.py` runs the three verification checks against a running
platform for every catalog package.

## Releases

A release is a `CHANGELOG.md` section merged to `main`. The newest section's heading is the
version (`pyproject.toml` reads it from there too); when it has no tag yet, the release workflow
tests that commit, builds the wheel and the image for amd64 and arm64, tags it `vX.Y.Z`, publishes
a GitHub release with the wheel and the section as its notes, and pushes the image to
`ghcr.io/harnessgateway/harnessgateway` as `:X.Y.Z`, `:X` and `:latest`. Nothing else creates
tags. The sandbox base image is rebuilt weekly and on change by its own workflow, scanned, and
pushed as `ghcr.io/harnessgateway/harnessgateway-base`.

## Conventions

No framework and no build step in the console. Every public number has a ceiling. Nothing secret
in the repository, ever; scan a diff before committing. Comments say why, not what.
