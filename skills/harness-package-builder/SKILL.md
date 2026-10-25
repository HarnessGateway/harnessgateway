---
name: harness-package-builder
description: Build a HarnessGateway harness package that runs any coding-agent CLI (Claude Code, Codex, Hermes, Pi, Aider, an in-house agent...) on the HarnessGateway platform. Use when an organization wants to bring its own harness, customize a built-in one, or publish one to the open marketplace. Covers the contract (OpenAI Responses in, OpenAI Responses out), harness.json, install.sh, request.sh, the output mapper, seeding the workspace and home with instruction files, skills and config, local validation, and verifying on the platform.
---

# HarnessGateway Harness Package Builder

You are helping someone build a **harness package**: the unit HarnessGateway runs. Read this file fully, then the
references it points to when you reach that step. Everything an author needs is here; do not invent platform features.

## The one idea

HarnessGateway speaks **one protocol: the OpenAI Responses API**, and has no schema of its own.

```
client ──(Responses request)──▶ platform ──(the same JSON, verbatim)──▶ your package ──▶ the CLI
client ◀──(Responses response)── platform ◀──(Responses stream events)── your package ◀── the CLI
```

- **In:** the client's `POST /v1/responses` body is handed to the package **whole** at `$HG_REQUEST`. The package
  decides which fields its CLI can use (input text and files, `instructions`, `reasoning.effort`, `text.format`, MCP
  `tools`, `max_tool_calls`, `metadata`, ...) and how: flags, config files, environment, instruction files.
- **Out:** the package prints **Responses stream events** on stdout, one JSON object per line. The platform validates
  each against the OpenAI SDK's own models, numbers them, streams them to the client and builds the final `Response`.
- **Around:** the package **seeds** the workspace and `$HOME` (instruction files like `AGENTS.md`/`CLAUDE.md`, skills,
  MCP server lists, CLI settings). That is how a general agent becomes an organization's own agent.
- **Where:** every turn runs in a **gVisor sandbox** the platform owns: one per session, warm between turns, with the
  workspace, `$HOME` and `$HG_STATE` persisted across turns. The platform holds the provider key; the sandbox gets a
  per-turn token. Outbound traffic only reaches hosts the package allows.

The package is the organization's operational IP: the platform never knows what the CLI does.

## The package

```
my-harness/
├── harness.json        manifest: name, version, provider shapes, egress, timeouts, settings      (required)
├── install.sh          installs the CLI into $HG_TOOLS, pinned, self-contained                 (required)
├── request.sh          one turn: read $HG_REQUEST, run the CLI, print Responses events          (required)
├── map.py              your mapper: CLI output → Responses events (any language; hgkit helps)
├── seeds/workspace/    copied into a new session's /workspace        (optional)
├── seeds/home/         copied into a new session's $HOME             (optional)
├── tests/              recorded CLI output + an offline test of the mapper
└── README.md           what maps to what, providers/models verified, CLI version, limits
```

Full specification: `reference/contract.md`. Read it before writing `harness.json`.

## Workflow

Follow these steps in order. Do not skip verification.

1. **Study the CLI** (never from memory): install the exact version you will pin and read its `--help` for headless
   mode. You need: a non-interactive one-shot command; a machine-readable output stream (JSON lines are ideal: look for
   `--output-format stream-json`, `--json`, `--mode json`, an ACP/JSON-RPC mode); how to resume a session by id; how to
   point it at a custom base URL and API key; how to disable telemetry, update checks and permission prompts; where it
   reads instruction files, skills, MCP config and settings. Record each in a table (request field → CLI mechanism;
   CLI event → Responses item). `reference/request-fields.md` lists the usual mappings.
2. **Write `harness.json`** (`reference/contract.md` §Manifest). Pick `provider.apis` from the wire formats the CLI can
   speak (`anthropic`: Messages API; `openai`: Chat Completions and/or Responses). Keep `run.egress` empty unless the CLI
   needs a host at run time; put registries in `install.egress`.
3. **Write `install.sh`**: install the pinned CLI into `$HG_TOOLS` (`npm install -g --prefix "$HG_TOOLS" pkg@1.2.3`,
   `python3 -m venv "$HG_TOOLS/venv" && .../pip install pkg==1.2.3`, or download a checksummed binary into
   `$HG_TOOLS/bin`). It must be self-contained (the tools layer is cached by its hash) and end by printing the version.
4. **Write `request.sh` and the mapper.** Configure the CLI from the request and the environment, run it, and turn its
   output into events. Use **hgkit** (in the sandbox image): `Request` reads the request; `Emitter` prints a correct
   event sequence; `State` keeps the CLI's session id for resume. See `reference/events.md` for the event cookbook and
   `examples/minimal/` for a complete package.
5. **Seed** what the agent should find (`reference/seeding.md`): `seeds/workspace/AGENTS.md`, skills, MCP config,
   `seeds/home/.<cli>/settings`. Per-request instructions go through the request's `instructions` field instead.
6. **Validate locally**: `python3 scripts/validate.py ./my-harness` checks the manifest, the scripts and (with
   `--fixture`) replays recorded CLI output through your mapper, validating every event with the OpenAI SDK.
7. **Install and verify on the platform** (`reference/verify.md`): upload the package, register a provider, and run the
   checks: a file-creating first turn, a second turn that recalls the first (resume), streaming, `instructions`, usage,
   cancel, and a provider error. Fix until all pass.
8. **Share** (optional): push the package directory to a public git repository. Any organization installs it with
   `POST /v1/harnesses {"git": {"url": "...", "ref": "<tag or commit>", "path": "<dir>"}}`. The marketplace is free;
   organizations pay only for the compute and storage their sessions use.

## Rules that fail a turn if broken

- Every stdout line that is JSON with `"type": "response.<...>"` must be a valid event of that type (the platform adds
  `sequence_number`). Text deltas need `logprobs: []`. Other lines are logs and never reach the client.
- Exactly **one terminal event**, last: `response.completed`, `response.incomplete` or `response.failed`. Exiting without
  one fails the turn (`hg_reason: no_terminal_event`).
- `usage` on the terminal event is a full Responses usage object (`Emitter.usage(...)` builds it).
- The package never prints the provider token or settings secrets.
- On a CLI or provider error, emit `response.failed` with a clear `error.message` instead of exiting silently.

## Sandbox facts to design around

- cwd `/workspace`; `HOME=/home/hg`; `$HG_STATE=/state`; all three persist across the session's turns. `/tmp` does not.
  The root filesystem is read-only; the user is uid 10001.
- Provider: `HG_PROVIDER_URL` (no `/v1`) with `HG_PROVIDER_TOKEN` as the API key; `HG_PROVIDER_API` says which shape the
  organization chose. Set `ANTHROPIC_BASE_URL=$HG_PROVIDER_URL` or `OPENAI_BASE_URL=$HG_PROVIDER_URL/v1`.
- `HG_MODEL` is the model the client asked for (or the default). Pin every model the CLI might call to it.
- No DNS and no network except the provider URL and `run.egress` hosts through the proxy (`HTTPS_PROXY` is set).
- The sandbox is the security boundary: run the CLI in its non-interactive, auto-approve mode.
- Organization settings declared in `harness.json` arrive in `$HG_SETTINGS` (JSON; hgkit `settings()`).
- Files the client sent by `file_id` are under `$HG_FILES` (`index.json`); inline files are in the request.

## When done

Hand back: the package files, the verification evidence (response ids and short outputs for each check), the pinned
CLI version, and the provider/model combinations verified.
