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
uv run pytest tests/unit                    # the rules: no database, no Docker
./install.sh && tests/run.sh                # everything, on a real database
HG_URL=http://127.0.0.1:8000 HG_KEY=hg_… python3 tests/live/turns.py     # real containers
```

A change starts with a test that fails without it: unit for a rule, integration for a store, the runner or the
API, live for what only a real container shows. Say which levels you ran.

Commits are small and say why. One change, one commit.
