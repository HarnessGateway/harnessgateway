# gemini-cli

Google's [Gemini CLI](https://github.com/google-gemini/gemini-cli) behind the OpenAI Responses API. Each turn runs
`gemini` once, headless, in the session's workspace with every tool auto-approved (the gVisor sandbox is the boundary),
and maps its `stream-json` output onto Responses stream events.

- **CLI:** `@google/gemini-cli` **0.65.0**, pinned in `install.sh` (npm, egress `registry.npmjs.org`,
  `--ignore-scripts`: no dependency needs an install script; npm adds the prebuilt node-pty where one is published for
  the machine, and the shell tool falls back to plain child processes without it). `install.sh` fails unless
  `gemini --version` prints 0.65.0.
- **Provider:** the Gemini API only (`provider.apis: ["google"]`); partly verified, since no Google key was
  available (see Tests). The CLI has no OpenAI- or Anthropic-shaped mode
  (0.65.0 reads no `OPENAI_*`/`ANTHROPIC_*` variable; its only model API is `@google/genai`), so it runs on an
  organization provider registered with `"api": "google"`. Call it as `gemini-cli/<provider>/<model>`; the default
  model is `gemini-3.8-flash`, Google's current Flash model for coding and the newest one Gemini CLI 0.65.0 knows
  (Gemini 2.5 is limited access; the newer `gemini-4-argon` is not in 0.65.0's model configs).
- **Run egress:** none of its own; the organization's egress applies. Auto-update, usage statistics (Clearcut) and
  telemetry are off.
- **Seeds:** none. An organization's overlay can seed `seeds/home/.gemini/settings.json` (its settings, MCP servers)
  and instruction files such as `~/.gemini/GEMINI.md`; `map.py` puts the turn's settings on top of that seed.

## Files

| File | Role |
|---|---|
| `harness.json` | manifest (contract 1); persists `.gemini/tmp` (session transcripts, per project) and `.gemini/projects.json` (the workspace's project name) |
| `install.sh` | installs the pinned CLI into `$HG_TOOLS` |
| `run.sh` | `exec python3 "$HG_PACKAGE_DIR/map.py"` (the platform starts it in the workspace) |
| `map.py` | places input files, writes the turn's settings and instruction file, builds the command line and environment, runs the CLI, maps its stream (hgkit `Emitter`) |
| `tests/` | recorded CLI streams (`fixtures/*.jsonl`), a recorded provider stream, and `test_map.py` |

## The command

```
gemini -o stream-json -m $HG_MODEL --approval-mode yolo --skip-trust [-r <session id>]      (prompt on stdin)
```

`--skip-trust` trusts the workspace for the run (a headless run cannot answer the folder-trust prompt); the prompt goes
on stdin. Environment: `GOOGLE_GEMINI_BASE_URL=$HG_PROVIDER_URL` (the platform's relay, `http://hg-door:8080`, already
in `NO_PROXY`), `GEMINI_API_KEY=$HG_PROVIDER_TOKEN` (the CLI sends it as `x-goog-api-key`, which the gateway swaps for
the real key), `NO_COLOR=1`. `GOOGLE_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_*`, `GOOGLE_GENAI_USE_*`
and `CLOUD_SHELL` are removed so no other auth path is taken.

Each turn writes `~/.gemini/settings.json`: the composed package's seed (`$HG_PACKAGE_DIR/seeds/home/.gemini/settings.json`,
if an overlay adds one) with this on top. (The CLI ignores a system settings file whose directory is not owned by root, which a
sandbox cannot provide, so the user file is the one used; `~/.gemini/settings.json` is not persisted.)

- `security.auth.selectedType: gemini-api-key` (without it a headless run exits "Invalid auth method selected")
- `general.enableAutoUpdate`, `general.enableAutoUpdateNotification`, `privacy.usageStatisticsEnabled`,
  `telemetry.enabled`: false
- `context.fileName: ["GEMINI.md", "AGENTS.md", "HG_INSTRUCTIONS.md"]`
- `modelConfigs.customAliases`: every helper alias that names a model of its own in 0.65.0's `DEFAULT_MODEL_CONFIGS`
  (`classifier`, `prompt-completion`, `fast-ack-helper`, `edit-corrector`, `summarizer-*`, the `gemini-*-flash-base`
  bases behind `web-search`/`web-fetch`/`loop-detection`/`llm-edit-fixer`/`next-speaker-checker`/`context-snapshotter`,
  `loop-detection-double-check`, `chat-compression-*`, `agent-history-provider-summarizer`) is redefined with the
  turn's model and its parent alias, so the CLI never asks the provider for a model the request did not name. 0.65.0
  sends `-m` unchanged (checked for `gemini-2.5-flash`, `gemini-3.8-flash`, `gemini-3.1-pro-preview`,
  `gemini-flash-latest`)
- `modelConfigs.customOverrides` for the turn's model (effort, output limit), `model.maxSessionTurns`, the turn's
  `mcpServers` (below), added to the seed's

## Request fields

| Request field | Where it goes |
|---|---|
| `input` (string, or the `input_text` parts of user messages) | the prompt, on stdin |
| `input_file` / `input_image` by `file_id` or inline | written into the workspace by name (hgkit `place_inputs`; a name taken by other bytes gets a numeric suffix), and appended to the prompt as `@name` references, which the CLI inlines |
| `instructions`, and `system`/`developer` messages in `input` | `~/.gemini/HG_INSTRUCTIONS.md`, a user-level context file the CLI appends to its standing system instruction (checked against the request the provider received); rewritten or removed every turn, so it applies to the turn it came with. The CLI has no system-prompt flag |
| `text.format` `json_schema` / `json_object` | an instruction appended to the same file (the CLI has no structured-output option) |
| `previous_response_id` | `-r <id>`: the CLI's session id is kept in `$HG_STATE/gemini_session` and its transcript under `$HOME/.gemini/tmp/<project>/chats` (persisted). If the transcript is gone, or the CLI refuses it (`Error resuming session`: a session whose turns never got a model reply has nothing to resume), the turn starts a new CLI session |
| `reasoning.effort` | `thinkingConfig` for the model: Gemini 2.5 `thinkingBudget` (`none` 0, `minimal` 512, `low` 2048, `medium` 8192, `high` 24576, `xhigh` 32768), later models `thinkingLevel` (`none`/`minimal` → `MINIMAL`, `low`, `medium`, `high`/`xhigh` → `HIGH`) |
| `max_output_tokens` | `generateContentConfig.maxOutputTokens` for the model |
| `max_tool_calls` | `model.maxSessionTurns` (the CLI counts model turns, so parallel tool calls in one turn count once) |
| `tools` of type `mcp` | resolved by the platform with the package's catalog (none) into `$HG_MCP`; each server becomes `mcpServers.<label>` (the label with anything but letters, digits and `-` replaced by `-`, since the CLI names tools `mcp_<server>_<tool>` and splits at the first `_`): `httpUrl` (or `url` for an SSE server), `trust: true`, `headers` (placeholders the door replaces), `includeTools` from `allowed_tools` (an empty list leaves the server out) |
| `model` | `harness/provider/model`; `$HG_MODEL` is passed with `-m` |

**Ignored**, and named in `metadata.hg_ignored_fields` when the request sets them: `tools` of other types,
`tool_choice`, `parallel_tool_calls`, `temperature`, `top_p`, `top_logprobs`, `truncation`, `include`,
`prompt_cache_key`, `service_tier`, `reasoning.summary`, `text.verbosity`. Also not acted on: the assistant/tool items
of an `input` list (the resumed CLI session supplies the history). `metadata`, `store`, `background`, `stream` and MCP
`require_approval` are the platform's.

## Output mapping

| Gemini CLI stream-json | Responses |
|---|---|
| `init` | session id → `$HG_STATE/gemini_session`; `metadata.gemini_session`, `gemini_model` |
| `message` (assistant, `delta: true`) | `response.output_text.delta` in an assistant `message` (one delta per streamed chunk) |
| `message` (user) | not shown (it is the prompt) |
| `tool_use` `run_shell_command` + its `tool_result` | `shell_call` + `shell_call_output`: stdout is the CLI's display of the output (stdout and stderr together); the stream does not carry the exit code, so a run the CLI reports as a success is `exit_code` 0 and a failed one 1 with the error as stderr |
| `tool_use` `write_file` + result | `apply_patch_call` `create_file` (the content as `+` lines; also for an overwrite) + `apply_patch_call_output` |
| `tool_use` `replace` + result | `apply_patch_call` `update_file` (`-old_string` / `+new_string`) + output |
| `tool_use` `mcp_<server>_<tool>` + result | `mcp_call`: added `in_progress` at the call, done `completed` with its output or `failed` with an `mcp_tool_execution_error`; the server label maps back to the request's |
| any other `tool_use` (`read_file` and the CLI's other built-in tools) + result | `function_call` + `function_call_output` (`incomplete` with the error if the tool failed) |
| `error` `severity: warning` | the turn's log |
| `error` `severity: error` | kept for the failure message |
| `result` `stats` | `usage`: `input_tokens` (cached included), `cached_tokens` = `cached`; thinking tokens (`total_tokens` − input − output) are added to `output_tokens` and reported as `reasoning_tokens`. The stats are the run's own, also after a resume |
| `result` `status: success` | `response.completed` (with `metadata.hg_ignored_fields` when the request set fields the CLI ignores) |
| `result` `FatalTurnLimitedError` | `response.incomplete`, `max_messages`, with the work so far and `metadata.gemini_stop` |
| `result` `status: error` otherwise | `response.failed`: `provider_error` for `[API Error: ...]` with the provider's own message unwrapped (`rate_limit_exceeded` for 429 / `RESOURCE_EXHAUSTED`), else the CLI's error type |
| no `result` (the CLI was stopped or crashed) | `response.failed` `cli_error` with the exit code and the CLI's errors or stderr tail |

Every raw CLI line is also printed to the turn's log, prefixed `gemini-stream ` (log lines reach the trace, never the
client).

On SIGTERM (cancel, timeout) the CLI exits but leaves a running shell command behind (each runs in a session of its
own), so `map.py` tracks every process below it while the turn runs and stops them all (TERM, then KILL after 2 s)
before it exits. Checked in a sandbox container: no `sleep 300` survives the turn's process group being signalled.

## Known limits

- No reasoning items: the CLI's stream-json carries no thinking text (only the token count, in `usage`).
- Text streams per provider chunk, not per token.
- The shell's exit code is not in the stream (see above).
- **MCP servers need egress.** The package has no catalog and no `run.egress`, so a request can add only a server whose
  host the organization's egress already allows (the platform refuses others with `mcp_server_not_allowed`); an
  overlay can add a catalog server, whose host then joins the egress.

## Tests

There is no Google API key on the verifying machine, so this package is **partly verified**:

- **Platform (this revision, on a provider registered as `{"api": "google", "base_url":
  "https://generativelanguage.googleapis.com", "key": "invalid-test-key"}`, model `gemini-2.5-flash`):** install runs
  the pinned npm install in an install box; a turn's provider trace shows the CLI's `POST
  /v1beta/models/gemini-2.5-flash:streamGenerateContent` reaching `generativelanguage.googleapis.com` through the
  door's relay and the gateway with the swapped key, and Google's `400 API key not valid` ends the turn as a clean
  `failed` (`hg_error_code: provider_error`, the provider's message unwrapped), with `temperature` named in
  `metadata.hg_ignored_fields`. A request `mcp` tool on a host the organization allows was written into the settings
  and the door injected the caller's token into the CLI's MCP request (intercepted connection, accepted by the
  server). A second turn with `previous_response_id` found the session id and transcript restored, was refused by
  the CLI (`Error resuming session`: the first turn had no model reply) and started a new CLI session. Everything that
  needs a working model (text, tools, resume of a real conversation, streaming deltas) is not verified on the platform.
- **Sandbox container (the previous revision; the sandbox image, the pinned CLI, `map.py`):** against a scripted
  Gemini API server and a minimal MCP server: write + shell + streamed text, resume by session id (the history reached
  the model), `replace` + a failing command, MCP with a bearer token, the turn limit, an API error, effort and
  output-limit overrides on the wire (`thinkingBudget` for 2.5, `thinkingLevel` for 3.x), instructions in the system
  instruction, and cancel. `provider_gemini_hello.sse` is the scripted server's Gemini API stream from that check.

```
.venv/bin/pytest -q packages/gemini-cli
```

`mock_*.jsonl` are the real CLI's streams against the scripted servers; `platform_api_key_invalid.jsonl` is a platform
turn against Google with an invalid key. Each is fed through `Mapper` and its output is validated with the platform's
`check_output`; the tests also check the settings (MCP servers from an `$HG_MCP` file), instructions (with system and
developer messages), ignored fields, command line and environment built from a request, and run `map.py` end to end
against a fake `gemini` (input files placed, a session the CLI refuses to resume).
