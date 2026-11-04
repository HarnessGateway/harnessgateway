---
name: harness-package-builder
description: Build a HarnessGateway harness package for any coding-agent CLI, for any purpose. A vanilla package from a CLI's name ("Create a harness package for codex"), or a tailored, least-privilege one that makes that CLI an organization's own agent ("I need an agent that…", "harden", "for our team"). Use whenever someone asks for a harness, a package, an agent on the platform, or to add, scope or tailor a CLI. Covers the platform's contract (a Responses request in, Responses events out), the three files, continuity (stop, interrupt, rewind, fork, a change of harness), seeds and the CLI's own permission mechanisms, observed egress, secrets, hgkit, recorded fixtures and validation.
---

# Harness package builder

You write **harness packages** for HarnessGateway. Read this whole file, then `reference/packages.md` (the
platform's guide) and `assets/template/` (the `shell` package every package starts from, with a README that says
what to change in each file and a `tests/` folder that shows how a package tests itself). Paths here are relative to
this skill's folder, `~/.claude/skills/harness-package-builder/`. What is not in the guide does not exist: never
invent a platform feature.

## The platform, in six lines

1. A client sends an OpenAI Responses request to `POST /v1/responses`. The platform runs it in a gVisor sandbox
   through a package and streams OpenAI Responses events back. There is no other schema, in either direction.
2. **The request serves the task; the package defines the agent.** Input, files, instructions and per-call knobs
   come with the request. The CLI, its version, its standing instructions, permissions, hooks, MCP catalog, egress
   and secrets are the package. A request may narrow what a package offers, never widen it.
3. **The platform knows no CLI and no language.** It reads `harness.json`, runs `install` and `run` as they are,
   lays down `seeds/`, moves bytes, fences the sandbox, keeps the session. Everything CLI-specific is yours.
4. **Two fences, both allow-only.** Outside: the egress door (host allow-list, deny wins, private addresses
   refused), secrets as placeholders injected only toward their bound hosts, the model reached only through the
   gateway. Inside: the CLI's own permission rules and hooks, which a tailored package seeds.
5. **No secret ever enters the sandbox.** The provider key is a per-turn token; declared secrets are placeholders.
   Never write a key or token into a file.
6. **Continuity is the platform's, in two harness-agnostic forms** (below). A package must work with both.

## What a package is

A folder `/workspace/<name>/`. Three files make it; the rest is for the people who maintain it.

| File | What you write |
|---|---|
| `harness.json` | `name`; `version` as `<CLI version>-1` (`install` and `run` get it as `HG_PACKAGE_VERSION`); a one-paragraph `description`; `provider.apis` the CLI speaks (`anthropic`, `openai`, `google`; omit for no model); `defaults.model`, **a model the organization's provider serves** (this session's `HG_MODEL` unless the person names one), never the vendor's default; `install.egress` and `run.egress`, **as observed**; `persist`, the CLI's session store under `$HOME`; `settings`, `secrets`, `mcp` when the agent needs them |
| `install` | an executable (`#!/usr/bin/env bash`): the vendor's documented Linux install of version `${HG_PACKAGE_VERSION%-*}` into `$HG_TOOLS`; never name a CPU; end with the CLI's version command and `installed: …` |
| `run` | an executable in any language: one turn. The request (`$HG_REQUEST`, stdin) in, Responses events (one JSON object per stdout line) out, exactly one terminal event last. In Python with hgkit: `Request.load()` → the CLI's command and environment; run it; its output → `out.say`, `out.think`, `out.ran`, `out.wrote` / `out.edited`, `out.called`, `out.used`; then `out.done`, `out.stopped` or `out.failed`. `reference/events.md` is the cookbook, `reference/request-fields.md` the field map |
| `seeds/home/` | tailored only: the agent's standing configuration in the CLI's own files (instructions, permission rules, hooks, skills); reseeded in every new sandbox |
| `seeds/workspace/` | tailored only: a starter project, when the purpose needs one |
| `README.md` | Files; The command; Request fields (field → where it goes; the ignored ones); Output mapping; Continuity (how the CLI resumes, and from the history); Known limits; Tests. Tailored: also Purpose; May and may not (as proven); Operator settings |
| `tests/` | recorded CLI output as fixtures, and a `test_run.py` that runs `run` on them and checks every line with `hgkit.check` |

A vanilla package has no `seeds/` unless the CLI cannot run headless without a config file, and then the minimum.
Delete the template's `seeds/workspace/README.md`. `install` and `run` are executable and start with a shebang line.

## How a conversation continues

A client may stop or interrupt a turn, rewind to an earlier response, branch from one, or continue on another
harness, all by naming a response in `previous_response_id`. The platform never tells the package which happened;
from inside the sandbox each is a continuation, on a fresh box or the warm one. Two things carry the conversation:

1. **The CLI's own session**, kept under `$HOME` where `persist` names it, restored into every new box. `run` saves
   the CLI's session id in `req.state` **the moment the CLI reports it** (a killed turn must still resume), and
   resumes with it on the next turn.
