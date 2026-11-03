---
name: harness-package-builder
description: Build a HarnessGateway harness package: a vanilla one for any coding-agent CLI from its name alone ("Create a harness package for <name>"), or a tailored, tightly scoped one that makes that CLI an organization's own agent ("I need an agent that…", "harden", "for our team", "purpose-built"). Use whenever someone asks for a harness, a package, an agent on the platform, or to add, scope, lock down or tailor a CLI. Covers finding out the purpose with the person, researching the CLI, the contract (an OpenAI Responses request in, Responses stream events out), the three files (harness.json, install, run), seeds and the CLI's own permission mechanisms for least privilege, egress, secrets, hgkit, recording real CLI output, and validating the output against the Responses schema before handing the package back.
---

# Harness package builder

You write **harness packages** for HarnessGateway: the unit the platform runs. The platform knows no CLI and no
language; a package teaches it one agent. You are in a sandbox on the platform with full outbound network access, so
you can read the project's documentation, install the CLI, run it, record its output, and write and check the
package here.

Two documents are the ground truth; read both before writing anything:

- `reference/packages.md`: the platform's guide to packages (the manifest, the turn environment, hgkit,
  secrets, persistence, testing).
- `assets/template/`: the `shell` package, the template every package starts from, with a README that says what
  to change in each file, and a `tests/` folder that shows how a package tests itself.

`reference/events.md` is the cookbook of what a package prints, `reference/request-fields.md` says where each request
field usually goes, `reference/tailoring.md` is how an organization's purpose becomes a package. Every path here is
relative to this skill's folder, `~/.claude/skills/harness-package-builder/`. Do not invent
platform features: what is not in the guide does not exist.

## Two kinds of package

| | Vanilla | Tailored |
|---|---|---|
| What it is | the CLI as its vendor ships it plus what it needs to run headless in the sandbox | an organization's own agent on that CLI: a purpose, least privilege, its tools, its knobs |
| Who starts from it | anyone; it is the base others tailor | the organization that asked; its applications call it |
| Seeds | none (or the minimum a config file forces) | `seeds/home`: instructions, the CLI's permission rules, hooks, skills; `seeds/workspace`: a starter project |
| Manifest | provider, install egress, persist | plus `mcp`, `settings`, `secrets`, narrowed `run.egress`, timeouts, a default model |
| The request that asks for it | "Create a harness package for codex" | "I need an agent that triages support tickets", "harden codex for our docs team", "make pi ours" |

A request that names a CLI and nothing else wants a vanilla package: build it, no questions. A request that names a
purpose, a team, a restriction or a tailoring wants a tailored package: find out what it is for first (below). When
you cannot tell, ask one question: "A vanilla package for `<cli>`, or an agent built for a purpose? If a purpose,
what is it?"

## Talking with the person

Each turn is one headless run: you cannot wait for an answer inside a turn. To ask, end your reply with your
questions, numbered, and stop; the person's next message continues this same session, with the workspace and your
notes intact. So:

- Ask only what changes what you will build, and propose a default for each question so a one-word answer suffices.
  Three to six questions at most, in one message; never one question per turn.
- Before building a tailored package, send the plan as a short table (purpose, CLI, model, what it may reach, what it
  may do, what the operator will set) and ask for a go. When the person gave everything up front, say the plan and
  proceed in the same turn.
- Explain each tailoring choice in one sentence as you make it: what it restricts, and where it lives in the package.
  The person is learning how to make the agent theirs; the README you write is the lasting version of that.
- A long study (installing a large CLI, recording runs) is one long turn; say what you are about to do and do it.

## The contract, which is fixed

```
client ──(Responses request)──▶ platform ──(the same JSON, at $HG_REQUEST and on stdin)──▶ run ──▶ the CLI
client ◀──(Responses response)── platform ◀──(Responses stream events, one JSON object per stdout line)──┘
```

- **In:** the client's `POST /v1/responses` body, whole. The request is the task: `input` text and files,
  `instructions`, `reasoning.effort`, `text.format`, MCP servers, `max_tool_calls`.
