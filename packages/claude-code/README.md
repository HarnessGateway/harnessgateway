# claude-code

Anthropic's Claude Code CLI behind the OpenAI Responses API. Each turn runs `claude -p` once in the session's
workspace, with permissions skipped (the gVisor sandbox is the boundary), and maps its `stream-json` output onto
Responses stream events.

- **CLI:** `@anthropic-ai/claude-code` **2.1.310**, pinned in `install.sh` (npm, egress `registry.npmjs.org`). The npm
  postinstall places the native binary for the machine's CPU (npm picks it) at `$HG_TOOLS/bin/claude`. `install.sh` fails if `claude --version`
  is anything other than 2.1.310.
- **Provider:** any Anthropic Messages endpoint (`provider.apis: ["anthropic"]`). Verified with `deepseek`
  (DeepSeek's Anthropic-compatible endpoint) using `deepseek-flash` (the default) and `deepseek-v4-pro`.
- **Run egress:** none. Telemetry, error reporting and auto-update are switched off.

## Files

| File | Role |
|---|---|
| `harness.json` | manifest (contract 1) |
| `install.sh` | installs the pinned CLI into `$HG_TOOLS` |
| `request.sh` | `exec python3 map.py` in the workspace |
| `map.py` | builds the command line and environment from the request, runs the CLI, maps its stream (hgkit `Emitter`) |
| `tests/` | recorded CLI streams from real turns (`fixtures/*.jsonl`) and `test_map.py` |

## The command

```
claude -p --output-format stream-json --verbose --include-partial-messages
       --dangerously-skip-permissions --system-prompt-snapshot off --model $HG_MODEL
       [--resume <session id>] [--append-system-prompt-file F] [--effort E] [--json-schema S]
       [--mcp-config F --strict-mcp-config] [--max-turns N]          (prompt on stdin)
```

Environment: `ANTHROPIC_BASE_URL=$HG_PROVIDER_URL`, `ANTHROPIC_API_KEY=$HG_PROVIDER_TOKEN` (and
`ANTHROPIC_AUTH_TOKEN` removed). Every variable the CLI reads a model from is set to `$HG_MODEL`, so no other model
is ever requested: `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_{FABLE,OPUS,SONNET,HAIKU}_MODEL`,
`ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`, `CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL`,
`CLAUDE_CODE_AUTO_MODE_MODEL` and `CLAUDE_CODE_BG_CLASSIFIER_MODEL`. Also set: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`,
`DISABLE_AUTOUPDATER=1`, `DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1`, `DISABLE_BUG_COMMAND=1`, `IS_SANDBOX=1`,
`CLAUDE_CODE_MAX_RETRIES=3`, and `hg-egress` added to `NO_PROXY`. Provider traces show only `/v1/messages` calls, all
for the requested model. The CLI writes `[claude-code:unrecognized_model]` to stderr for non-Anthropic model names;
this is harmless.

Every flag above was checked against `claude --help` for 2.1.310. `--max-turns` and `--append-system-prompt-file`
don't appear in the help text, but they are in the binary and the CLI accepts them.

## Request fields

| Request field | Where it goes |
|---|---|
| `input` (string, or `input_text` parts of user/system/developer messages) | the prompt, on the CLI's stdin |
| `input_file` / `input_image` by `file_id` | copied from `$HG_FILES` into the workspace under its filename, and the prompt lists the names |
| `input_file` / `input_image` inline (`file_data`, data: URL) | written into the workspace the same way |
| `instructions` | `--append-system-prompt-file`. `--system-prompt-snapshot off` makes each turn's instructions apply, so the CLI doesn't replay turn 1's |
| `previous_response_id` | `--resume <id>`. The CLI's session id is kept in `$HG_STATE/session` and its transcript in `$HOME/.claude`, and both persist. If the transcript has gone, the turn starts a new CLI session |
| `reasoning.effort` | `--effort`: `minimal`→`low`; `low`/`medium`/`high`/`xhigh` pass through unchanged. `none` sets `CLAUDE_CODE_DISABLE_THINKING=1`. The provider receives `output_config.effort` |
| `text.format` `json_schema` | `--json-schema <schema>`. The CLI's `structured_output` becomes the final assistant message (JSON text), and its internal `StructuredOutput` tool call is not shown |
| `tools` of type `mcp` | `--mcp-config` (`{"mcpServers": {label: {"type":"http","url":...,"headers":...}}}`, with `authorization` → `Authorization: Bearer`) plus `--strict-mcp-config`, so only the request's servers are used |
| `max_tool_calls` | `--max-turns N`. The CLI counts agentic turns, so parallel tool calls in one turn count once |
| `model` | `harness/provider/model`. The platform passes the model as `$HG_MODEL` |

These fields are **ignored**: `tools` of type `function` / `web_search` / others (Claude Code uses only its own
built-in tools plus MCP), `tool_choice`, `parallel_tool_calls`, `temperature`, `top_p`, `max_output_tokens`, `truncation`,
`include`, `prompt_cache_key`, `service_tier`, `top_logprobs`, `text.format` `json_object`, `text.verbosity`,
`mcp.allowed_tools` / `require_approval` (MCP tools always run), `reasoning.summary`, and the assistant/tool items of
an `input` list (the CLI's own resumed session supplies the history). `metadata`, `store`, `background` and `stream`
are handled by the platform.

## Output mapping

| Claude Code stream-json | Responses |
|---|---|
| `system` `init` / `result` | session id → `$HG_STATE/session`. Also `metadata.claude_session_id`, `claude_num_turns`, `claude_version` |
| `stream_event` `content_block_delta` `text_delta` | `response.output_text.delta`, token by token, in an assistant `message` |
| `stream_event` `thinking_delta`, then the `thinking` block | a `reasoning` item whose `summary_text` streams as `response.reasoning_summary_text.delta` (the raw thinking text). Empty thinking blocks are dropped |
| `tool_use` `Bash` + its `tool_result` | `shell_call` (emitted when called, `timeout_ms` from the tool input) + `shell_call_output` (stdout/stderr from `tool_use_result`, exit code from `Exit code N`, `timeout` outcome if interrupted) |
| `tool_use` `Write` / `Edit` / `MultiEdit` + result | `apply_patch_call` (`create_file` with every line `+`, or `update_file` with the CLI's own unified hunks) + `apply_patch_call_output` (`failed` if the tool errored). Paths are relative to the workspace |
| `tool_use` `mcp__<server>__<tool>` + result | `mcp_call`: added `in_progress` when called, done `completed` with `output`, or `failed` with an `mcp_tool_execution_error`. The server label maps back to the request's |
| any other `tool_use` (Read, Glob, Grep, ToolSearch, Task, WebFetch, TodoWrite, ...) + result | `function_call` + `function_call_output` (`incomplete` if the tool errored) |
| subagent messages (`parent_tool_use_id` set) | their tool calls are mapped as above. Their text and thinking are not added to the reply |
| `result` `usage` | terminal `usage`: `input_tokens` = input + `cache_read_input_tokens` + `cache_creation_input_tokens` (OpenAI counts cached tokens inside input); `cached_tokens` = cache reads; `cache_write_tokens` = cache creation. `reasoning_tokens` is 0 because the CLI doesn't report it separately |
| `result` `success` | `response.completed` |
| `result` `error_max_turns` | `response.incomplete` with `incomplete_details.reason = "max_messages"` and the work done so far |
| `result` `is_error` / any other subtype | `response.failed`. The message is the CLI's (for example `API Error: 400 The supported API model names are ...`), and the code is `provider_error` for API errors, otherwise the subtype. The CLI's synthetic error message is not streamed as model text |
| no `result` (the CLI crashed) | `response.failed` `cli_error` with the exit code and the tail of stderr |

Every raw CLI line is also printed to the turn's log, prefixed `claude-stream `. Log lines go to the trace and are
never shown to the client. The test fixtures came from those log lines.

On SIGTERM (cancel or timeout), `map.py` stops the CLI **and every process it started**. Claude Code runs each Bash
command in its own process group, which a signal to the turn's group would miss, so `map.py` tracks the CLI's
descendants and signals them (TERM, then KILL after 3 s).

## Known limits

- **MCP servers need egress.** `run.egress` is empty, so the sandbox can't reach a remote MCP server. The CLI reports
  `ERR_PROXY_TUNNEL ... 403` and the model says it failed to connect. The mapping was verified end to end with a
  temporary copy of this package whose `run.egress` allowed `mcp.deepwiki.com` (fixture `mcp_call.jsonl`).
- Claude Code's built-in tool set includes tools that are pointless in a one-shot headless turn (Cron*, ScheduleWakeup,
  worktrees, Monitor). They are left enabled. Background Bash tasks do not outlive the turn.
- The CLI rejects a bare `sleep N` in the shell ("Blocked: standalone sleep"). That is Claude Code's policy, not this
  package's.
- Cost (`total_cost_usd`) is not reported, because the CLI prices non-Anthropic models with Anthropic's rates.

## Tests

```
.venv/bin/python -m pytest packages/claude-code/tests -q
```

The fixtures are raw CLI streams recorded from real turns on the platform (deepseek-flash / deepseek-v4-pro):
write+bash, streamed thinking, Write+Edit+Bash, json_schema, max_turns, a blocked Bash plus ToolSearch, a provider
error, an MCP call, and an unreachable MCP server. Each one is fed through `Mapper`, and every printed line is
validated with `harnessgateway.domain.wire.TurnOutput` and its `outcome()`. The tests also check the command line
and environment built from a request.
