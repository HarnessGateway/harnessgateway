# pi — Pi coding agent for HarnessGateway

[Pi](https://github.com/earendil-works/pi) (`@earendil-works/pi-coding-agent`, **pinned 1.4.0**), the minimal coding
agent with `read`, `bash`, `edit` and `write` tools, run headless as `pi -p --mode json` in the session's workspace.

Call it as `pi/<provider>/<model>`. Provider shapes: `anthropic` and `openai` (`provider.apis`).

## Files

| File | What it does |
|---|---|
| `harness.json` | manifest (contract 1); install egress `registry.npmjs.org`; run egress none |
| `install.sh` | `npm install --global --prefix $HG_TOOLS --ignore-scripts @earendil-works/pi-coding-agent@1.4.0` |
| `request.sh` | `exec python3 map.py` in the workspace |
| `map.py` | writes Pi's provider/MCP config, builds the argv, runs Pi, maps its JSONL events with hgkit (`Mapper`) |
| `tests/` | recorded Pi streams from real turns (`fixtures/*.jsonl`) and `test_map.py` (offline) |

`--ignore-scripts`: the only dependency install scripts in 1.4.0's tree are protobufjs's `postinstall` (a version
warning) and @google/genai's no-op `preinstall`, and neither is needed.

## Provider

Each turn writes `$HOME/.pi/agent/models.json` (HOME persists with the session) with one custom provider, `hg`:

- `$HG_PROVIDER_API=anthropic` → Pi api `anthropic-messages`, `baseUrl=$HG_PROVIDER_URL`
- `$HG_PROVIDER_API=openai` → `baseUrl=$HG_PROVIDER_URL/v1`, Pi api `openai-responses` or `openai-completions`: the
  one Pi's own model catalog uses for that model id (e.g. `gpt-5.4-mini` → responses, `deepseek-flash` →
  completions), else `openai-completions`. The org setting `PI_OPENAI_API` forces one.
- `apiKey: "$HG_PROVIDER_TOKEN"` (Pi's env interpolation: the per-turn token is never written to disk).
- The model entry borrows context window, output limit, reasoning support and cost from Pi's catalog
  (`pi-ai/dist/providers/data/*.json`); compat flags and the thinking-level map only when the catalog entry uses
  the same wire API. DeepSeek models over chat completions get DeepSeek's compat (thinking format,
  `reasoning_content` replay) even when not in the catalog.

Pi runs with `PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0` (all three are 1.4.0 env vars, see
`docs/environment-variables.md`): no model-catalog refresh, no version check, no install telemetry or attribution
headers. Pi has no permission prompts; `--approve` trusts project-local `.pi/` resources (the sandbox is the boundary).

### Verified (2026-10-25)

| provider (shape) | model | Pi api | checks |
|---|---|---|---|
| `deepseek` (anthropic) — default | `deepseek-flash` | anthropic-messages | make + run, recall, stream, French, usage, cancel, error, MCP, json_schema, effort |
| `deepseek-openai` (openai) | `deepseek-flash` | openai-completions | make + run, recall (reasoning_content replayed), usage |
| `openai` (openai) | `gpt-5.4-mini` | openai-responses | make + run, recall, reasoning summaries, input_file + input_image |

`openai` with `PI_OPENAI_API=openai-completions` fails cleanly for `gpt-5.4-mini`: OpenAI rejects function tools with
`reasoning_effort` on chat completions — use the default (responses).

## Request fields