- **Out:** Responses stream events on stdout, one JSON object per line; any other line is a log line. Exactly one
  terminal event (`response.completed`, `response.incomplete` or `response.failed`) ends the turn, last. The
  platform validates every event against the OpenAI SDK's own models and refuses the turn on the first bad one.
- **The package is the agent:** the CLI and its version, how it is run, what it persists, what it may reach, what it
  may do. The request may select or narrow what the package offers, never widen it.
- **A turn sees** `HG_REQUEST`, `HG_FILES`, `HG_MCP`, `HG_WORKSPACE` (the cwd), `HG_STATE`, `HOME`, `HG_PACKAGE_DIR`,
  `HG_TOOLS` (read-only at run time; `PATH` includes `$HG_TOOLS/bin`), `HG_PACKAGE_VERSION`, `HG_SESSION`,
  `HG_RESPONSE`, `HG_TURN`, `HG_MODEL`, `HG_PROVIDER_API`, `HG_PROVIDER_URL`, `HG_PROVIDER_TOKEN`, each declared
  setting and secret as a variable. The root filesystem is read-only, the user is uid 10001, `/tmp` does not persist,
  `$HOME` persists only where `persist` says, the workspace and `$HG_STATE` always do.
- **Two fences.** The platform fences the sandbox from outside: gVisor, the egress door (allow-list, deny wins,
  private addresses refused), secrets as placeholders injected only toward their bound hosts, the provider reached
  only through the gateway. A tailored package fences the agent from inside, in the CLI's own terms: which tools it
  may use, on which paths, with which hooks. Both are allow-only; default deny.

## What you deliver, which is fixed

A folder `/workspace/<name>/` with this layout (copy `assets/template/`, then edit). Three files make a package; the rest
is for the people who maintain it:

| File | What you write |
|---|---|
| `harness.json` | `name`; `version` as `<CLI version>-1` (the platform hands it to `install` and `run` as `HG_PACKAGE_VERSION`, so the version lives here and nowhere else); a one-paragraph `description`; `provider.apis`, the API shapes the CLI speaks (`anthropic`, `openai`, `google`; omit for no model); `defaults.model`; `install.egress`, the hosts `install` needs, and `install.timeout_s`; `run.egress`, the hosts a turn needs besides the provider (usually `[]`); `persist`, the CLI's session store under `$HOME`; `settings`, `secrets`, `mcp` when the agent needs them |
| `install` | an executable (`#!/usr/bin/env bash`): the vendor's documented Linux install of version `${HG_PACKAGE_VERSION%-*}` into `$HG_TOOLS`. End by running the CLI's version command and printing `installed: …` |
| `run` | an executable, in any language: one turn. Read the request, run the CLI, print events. In Python with hgkit (`#!/usr/bin/env python3`): `Request.load()` → the CLI's command and environment; run it; its output → `out.say`, `out.think`, `out.ran`, `out.wrote` / `out.edited`, `out.called`, `out.used`; `req.state` for the CLI's session id; one terminal event (`out.done`, `out.stopped`, `out.failed`) |
| `seeds/home/` | tailored only: the agent's standing configuration, in the CLI's own files (instructions, permission rules, hooks, skills). Reseeded in every new sandbox, so it always wins |
| `seeds/workspace/` | tailored only, when the agent needs a starter project or reference files |
| `README.md` | Files; The command; Request fields (a table: field → where it goes, and the ignored ones); Output mapping (CLI output → events); Known limits; Tests. Tailored: also Purpose; What it may and may not do; Operator settings |
| `tests/` | the recorded CLI output as fixtures and a `test_run.py` that runs the mapper on them and checks every line with `hgkit.check` |

A vanilla package has no `seeds/` unless the CLI cannot run headless without a config file; then the minimum, and
nothing an organization would want to own. Delete the template's `seeds/workspace/README.md`. Both `install` and `run`
must be executable (`chmod +x`) and start with a shebang line.

## How to work: a vanilla package

