# Contributing

## The rules of this codebase

- **Every line is justified.** The least code that does the job. No option nobody asked for, no code for a case
  that cannot happen, nothing kept for later.
- **Say it once.** One definition of each rule. If two places need it, one of them imports it.
- **One responsibility** for each module and each class. A module's first lines say what it is for; if that takes
  an "and", it is two modules.
- **Rules are pure.** Anything that can be decided without I/O lives in `domain/` and is decided there.
- **Validate at every boundary**, and refuse with a message that names what was wrong and what is allowed.
- **The platform knows no harness.** Anything about one agent CLI belongs in its package.
- **The Responses API is the only schema.** A package reads the request as it was sent and prints output items
  and stream events as that API has them. Nothing of the platform's own is added to either: no helper to import,
  no line of its own to print.
- **What ships stands apart.** `packages/` and `environments/` are not the server's code. They know the
  platform only by its contract (`contract/`) and never import it; the server finds them in one place
  (`shipped.py`); their tests are `tests/shipped/`. Keep it so: any of them can then move to a repository of its own.
- **The console is a client.** It calls the public API and nothing else: what it needs, the API gives everyone.
  A page is drawn from DOM nodes, never from a string of markup, and what an agent made runs only sandboxed.
- **The Responses API is not extended.** No request field, no response field, no route under its paths.
- **An edition extends at a seam.** A change that makes the core know about an edition is the wrong change.
- **No secret** in any file, log, test, commit or error message.
- **No backward compatibility until 1.0, and no dead code**: change the thing and everything that used it.
- **The schema** is the numbered files in `store/schema/`. A released file is never edited: a change is a new file.
- **Comments** say what a thing is and why, in plain sentences. The code says how.

## Working on it

```sh
uv sync --extra dev
uv run ruff check . && uv run mypy          # lint, and strict types
uv run pytest tests/unit tests/shipped      # the rules, and what ships: no database, no Docker
./install.sh && tests/run.sh                # everything, on a real database
HG_URL=http://127.0.0.1:8000 HG_KEY=hg_… python3 tests/live/turns.py     # real containers
HG_URL=http://127.0.0.1:8000 HG_KEY=hg_… node tests/live/console.cjs     # the console, in a real browser
```

A change starts with a test that fails without it: unit for a rule, integration for a store, the runner or the
API, live for what only a real container shows. Say which levels you ran.

Commits are small and say why. One change, one commit.

## Versions

Every change that reaches `main` is a version. Give it one in `pyproject.toml` and `src/harnessgateway/__init__.py`
(then `uv lock`), and an entry in `CHANGELOG.md` that says what changed for someone who runs the platform. A push
to `main` whose version has no tag yet is released by `.github/workflows/release.yml`: the image is built and
pushed, the commit is tagged, and the entry becomes the release's notes. A push whose version is already released
fails, and says so.