| Request field | Pi |
|---|---|
| `input` text (user/system/developer messages) | the prompt (after `--`, so a leading `-` is safe) |
| `input_file` / `input_image` by `file_id` | `@/run/hg/files/<id>/<name>` arguments (text inlined, images attached) |
| inline `file_data` / data-URL images | saved under `$HOME/.hg-inputs/turn-N/`, passed as `@file` |
| `instructions` | `--append-system-prompt` (Pi also reads `AGENTS.md` / `CLAUDE.md` in the workspace) |
| `previous_response_id` | `--session-id $HG_SESSION`: Pi opens (or creates) the session named after the platform's session, stored in `$HOME/.pi/agent/sessions/` |
| `store: false` | `--no-session` |
| `reasoning.effort` | `--thinking` (`none`→`off`, `minimal`, `low`, `medium`, `high`, `xhigh`) |
| `max_output_tokens` | the model's `maxTokens` in models.json |
| `text.format` json_schema / json_object | an instruction appended to the system prompt (Pi has no structured-output flag) |
| `tools` of type `mcp` (`server_url`) | `$HOME/.pi/agent/mcp.json`, `exposure: direct` (`allowed_tools` → `hidden` + `toolExposure: direct` per tool); `headers`/`authorization` go through env vars (`${HG_MCP_i_j}`), never into the file. Rewritten every turn; `--no-mcp` when there are none |
| `model` | `--provider hg --model $HG_MODEL` |
| ignored | `temperature`, `top_p`, `max_tool_calls`, `tool_choice`, `parallel_tool_calls`, function tools, `truncation`, `prompt_cache_key`, `service_tier`, `metadata` (echoed by the platform), MCP `require_approval` (always autonomous), MCP `connector_id` (no URL) |

MCP servers need their host in `run.egress`, which is empty: on this package remote MCP servers are blocked unless
the operator ships a copy with the host added (verified with `mcp.deepwiki.com` in such a copy).

## Output mapping

| Pi `--mode json` event | Responses |
|---|---|
| `message_update` `text_delta` | `response.output_text.delta`, token by token (hgkit `Emitter.text`) |
| `thinking_delta` / `thinking_end` | a `reasoning` item, emitted before the text or tool call that follows it |
| `tool_execution_start` `bash` | `shell_call` (command, `timeout_ms`) at start; `shell_call_output` at end with stdout and `exit_code` from Pi's `structuredContent` (`timeout` outcome on a tool timeout) |
| `edit` | `apply_patch_call` `update_file` with Pi's unified patch + `apply_patch_call_output` |
| `write` | `apply_patch_call` `create_file` (content as `+` lines) + output |
| `mcp__<server>__<tool>` | `mcp_call` (server_label, name, arguments, output or error) |
| any other tool (`read`, `grep`, `find`, `ls`, extension tools) | `function_call` at start + `function_call_output` at end |
| assistant `message_end` usage | summed over every assistant message of the turn → `usage` on the terminal event (`input_tokens` = input + cacheRead + cacheWrite; cached, cache-write and reasoning details) |
| `stopReason: error`, `auto_retry_end` failure | `response.failed`, `provider_error` (or `rate_limit_exceeded`) with Pi's message |
| `stopReason: length` | `response.incomplete` (`max_output_tokens`) |
| Pi exits non-zero with no assistant message | `response.failed`, `harness_error` with Pi's stderr tail |
| otherwise | `response.completed`, `metadata.pi_session` |

The org setting `PI_RAW_EVENTS=1` copies Pi's raw stream to the turn's stderr as `PI_RAW <json>` lines (how
`tests/fixtures` were recorded); it never reaches client-visible events.

## Known limits

- Stop/cancel: Pi and the mapper exit on SIGTERM (`stop_grace_s` 10). Each turn first kills any mapper / Pi / Pi tool process tree an
  earlier stopped turn left behind in the warm sandbox (Pi's bash tool runs commands in their own sessions, so a
  process-group signal does not reach them).
- Thinking text depends on the provider: DeepSeek's Anthropic endpoint often returns empty thinking blocks on tool
  turns (no reasoning item then); OpenAI returns reasoning summaries only sometimes.
- Tool calls are reported when Pi runs them, so output items arrive per tool, not as the model streams arguments.

## Tests

```
.venv/bin/python -m pytest packages/pi/tests -q
```
