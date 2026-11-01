---
name: harness-package-builder
description: Build a HarnessGateway harness package for any coding-agent CLI from its name alone ("Create a harness package for <name>"). Use whenever someone asks for a harness, a package, or to add a CLI to the platform. Covers researching the CLI, the contract (OpenAI Responses in, Responses stream events out), harness.json, install.sh, run.sh, map.py with hgkit, recording real CLI output, and checking the mapper offline.
---

# Harness package builder

You write **harness packages**: the unit HarnessGateway runs. The platform knows no CLI; a package teaches it one. The
request may be nothing more than a name: `Create a harness package for codex`. Everything else you find out. You are
in a sandbox on the platform with full outbound network access, so you can read the project's documentation, install
the CLI, run it, record its output, and write and check the package here.

Two documents are the ground truth; read both before writing anything:

- `/workspace/docs/packages.md`: the platform's guide to packages (the manifest, the turn environment, hgkit,
  secrets, persistence).
- `/workspace/template/`: the `shell` package, the template every package starts from, with a README that says what
  to change in each file.

`reference/events.md` is the cookbook of events, `reference/request-fields.md` says where each request field usually
goes, `scripts/check_events.py` checks a mapper's stdout offline. Do not invent platform features: what is not in the
guide does not exist.

## The contract, which is fixed

```
client ──(Responses request)──▶ platform ──(the same JSON, at $HG_REQUEST)──▶ run.sh → map.py ──▶ the CLI
client ◀──(Responses response)── platform ◀──(Responses stream events, one JSON object per stdout line)──┘
```

- **In:** the client's `POST /v1/responses` body, whole, at `$HG_REQUEST` and on stdin. The request is the task:
  `input` text and files, `instructions`, `reasoning.effort`, `text.format`, MCP servers, `max_tool_calls`.
- **Out:** Responses stream events on stdout, one JSON object per line; any other line is a log. Exactly one terminal
  event (`response.completed`, `response.incomplete` or `response.failed`) ends the turn, last.
- **The package is the agent:** the CLI and its version, how it is run, what it persists, what it may reach.
- **A turn sees** `HG_REQUEST`, `HG_FILES`, `HG_MCP`, `HG_WORKSPACE` (the cwd), `HG_STATE`, `HOME`, `HG_PACKAGE_DIR`,
  `HG_TOOLS` (read-only at run time; `PATH` includes `$HG_TOOLS/bin`), `HG_SESSION`, `HG_RESPONSE`, `HG_TURN`,
  `HG_MODEL`, `HG_PROVIDER_API`, `HG_PROVIDER_URL`, `HG_PROVIDER_TOKEN`, each declared setting and secret as a
  variable. The root filesystem is read-only, the user is uid 10001, `/tmp` does not persist, `$HOME` persists only
  where `persist` says, the workspace and `$HG_STATE` always do.

## What you deliver, which is fixed

A folder `/workspace/<name>/` with exactly this layout (copy the template, then edit):

| File | What you write |
|---|---|
| `harness.json` | `name`; `version` as `<CLI version>-1`; a one-paragraph `description`; `provider.apis`, the API shapes the CLI speaks (`anthropic`, `openai`, `google`; omit for no model); `defaults.model`; `install.egress`, the hosts `install.sh` needs, and `install.timeout_s`; `run.egress`, the hosts a turn needs besides the provider (usually `[]`); `persist`, the CLI's session store under `$HOME`; `settings`, `secrets`, `mcp` only when the CLI needs them |
| `install.sh` | the vendor's documented Linux install, pinned to the version you studied, into `$HG_TOOLS`. End by running the CLI's version command and printing `installed: …` |
| `run.sh` | leave it: `exec python3 "$HG_PACKAGE_DIR/map.py"` |
| `map.py` | `Request.load()` → the CLI's command and environment; run it; its output → events with `Emitter`; `State` for the CLI's session id; one terminal event |
| `README.md` | Files; The command; Request fields (a table: field → where it goes, and the ignored ones); Output mapping (CLI output → items); Known limits; Tests |
| `tests/` | the recorded CLI output as fixtures and `test_map.py` that runs the mapper on them |

No `seeds/` unless the CLI cannot run headless without a config file; then the minimum, and nothing an organization
would want to own (no persona, no instructions, no MCP catalog). Delete the template's `seeds/workspace/README.md`.

## How to work

1. **Research the CLI first.** Find its official home (the project's repository, documentation site and package
   registry page) and read, in this order: how it is installed on Linux, its latest stable release, its headless or
   non-interactive mode, its machine-readable output (JSON lines, a stream-json flag, a JSON-RPC or ACP mode), how a
   session is resumed by id, how it is pointed at a custom base URL and API key and which API shapes it speaks, how
   prompts, telemetry and update checks are disabled, where it reads instruction files, config and MCP servers. Fetch
   pages and clone repositories as you need; nothing outbound is blocked for you. Write what you found as a table,
   with the version you will pin, before touching code. Your knowledge of the CLI may be out of date: what the
   current docs and `--help` say wins.
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
4. **Write `map.py` with hgkit** (importable here: `python3 -c "import hgkit, inspect; print(inspect.getsource(hgkit))"`
   shows the whole library). Build the command from the request: the prompt, the `instructions` appended to the
   CLI's own system prompt, `$HG_MODEL`, the base URL and token, resume from the id saved in `State`. Map each output
   line: assistant text → `out.text`, thinking → `out.reasoning`, a shell run → `out.shell`, a file write or edit →
   `out.patch`, an MCP tool → `out.mcp`, anything else → `out.function`; then
   `out.completed(usage=Emitter.usage(input_tokens, output_tokens))`, or `out.failed(code, message)` on an error.
   Report request fields the CLI cannot honour with `out.ignored([...])`.
5. **Check the mapper offline** on the fixtures: `python3 ~/.claude/skills/harness-package-builder/scripts/check_events.py
   <stdout of your mapper>` refuses a line that is not an event, a text delta without `logprobs`, a missing or second
   terminal event, usage without its details. Fix until it passes, then put the same runs in `tests/test_map.py`.
   Run `install.sh` once end to end into a scratch `$HG_TOOLS` to prove it.
6. **Hand back** the folder and a short note: the CLI version pinned, the hosts in `install.egress` and why, what maps
   to what, what you could and could not try here. The person installs it from the console (New harness from
   workspace, on this session's page) and a turn on it is its real test; if that fails, they will bring you the
   trace, and you fix `map.py`.

## Rules the platform enforces

- Every stdout line that is JSON with `"type": "response.…"` must be a valid event of that type; text deltas carry
  `"logprobs": []`; usage carries `input_tokens_details.cached_tokens` and `output_tokens_details.reasoning_tokens`.
- Exactly one terminal event, last. Exiting without one fails the turn.
- The package never prints `HG_PROVIDER_TOKEN` or a secret's value, and never writes a key into a file. On a CLI or
  provider error, `response.failed` with a clear message, never a silent exit.
- `harness.json` refuses unknown fields; `name` is `[a-z0-9][a-z0-9._-]{0,62}`; egress entries are `host`,
  `*.domain` or `*`, with an optional `:port`.
- The package a turn runs has only the egress the manifest declares: the full network you have while building is
  yours, not the harness's.
