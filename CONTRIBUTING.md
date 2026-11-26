# Contributing

## The rules of this codebase

- **Every line is justified.** The least code that does the job. No option nobody asked for, no code for a case
  that cannot happen, nothing kept for later.
- **Say it once.** One definition of each rule. If two places need it, one of them imports it.
- **One responsibility** for each module and each class. A module's first lines say what it is for; if that takes
  an "and", it is two modules.
- **Rules are pure.** Anything that can be decided without I/O lives in `domain/` and is decided there.
- **Validate at every boundary**, and refuse with a message that names what was wrong and what is allowed.
- **What must not happen is written down, and checked.** A program is also the things it refuses. Every invariant
  is an assertion, a type, or a constraint of the database, so a state that must not exist cannot be stored, and
  every promise has a test that tries to break it. Nothing fails silently: what is caught is handled or logged.
- **The platform knows no harness.** Anything about one agent CLI belongs in its package.
- **The Responses API is the only schema.** A package reads the request as it was sent and prints output items
  and stream events as that API has them. Nothing of the platform's own is added to either: no helper to import,
  no line of its own to print.
- **What ships stands apart.** `packages/` and `environments/` are not the server's code. They know the
  platform only by its contract (which the builder's package holds) and never import it; the server finds them in one place
  (`shipped.py`); their tests are `tests/shipped/`. Keep it so: any of them can then move to a repository of its own.
- **The console is a client.** It calls the public API and nothing else: what it needs, the API gives everyone.
  A page is drawn from DOM nodes, never from a string of markup, and what an agent made runs only sandboxed.
- **The Responses API is not extended.** No request field, no response field, no route under its paths.
- **An edition extends at a seam.** A change that makes the core name an edition, or behave differently because
  one is loaded, is the wrong change. An extension adds or narrows, and takes the place of nothing.
- **What an extension imports is `harnessgateway.edition`.** Moving or renaming anything it names is a change every
  edition must follow: say so in the changelog.
- **No secret** in any file, log, test, commit or error message.
- **Installations are updated in place.** From any 1.x to a later one, with their data, by the two commands of an
  update. A version that asks more of whoever runs the platform is a new major version, and its changelog entry
  says what. There is no dead code: change the thing and everything that used it.
- **The schema** is the numbered files in `store/schema/`. A released file is never edited: a change is a new file.
- **Comments** say what a thing is and why, in plain sentences. The code says how.

## Working on it

```sh
uv sync --extra dev
uv run ruff check . && uv run mypy          # lint, and strict types
uv run pytest tests/unit tests/shipped      # the rules, and what ships: no database, no Docker
HG_TEST_DATABASE_URL=postgresql://… uv run pytest tests/integration     # on a database the tests may empty
tests/e2e/run.sh                            # the image built from this tree, installed from it alone, and removed
```

A change starts with a test that fails without it, at the lowest level that can show it: **unit** for a rule (no
I/O), **integration** for a store, the runner, the API, or a seam (a real database, a stand-in sandbox), **end to
end** for what only a real installation shows (the released image, real containers, a real browser). A change is
done when all three levels pass. Say which you ran.

To run a checkout as a release is run: `uv run harnessgateway secrets > .env`, once, then
`docker compose --project-directory . -f src/harnessgateway/compose.yaml -f compose.dev.yaml up --build --wait`.

Commits are small and say why. One change, one commit.

## Versions

Every change that reaches `main` is a version. Give it one in `pyproject.toml`, `src/harnessgateway/__init__.py`,
the `Dockerfile` (the image's own name), and `SPEC.md` (then `uv lock`; a test holds them together), and an entry
in `CHANGELOG.md` that says what changed for someone who runs the platform. A push
to `main` whose version has no tag yet is released by `.github/workflows/release.yml`: the image is built and
pushed, the commit is tagged, and the entry becomes the release's notes. A push whose version is already released
fails, and says so.
