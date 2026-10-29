# pi

[Pi](https://github.com/earendil-works/pi), the minimal coding agent (`read`, `bash`, `edit` and `write` tools),
behind the OpenAI Responses API. Each turn writes Pi's provider and MCP configuration, runs `pi -p --mode json` once in
the session's workspace and maps its JSON events onto Responses stream events.

- **CLI:** `@earendil-works/pi-coding-agent` **1.4.0**, pinned in `install.sh`, installed by the npm route Pi's README
  documents (`npm install -g --ignore-scripts @earendil-works/pi-coding-agent`; egress `registry.npmjs.org`). Pi's
  curl installer cannot pin a version, so the npm route is the one used. `install.sh` fails if `pi --version` is not
  1.4.0.
- **Provider:** Anthropic Messages or OpenAI-shaped endpoints (`provider.apis: ["anthropic", "openai"]`). Verified with
  DeepSeek's Anthropic-compatible and Chat Completions endpoints (`deepseek-flash`) and with OpenAI over the
  Responses API (`gpt-5.4-mini`).
- **Run egress:** none of its own; the organization's egress and the harness's MCP catalog hosts apply. Pi runs
  offline: no model-catalog refresh, version check or telemetry.

## Files

| File | Role |
|---|---|
| `harness.json` | manifest (contract 1) |
| `install.sh` | installs the pinned CLI into `$HG_TOOLS` |
| `run.sh` | `exec python3 "$HG_PACKAGE_DIR/map.py"` (the platform starts it in the workspace) |
| `map.py` | places the request's files, writes Pi's provider and MCP config (from `$HG_MCP`), builds the command line from the request, runs Pi, maps its stream (hgkit `Emitter`) |
| `tests/` | recorded Pi streams from real turns (`fixtures/*.jsonl`) and `test_map.py` |

## The command

```
pi -p --mode json --provider hg --model $HG_MODEL --approve
   [--session-id $HG_SESSION | --no-session] [--append-system-prompt TEXT] [--thinking LEVEL] [--no-mcp]
   -- [@<placed file> ...] PROMPT
```

Each turn writes `$HOME/.pi/agent/models.json` (only `.pi/agent/sessions` persists, so it is rewritten every turn)
with one custom provider, `hg`:

- `$HG_PROVIDER_API=anthropic`: Pi api `anthropic-messages`, `baseUrl=$HG_PROVIDER_URL` (the box's door,
  `http://hg-door:8080`, which relays to the gateway).
- `$HG_PROVIDER_API=openai`: `baseUrl=$HG_PROVIDER_URL/v1`, Pi api `openai-responses` or `openai-completions`, the
  one Pi's own model catalog uses for that model id (`gpt-5.4-mini`: responses; `deepseek-flash`: completions), else
  `openai-completions`. The harness setting `PI_OPENAI_API` (an environment variable, like every setting) forces one.
- `apiKey: "$HG_PROVIDER_TOKEN"`: Pi's own environment interpolation, so the per-turn token is never written to disk.
- The model entry borrows context window, output limit, reasoning support and cost from Pi's catalog
  (`pi-ai/dist/providers/data/*.json`), with compat flags and the thinking-level map only when the catalog entry uses
  the same wire API. DeepSeek models over chat completions get DeepSeek's compat (thinking format, `reasoning_content`
  replay) even when they are not in the catalog.

Environment: `PI_OFFLINE=1`, `PI_SKIP_VERSION_CHECK=1`, `PI_TELEMETRY=0` (all documented in 1.4.0's
`docs/environment-variables.md`) and `NO_COLOR=1`. Pi has no permission prompts; `--approve` trusts project-local
`.pi/` resources (the sandbox is the boundary).

`openai` with `PI_OPENAI_API=openai-completions` fails cleanly for `gpt-5.4-mini`: OpenAI rejects function tools with
`reasoning_effort` on chat completions, so use the default (responses).

## Request fields