2. **The history**, `req.history()` (`$HG_HISTORY`): every earlier turn's input and output items in Responses form,
   a cancelled turn's partial output included, through every fork. When there is no saved id, or the CLI refuses it,
   `run` starts a fresh CLI session and gives it the conversation from the history (`req.transcript()` as text, or
   written into the CLI's own transcript format). A fork onto this harness from another arrives this way: the
   workspace and the history, nothing of the other CLI.

Native resume is the richer path; the history is what makes every path work. Both are required of every package.

## Two kinds of package

| | Vanilla | Tailored |
|---|---|---|
| Is | the CLI as its vendor ships it, headless in the sandbox, nothing an organization would own | an organization's agent on that CLI: a purpose, least privilege, its tools, its knobs |
| Asked as | a CLI's name and nothing else | a purpose, a team, a restriction, a tailoring |
| Questions | none | one round, then a plan and a go |
| Adds | — | `seeds/home` (instructions, the CLI's permission rules, hooks), `mcp`, `settings`, `secrets`, narrowed `run.egress`, timeouts, a proven forbidden run |

When you cannot tell, ask one question: "A vanilla package for `<cli>`, or an agent built for a purpose? If a
purpose, what is it?" A tailored package is built on a vanilla base; say in the plan whether the base is delivered
beside it as its own package (default: yes, so the organization can tailor again), and build only what the plan says.

## Working with the person

Each turn is one headless run, so you cannot wait for an answer inside a turn: ask by ending your reply. Ask only
what changes what you will build, numbered, with a proposed default for each, three to six at most
(`reference/tailoring.md` has the tailoring questions). For a tailored package send the plan as a short table and
ask for a go; for a vanilla one, build. Explain each tailoring choice in one sentence as you make it; the README is
the lasting version. End with a short summary: what you pinned, the egress and why, what maps to what, the two
continuity paths, what the operator must set, what you could not try. The person installs the package from the
console (New harness from workspace, on this session's page); a turn on it is the real test, and the session page
shows every host the door denied, which they bring back to you.

## How to build one

**Research the CLI first.** From its official home (repository, documentation, registry page): the latest stable
release and how it is installed on Linux; its headless mode and machine-readable output; how a session is resumed
by id and where its store lives; how it takes a base URL, an API key and a model, and which API shapes it speaks;
how prompts, telemetry and update checks are switched off; where it reads instruction files, permission rules,
hooks, MCP config. Your knowledge may be stale: the current docs and `--help` win.

**Install it and record real runs.** Install the pinned release the documented way into `/tmp/tools`; scratch runs
stay under `/tmp` (the workspace is snapshotted; only the package belongs in it). Run the CLI against this session's
provider when the shapes match: `HG_PROVIDER_URL` (no `/v1`), `HG_PROVIDER_TOKEN`, `HG_PROVIDER_API`, `HG_MODEL`;
pin the model. **Observe egress rather than guess it:** run both the install and the CLI through
`assets/observe-egress.py`, which logs every host they ask for; those lists are `install.egress` and `run.egress`.
Record as fixtures: a text turn, a tool turn, a resumed turn, a turn you kill mid-way (SIGTERM) and the turn that
resumes it, a provider error, a stopped-short turn. A CLI whose shape differs from the provider's cannot be run
here: write the mapping from its documented output, say so, and keep the fixtures to what you could record.

**Write `run` and validate it.** Start from the template; map the request with `reference/request-fields.md` and
print events with `reference/events.md`; implement both continuity paths; `out.ignored([...])` for fields the CLI
cannot honour; the CLI's raw lines in the log with `out.log(...)`. Then the checklist below, the tests, and
`install` run end to end into a scratch `$HG_TOOLS`.

**Tailored: the organization's layer.** Give it the purpose in `seeds/home` (the CLI's instruction file: what it
does, how it answers, what it refuses); fence it inside with the CLI's permission mechanism (tools allowed,
everything else denied, paths and commands restricted, a hook for what the rules cannot express); its tools as the
`mcp` catalog with `allowed_tools` narrowed and each credential a declared `secret` bound to the server's host; its
knobs as `settings` and `secrets`; `run.egress` only what the purpose needs beyond the provider and the catalog's
hosts, including any host a login or OAuth flow redirects to; `run.timeout_s` to what a task takes. Record a
realistic run and a forbidden one (the path, host or tool it may not use) that shows the refusal.
`reference/tailoring.md` has the layers and where each CLI keeps its mechanisms.

## The checklist, before any hand-back

- `python3 -m hgkit check` passes on every recorded output; `Emitter(validate=True)` while developing.
- Exactly one terminal event, last; a CLI or provider error is `response.failed` with a clear message, never a
  silent exit; the CLI's raw lines are in the log, never in the events.
- The session id is in `req.state` as soon as the CLI prints it; a turn killed with SIGTERM is resumed by the next.
- A first turn given a history (set `HG_HISTORY` to a recorded one) continues the conversation.
- `defaults.model` is served by this provider; `install.egress` and `run.egress` are the observed hosts.
- No placeholder, token or key appears in anything `run` prints or writes.
- Tailored: the forbidden run is recorded and the README's "may not" table states only what was demonstrated.

## Rules the platform enforces

Every stdout line with `"type": "response.…"` must be a valid event of that type (the OpenAI SDK's model, exactly;
`hgkit.check` is that check). `harness.json` refuses unknown fields; `name` is `[a-z0-9][a-z0-9._-]{0,62}`; egress
entries are `host`, `*.domain` or `*`, with an optional `:port`, `!` to deny; settings and secrets are named like
environment variables, not starting `HG_`. The package a turn runs has only the egress its manifest declares: the
full network you have while building is yours, not the harness's. Narrowing is safe; widening is escalation: a
tailored package never grants more than the vanilla CLI has, nor more than the purpose needs.
