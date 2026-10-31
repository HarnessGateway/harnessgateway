---
name: harness-package-builder
description: Build a HarnessGateway harness package for any coding-agent CLI (Claude Code, Codex, Gemini CLI, Aider, OpenCode, Pi, an in-house agent). Use whenever someone asks for a harness, a package, or to "add <CLI> to the platform". Covers the contract (OpenAI Responses in, Responses stream events out), harness.json, install.sh, run.sh, map.py with hgkit, seeds, recording real CLI output, and checking the mapper offline.
---

# Harness package builder

You write **harness packages**: the unit HarnessGateway runs. The platform knows no CLI; a package teaches it one. You
are in a sandbox on that platform right now, so you can install the CLI, read its help, run it, record its output,
and write and check the package here. The platform's own guide is at `/workspace/docs/packages.md` and the template
every package starts from is at `/workspace/template/` (the `shell` package, with a README that says what to change in
each file). Read both before writing anything. Do not invent platform features: what is not in the guide does not
exist.

## The one idea

```
client ──(Responses request)──▶ platform ──(the same JSON, at $HG_REQUEST)──▶ run.sh → map.py ──▶ the CLI
client ◀──(Responses response)── platform ◀──(Responses stream events, one JSON object per stdout line)──┘
```

The request is the task (`input` text and files, `instructions`, `reasoning.effort`, `text.format`, MCP servers).
The package is the agent (the CLI and its version, how it is run, what it persists, what it may reach). A turn runs
`run.sh` in the session's workspace with the request on stdin and at `$HG_REQUEST`; everything printed on stdout that
is JSON with `"type": "response.…"` is an event, anything else is a log line; exactly one terminal event
(`response.completed`, `.incomplete` or `.failed`) ends the turn. `reference/events.md` is the cookbook;
`reference/request-fields.md` says where each request field usually goes.

## What you deliver

A folder `/workspace/<name>/` with exactly this layout (copy the template, then edit):

| File | What you write |
|---|---|
| `harness.json` | name, version `<CLI version>-1`, one-paragraph description, `provider.apis` (the API shapes the CLI speaks: `anthropic`, `openai`, `google`; omit for no model), `defaults.model`, `install.egress` (the registry and download hosts `install.sh` needs) and `install.timeout_s`, `run.egress` (usually `[]`), `persist` (the CLI's session store under `$HOME`), `settings`/`secrets`/`mcp` only if the CLI needs them |
| `install.sh` | the vendor's documented Linux install, pinned to the version you studied, into `$HG_TOOLS` (`npm install -g --prefix "$HG_TOOLS" pkg@x.y.z`; `python3 -m venv "$HG_TOOLS/venv" && "$HG_TOOLS/venv/bin/pip" install pkg==x.y.z`; or the official installer pointed at `$HG_TOOLS`). Never name a CPU architecture. End by running the CLI's `--version` and printing `installed: …` |
| `run.sh` | leave it: `exec python3 "$HG_PACKAGE_DIR/map.py"` |
| `map.py` | `Request.load()` → the CLI's command and environment; run it; its output → events with `Emitter`; `State` for the CLI's session id; one terminal event |
| `README.md` | Files; The command; Request fields (a table: field → where it goes, and the ignored ones); Output mapping (CLI output → items); Known limits; Tests |
| `tests/` | the recorded CLI output as a fixture and `test_map.py` that runs the mapper on it |

No `seeds/` unless the CLI cannot run headless without a config file; then the minimum, and nothing an organization
would want to own (no persona, no instructions, no MCP catalog). Delete the template's `seeds/workspace/README.md`.

## How to work

1. **Install the CLI here, the way its project documents**, into `/tmp/tools` (a venv or `npm --prefix`), and keep
   your scratch runs under `/tmp` too: the workspace is snapshotted after the turn, so only the package belongs in it.
   `/tmp` does not survive into a later turn, which is fine: a later turn reinstalls in a minute.
   Your sandbox reaches the package registries and GitHub; nothing else. Read `--help` for: a non-interactive
   one-shot mode; machine-readable output (JSON lines, `--output-format stream-json`, `--json`, a JSON-RPC mode);
   resuming a session by id; a custom base URL and API key; disabling prompts, telemetry and update checks; where it
   reads config. Record each in a table before writing code.
2. **Try it against this session's provider when you can.** Your environment has `HG_PROVIDER_URL` (a base URL without
   `/v1`), `HG_PROVIDER_TOKEN` (the API key to present), `HG_PROVIDER_API` (`anthropic`, `openai` or `google`) and
   `HG_MODEL`. A CLI that speaks that same shape can be run here for real: for `anthropic`, set
   `ANTHROPIC_BASE_URL=$HG_PROVIDER_URL` and `ANTHROPIC_API_KEY=$HG_PROVIDER_TOKEN`; for `openai`,
   `OPENAI_BASE_URL=$HG_PROVIDER_URL/v1` and `OPENAI_API_KEY=$HG_PROVIDER_TOKEN`; and pin the model to `$HG_MODEL`.
   Run a small task (create a file, run a command) and save the raw output as the fixture. A CLI that speaks another
   shape cannot be run here: write the mapping from its documented output format, say so in the README, and keep the
   fixture to what you could record (its help, its version).