| Request field | Where it goes |
|---|---|
| `input` (string, or the `input_text` parts of user messages) | the prompt (after `--`, so a leading `-` is safe) |
| `input_file` / `input_image` (by `file_id`, or inline `file_data` / data: URL) | written into the workspace by name (hgkit `place_inputs`) and passed as `@<name>` (text inlined, images attached) |
| `instructions`, and `system` / `developer` messages in `input` | `--append-system-prompt`: appended to Pi's own system prompt (Pi also reads `AGENTS.md` / `CLAUDE.md` in the workspace) |
| `previous_response_id` | `--session-id $HG_SESSION`: Pi opens (or creates) the session named after the platform's session, stored in `$HOME/.pi/agent/sessions/`, which the manifest persists |
| `store: false` | `--no-session` |
| `reasoning.effort` | `--thinking` (`none`→`off`, `minimal`, `low`, `medium`, `high`, `xhigh`) |
| `max_output_tokens` | the model's `maxTokens` in models.json |
| `text.format` json_schema / json_object | an instruction appended to the system prompt (Pi has no structured-output flag) |
| `tools` of type `mcp` | the platform resolves them with the harness's catalog into `$HG_MCP` (SPEC §3.3); `map.py` reads only that file. Each server becomes an entry of `$HOME/.pi/agent/mcp.json` with `exposure: direct`; header values (the platform's placeholders, which the door replaces) go through environment variables (`${HG_MCP_i_j}`), so Pi never interprets them. Rewritten every turn; `--no-mcp` when there are none. An `sse` server is left out (Pi speaks streamable HTTP only) |
| `allowed_tools` (of a turn's MCP server) | the server's `exposure: hidden` with `toolExposure: {tool: direct}` for each allowed tool |
| `model` | `--provider hg --model $HG_MODEL` |

**Ignored**, and named per turn in `metadata.hg_ignored_fields` when the request sets them: `tools` other than `mcp`,
`tool_choice`, `parallel_tool_calls`, `temperature`, `top_p`, `top_logprobs`, `max_tool_calls`, `truncation`,
`include`, `prompt_cache_key`, `service_tier`, `text.verbosity` and `reasoning.summary`. `metadata`, `background`,
`stream` and MCP `require_approval` are the platform's.

## Output mapping

| Pi `--mode json` event | Responses |
|---|---|
| `message_update` `text_delta` | `response.output_text.delta`, token by token (hgkit `Emitter.text`) |
| `thinking_delta` / `thinking_end` | a `reasoning` item, emitted before the text or tool call that follows it |
| `tool_execution_start` `bash` | `shell_call` (command, `timeout_ms`) at start; `shell_call_output` at end with stdout and `exit_code` from Pi's `structuredContent` (`timeout` outcome on a tool timeout) |
| `edit` | `apply_patch_call` `update_file` with Pi's unified patch + `apply_patch_call_output` |
| `write` | `apply_patch_call` `create_file` (content as `+` lines) + output |
| `mcp__<server>__<tool>` | `mcp_call` (the turn's server label, name, arguments, output or an `mcp_tool_execution_error`) |
| any other tool (`read`, `grep`, `find`, `ls`, extension tools) | `function_call` at start + `function_call_output` at end |
| assistant `message_end` usage | summed over every assistant message of the turn → `usage` on the terminal event (`input_tokens` = input + cacheRead + cacheWrite; cached, cache-write and reasoning details) |
| `stopReason: error`, `auto_retry_end` failure | `response.failed`, `provider_error` (or `rate_limit_exceeded`) with Pi's message |
| `stopReason: length` | `response.incomplete` (`max_output_tokens`) |
| Pi exits non-zero with no assistant message | `response.failed`, `harness_error` with Pi's stderr tail |
| otherwise | `response.completed`, `metadata.pi_session` (and `metadata.hg_ignored_fields`) |

The harness setting `PI_RAW_EVENTS=1` copies Pi's raw stream to the turn's stderr as `PI_RAW <json>` lines (how
`tests/fixtures` were recorded); it never reaches client-visible events.

## Known limits

- Stop/cancel: the platform signals the turn's process group (`map.py` and Pi) and then stops every process left in
  the box, including the commands Pi's bash tool runs in sessions of their own.
- MCP servers need egress: the harness's catalog hosts are in it; a request-added server is refused by the platform
  unless the organization allows its host.
- Thinking text depends on the provider: DeepSeek's Anthropic endpoint often returns empty thinking blocks on tool
  turns (no reasoning item then); OpenAI returns reasoning summaries only sometimes.
- Tool calls are reported when Pi runs them, so output items arrive per tool, not as the model streams arguments.

## Tests

Recorded `--mode json` streams from real turns through the platform (`PI_RAW_EVENTS=1` copies the raw stream to the
turn's stderr): `deepseek_anthropic_*` (hello, edit with a failing command, French instructions while streaming, an MCP
call, an unknown model, a cancelled `sleep`), `deepseek_openai_recall_turn2` and `openai_responses_hello`.
`test_map.py` replays each through `map.Mapper` and validates the output with the platform's own reader,
`harnessgateway.domain.contract.check_output`. It also checks the command line, models.json, the MCP config built from
`$HG_MCP`, the ignored fields, and runs `map.py` end to end against a fake `pi` (instructions and system/developer
messages, placed files).

```
.venv/bin/pytest -q packages/pi
```