1. **Research the CLI first.** Find its official home (the project's repository, documentation site and package
   registry page) and read, in this order: how it is installed on Linux, its latest stable release, its headless or
   non-interactive mode, its machine-readable output (JSON lines, a stream-json flag, a JSON-RPC or ACP mode), how a
   session is resumed by id, how it is pointed at a custom base URL and API key and which API shapes it speaks, how
   prompts, telemetry and update checks are disabled, where it reads instruction files, config, permission rules,
   hooks and MCP servers (you will need these for tailoring). Fetch pages and clone repositories as you need; nothing
   outbound is blocked for you. Write what you found as a table, with the version you will pin, before touching code.
   Your knowledge of the CLI may be out of date: what the current docs and `--help` say wins.
2. **Install the latest stable version here, the way its project documents**, into `/tmp/tools` (a venv, `npm
   --prefix`, or the official installer pointed there), and keep your scratch runs under `/tmp`: the workspace is
   snapshotted after the turn, so only the package belongs in it. Note every host the install reached (the registry,
   GitHub releases, an installer script's download hosts): that list is `install.egress`. Never name a CPU
   architecture; the package manager or installer picks the build.
3. **Try it against this session's provider when you can.** Your environment has `HG_PROVIDER_URL` (a base URL
   without `/v1`), `HG_PROVIDER_TOKEN` (the API key to present), `HG_PROVIDER_API` (`anthropic`, `openai` or
   `google`) and `HG_MODEL`. A CLI that speaks that same shape can be run here for real: for `anthropic`, set
   `ANTHROPIC_BASE_URL=$HG_PROVIDER_URL` and `ANTHROPIC_API_KEY=$HG_PROVIDER_TOKEN`; for `openai`,
   `OPENAI_BASE_URL=$HG_PROVIDER_URL/v1` and `OPENAI_API_KEY=$HG_PROVIDER_TOKEN`; and pin the model to `$HG_MODEL`.
   Run small tasks (create a file, run a command, a second turn that resumes) and save the raw output as fixtures.
   Note any host the CLI reaches at run time besides the provider (a model catalog, telemetry): that is
   `run.egress`, or something to switch off. A CLI that speaks another shape cannot be run here: write the mapping
   from its documented output, say so in the README, and keep the fixtures to what you could record.
4. **Write `run`.** hgkit is importable here (`python3 -c "import hgkit, inspect; print(inspect.getsource(hgkit))"`
   shows the whole library; it is optional, and `run` may be any language that prints the same JSON lines). Build
   the command from the request: `req.prompt`, `req.instructions` appended to the CLI's own system prompt,
   `req.model`, `req.provider.url` and `req.provider.token` as the CLI's base URL and key, resume from the id saved
   in `req.state`. Map each output line: assistant text → `out.say` (streamed), thinking → `out.think` (streamed), a
   shell run → `out.ran`, a file write or edit → `out.wrote` / `out.edited`, an MCP tool → `out.called`, anything
   else → `out.used`; then `out.done(input=…, output=…, cached=…)`, `out.stopped(reason)` when the CLI stopped short,
   or `out.failed(code, message)` on an error. Report request fields the CLI cannot honour with `out.ignored([...])`.
   Keep the CLI's raw lines in the turn's log with `out.log(...)`: they are what you debug from.
5. **Validate the output before anyone else does.** `python3 -m hgkit check <stdout of your run>` reads it exactly
   as the platform will and names the first line and field it would refuse. `Emitter(validate=True)` does the same
   check on every event as it is printed, which is what to use while developing. Fix until it passes, then put the
   same runs in `tests/test_run.py` (`assets/template/tests/` shows the shape). Run `install` once end to end into a
   scratch `$HG_TOOLS` with `HG_PACKAGE_VERSION` set, to prove it.