3. **Write `map.py` with hgkit** (importable here: `python3 -c "import hgkit, inspect; print(inspect.getsource(hgkit))"`
   shows the whole library; it is in the sandbox image). Build the command from the request: the prompt, the
   `instructions` appended to the CLI's own system prompt, `$HG_MODEL`, the base URL and token, `--resume` from the
   id saved in `State`. Map each output line: assistant text → `out.text`, thinking → `out.reasoning`, a shell run →
   `out.shell`, a file write or edit → `out.patch`, an MCP tool → `out.mcp`, anything else → `out.function`; then
   `out.completed(usage=Emitter.usage(input_tokens, output_tokens))`, or `out.failed(code, message)` on an error.
   Report request fields the CLI cannot honour with `out.ignored([...])`.
4. **Check the mapper offline** on the fixture: `python3 ~/.claude/skills/harness-package-builder/scripts/check_events.py
   <stdout of your mapper>` refuses a line that is not an event, a text delta without `logprobs`, a missing or second
   terminal event, usage without its details. Fix until it passes, then put the same run in `tests/test_map.py`.
5. **Hand back** the folder and a short note: the CLI version pinned, what maps to what, what you could and could not
   try here. The person installs it from the console (New harness from this session's workspace) and a turn on it is
   its real test; if that fails, they will bring you the trace, and you fix `map.py`.

## Rules the platform enforces

- Every stdout line that is JSON with `"type": "response.…"` must be a valid event of that type; text deltas carry
  `"logprobs": []`; usage carries `input_tokens_details.cached_tokens` and `output_tokens_details.reasoning_tokens`.
- Exactly one terminal event, last. Exiting without one fails the turn.
- The package never prints `HG_PROVIDER_TOKEN` or a secret's value. On a CLI or provider error, `response.failed`
  with a clear message, never a silent exit.
- `harness.json` refuses unknown fields; `name` is `[a-z0-9][a-z0-9._-]{0,62}`.
- What a turn sees, exactly: `HG_REQUEST`, `HG_FILES`, `HG_MCP`, `HG_WORKSPACE`, `HG_STATE`, `HOME`, `HG_PACKAGE_DIR`,
  `HG_TOOLS` (read-only at run time; `PATH` includes `$HG_TOOLS/bin`), `HG_SESSION`, `HG_RESPONSE`, `HG_TURN`,
  `HG_MODEL`, `HG_PROVIDER_API`, `HG_PROVIDER_URL`, `HG_PROVIDER_TOKEN`, each declared setting and secret as a
  variable. The root filesystem is read-only, the user is uid 10001, `/tmp` does not persist, `$HOME` persists only
  where `persist` says, the workspace and `$HG_STATE` always do.