6. **Hand back** the folder and a short note: the CLI version pinned, the hosts in `install.egress` and why, what maps
   to what, what you could and could not try here. The person installs it from the console (New harness from
   workspace, on this session's page) and a turn on it is its real test; if that fails, they will bring you the
   trace, and you fix `run`.

## How to work: a tailored package

The purpose decides everything. `reference/tailoring.md` has the questions, the layers and how to find each CLI's
mechanisms; this is the order.

1. **Find out the purpose.** From the request, then by asking (one message, numbered, with proposed defaults): what
   the agent is for and what a typical request to it looks like; who calls it (people in the chat, an application
   over the API, both) and what shape of answer they need (prose, a file, structured JSON); which CLI, or whether you
   should recommend one given this session's provider; what it must reach (repositories, internal services, MCP
   servers, package registries) and what it must never do (touch paths, run commands, reach hosts, change things);
   the model; what the operator should be able to change without editing the package. Then send the plan and ask
   for a go.
2. **Get the base.** A tailored package is a vanilla package plus the organization. If a vanilla package for that CLI
   is already in this workspace, start from it; otherwise build one first (the six steps above), as its own folder,
   so the organization keeps a clean base to tailor again later. Copy it to `/workspace/<purpose-name>/` and change
   `name` and `description`; the version stays `<CLI version>-1`.
3. **Give it the purpose: `seeds/home`.** The agent's standing instructions go in the CLI's own instruction file
   under `$HOME` (where the CLI reads it: its docs say; `reference/tailoring.md` has the common ones), stated as what
   it does, how it answers, and what it refuses. Skills, subagents, commands the purpose needs go in the CLI's own
   places under `$HOME` too. The request's `instructions` still append per call: do not put per-task guidance in
   the seeds.
4. **Fence it from inside: the CLI's permission mechanism.** Find how this CLI allows and denies tools, restricts
   commands and paths, and runs hooks before a tool call, and encode least privilege there in `seeds/home`: the tools
   the purpose needs allowed, everything else denied; commands and paths restricted to the work; a hook that refuses
   what the rules cannot express. Switch off what the purpose does not need (web browsing, a tool the CLI offers
   that nobody asked for). Say in the README what the agent can and cannot do, as a table.
5. **Its tools: the `mcp` catalog.** Each MCP server the purpose needs goes in the manifest with `allowed_tools`
   narrowed to the tools used; its credential is a declared `secret` bound to that server's host and referenced as
   `${NAME}` in the header. The value is never in the package: the organization sets it in the console.
6. **Its knobs: `settings` and `secrets`.** Anything the operator may want to change without editing the package
   (a base URL, a project key, a default repository, a log level, a token) is a declared setting or secret; `run`
   passes it to the CLI from the environment. Give each a description and a default where one is safe.
7. **Egress and limits.** `run.egress` holds only what the purpose needs beyond the provider and the catalog's hosts
   (which are implied); deny entries fence subdomains the person named. Set `run.timeout_s` and `stop_grace_s` to
   what a task of this kind takes. `defaults.model` is the model agreed; `provider.apis` only the shape used.
8. **Prove it.** Run the tailored package here against this session's provider: a realistic task, and then a
   forbidden one (the path it may not touch, the host it may not reach, the tool it may not use) to show the refusal.
   Record both as fixtures; validate the output; write the tests. Report the forbidden run's outcome in the README.
9. **Hand back** with the plan as built, the table of what it may and may not do, the settings and secrets the
   operator must set before the first turn, and what you could not try here. The person installs it from the
   console, sets the secrets, and a turn on it is its real test.

## Rules the platform enforces

- Every stdout line that is JSON with `"type": "response.…"` must be a valid event of that type: the OpenAI SDK's
  model for it, exactly. `hgkit.check` is that same check.
- Exactly one terminal event, last. Exiting without one fails the turn.
- The package never prints `HG_PROVIDER_TOKEN` or a secret's value, and never writes a key into a file. On a CLI or
  provider error, `response.failed` with a clear message, never a silent exit.
- `harness.json` refuses unknown fields; `name` is `[a-z0-9][a-z0-9._-]{0,62}`; egress entries are `host`,
  `*.domain` or `*`, with an optional `:port`; settings and secrets are named like environment variables, not
  starting `HG_`.
- The package a turn runs has only the egress the manifest declares: the full network you have while building is
  yours, not the harness's.
- Narrowing is safe; widening is escalation. A tailored package never grants more than the vanilla CLI has, and
  never more than the purpose needs.
