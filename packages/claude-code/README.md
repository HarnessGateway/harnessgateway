# claude-code

Anthropic's Claude Code CLI behind the OpenAI Responses API. Each turn runs `claude -p` once in the session's
workspace, with permissions skipped (the gVisor sandbox is the boundary), and maps its `stream-json` output onto
Responses stream events.

- **CLI:** `@anthropic-ai/claude-code` **2.1.310**, pinned in `install.sh`, installed by the npm route Anthropic's
  setup docs give (`npm install -g @anthropic-ai/claude-code`, https://code.claude.com/docs/en/setup; egress
  `registry.npmjs.org`). The npm postinstall places the native binary for the machine's CPU (npm picks it) at
  `$HG_TOOLS/bin/claude`. `install.sh` fails if `claude --version` is anything other than 2.1.310.
- **Provider:** any Anthropic Messages endpoint (`provider.apis: ["anthropic"]`). The default model is
  `claude-sonnet-5-5`, Anthropic's newest mid-tier model; name another in `model` for any other provider. Verified with
  DeepSeek's Anthropic-compatible endpoint (`deepseek-flash`, `deepseek-v4-pro`).
- **Run egress:** none of its own; the organization's egress and the harness's MCP catalog hosts apply. Telemetry,
  error reporting and auto-update are switched off.

## Files

| File | Role |
|---|---|
| `harness.json` | manifest (contract 1) |
| `install.sh` | installs the pinned CLI into `$HG_TOOLS` |
| `run.sh` | `exec python3 "$HG_PACKAGE_DIR/map.py"` (the platform starts it in the workspace) |
| `map.py` | places the request's files, builds the command line and environment from the request and `$HG_MCP`, runs the CLI, maps its stream (hgkit `Emitter`) |
| `tests/` | recorded CLI streams from real turns (`fixtures/*.jsonl`) and `test_map.py` |

## The command

```
claude -p --output-format stream-json --verbose --include-partial-messages
       --dangerously-skip-permissions --system-prompt-snapshot off --model $HG_MODEL
       [--resume <session id>] [--append-system-prompt-file F] [--effort E] [--json-schema S]
       [--mcp-config F --strict-mcp-config] [--settings HOOK] [--max-turns N]          (prompt on stdin)
```

Environment: `ANTHROPIC_BASE_URL=$HG_PROVIDER_URL`, `ANTHROPIC_API_KEY=$HG_PROVIDER_TOKEN` (and
`ANTHROPIC_AUTH_TOKEN` removed). Every variable the CLI reads a model from is set to `$HG_MODEL`, so no other model
is ever requested: `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_{FABLE,OPUS,SONNET,HAIKU}_MODEL`,
`ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`, `CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL`,
`CLAUDE_CODE_AUTO_MODE_MODEL` and `CLAUDE_CODE_BG_CLASSIFIER_MODEL`. Also set: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`,
`DISABLE_AUTOUPDATER=1`, `DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1`, `DISABLE_BUG_COMMAND=1`, `IS_SANDBOX=1`,
`CLAUDE_CODE_MAX_RETRIES=3`. `HG_PROVIDER_URL` is the box's door (`http://hg-door:8080`, already in `NO_PROXY`), which
relays to the gateway. Provider traces show only `/v1/messages` calls, all for the requested model. The CLI writes
`[claude-code:unrecognized_model]` to stderr for non-Anthropic model names; this is harmless.

Every flag above was checked against `claude --help` for 2.1.310. `--max-turns` and `--append-system-prompt-file`
don't appear in the help text, but they are in the binary and the CLI accepts them.

## Request fields

| Request field | Where it goes |
|---|---|
| `input` (string, or the `input_text` parts of user messages) | the prompt, on the CLI's stdin |
| `input_file` / `input_image` (by `file_id`, or inline `file_data` / data: URL) | written into the workspace by name (hgkit `place_inputs`), and the prompt lists the names |
| `instructions`, and `system` / `developer` messages in `input` | `--append-system-prompt-file`: appended to Claude Code's own system prompt. `--system-prompt-snapshot off` makes each turn's instructions apply, so the CLI doesn't replay turn 1's |
| `previous_response_id` | `--resume <id>`. The CLI's session id is kept in `$HG_STATE/session` and its transcript in `$HOME/.claude/projects`, which the manifest persists. If the transcript has gone, the turn starts a new CLI session |
| `reasoning.effort` | `--effort`: `minimal`→`low`; `low`/`medium`/`high`/`xhigh` pass through unchanged. `none` sets `CLAUDE_CODE_DISABLE_THINKING=1`. The provider receives `output_config.effort` |
| `text.format` `json_schema` | `--json-schema <schema>`. The CLI's `structured_output` becomes the final assistant message (JSON text), and its internal `StructuredOutput` tool call is not shown |
| `tools` of type `mcp` | the platform resolves them with the harness's catalog into `$HG_MCP` (SPEC §3.3); `map.py` reads only that file. Each server becomes an entry of `--mcp-config` (`{"mcpServers": {label: {"type": "http" or "sse", "url", "headers"}}}`, header values holding the platform's placeholders, which the door replaces) plus `--strict-mcp-config`, so only the turn's servers are used |
| `allowed_tools` (of a turn's MCP server) | a `PreToolUse` hook in `--settings`, matching the narrowed servers' tools, that denies any tool not on the list. Claude Code has no per-server tool filter, so the tools stay visible and a call to one not allowed fails as a `failed` `mcp_call` |
| `max_tool_calls` | `--max-turns N`. The CLI counts agentic turns, so parallel tool calls in one turn count once |
| `model` | `harness/provider/model`. The platform passes the model as `$HG_MODEL` |

**Ignored**, and named per turn in `metadata.hg_ignored_fields` when the request sets them: `tools` other than `mcp`
(Claude Code uses only its own built-in tools plus MCP), `tool_choice`, `parallel_tool_calls`, `temperature`, `top_p`,
`top_logprobs`, `max_output_tokens`, `truncation`, `include`, `prompt_cache_key`, `service_tier`, `text.format`
`json_object`, `text.verbosity` and `reasoning.summary`. Also not acted on: the assistant/tool items of an `input` list
(the CLI's own resumed session supplies the history). `metadata`, `store`, `background`, `stream` and MCP
`require_approval` are the platform's.

## Output mapping

| Claude Code stream-json | Responses |
|---|---|
| `system` `init` / `result` | session id → `$HG_STATE/session`. Also `metadata.claude_session_id`, `claude_num_turns`, `claude_version` |
| `stream_event` `content_block_delta` `text_delta` | `response.output_text.delta`, token by token, in an assistant `message` |
| `stream_event` `thinking_delta`, then the `thinking` block | a `reasoning` item whose `summary_text` streams as `response.reasoning_summary_text.delta` (the raw thinking text). Empty thinking blocks are dropped |
| `tool_use` `Bash` + its `tool_result` | `shell_call` (emitted when called, `timeout_ms` from the tool input) + `shell_call_output` (stdout/stderr from `tool_use_result`, exit code from `Exit code N`, `timeout` outcome if interrupted) |
| `tool_use` `Write` / `Edit` / `MultiEdit` + result | `apply_patch_call` (`create_file` with every line `+`, or `update_file` with the CLI's own unified hunks) + `apply_patch_call_output` (`failed` if the tool errored). Paths are relative to the workspace |
| `tool_use` `mcp__<server>__<tool>` + result | `mcp_call`: added `in_progress` when called, done `completed` with `output`, or `failed` with an `mcp_tool_execution_error`. The server label maps back to the turn's (`$HG_MCP`) |
| any other `tool_use` (Read, Glob, Grep, ToolSearch, Task, WebFetch, TodoWrite, ...) + result | `function_call` + `function_call_output` (`incomplete` if the tool errored) |
| subagent messages (`parent_tool_use_id` set) | their tool calls are mapped as above. Their text and thinking are not added to the reply |
| `result` `usage` | terminal `usage`: `input_tokens` = input + `cache_read_input_tokens` + `cache_creation_input_tokens` (OpenAI counts cached tokens inside input); `cached_tokens` = cache reads; `cache_write_tokens` = cache creation. `reasoning_tokens` is 0 because the CLI doesn't report it separately |
| `result` `success` | `response.completed` (with `metadata.hg_ignored_fields` when the request set fields the CLI cannot honour) |
| `result` `error_max_turns` | `response.incomplete` with `incomplete_details.reason = "max_messages"` and the work done so far |
| `result` `is_error` / any other subtype | `response.failed`. The message is the CLI's (for example `API Error: 400 The supported API model names are ...`), and the code is `provider_error` for API errors, otherwise the subtype. The CLI's synthetic error message is not streamed as model text |
| no `result` (the CLI crashed) | `response.failed` `cli_error` with the exit code and the tail of stderr |

Every raw CLI line is also printed to the turn's log, prefixed `claude-stream `. Log lines go to the trace and are
never shown to the client. The test fixtures came from those log lines.

On cancel or timeout the platform signals the turn's process group (`map.py` and the CLI) and then stops every process
left in the box, including the commands Claude Code runs in process groups of their own.

## Known limits

- **MCP servers need egress.** A server's host must be in the turn's egress: the harness's catalog hosts are, a
  request-added server is refused by the platform unless the organization allows its host. Verified on the platform with
  a request-added server (the door injected its bearer token); an MCP tool call end to end was verified with a temporary
  copy of the old package whose egress allowed `mcp.deepwiki.com` (fixture `mcp_call.jsonl`), and the `allowed_tools`
  hook with the real CLI against a local MCP server.
- Claude Code's built-in tool set includes tools that are pointless in a one-shot headless turn (Cron*, ScheduleWakeup,
  worktrees, Monitor). They are left enabled. Background Bash tasks do not outlive the turn.
- The CLI rejects a bare `sleep N` in the shell ("Blocked: standalone sleep"). That is Claude Code's policy, not this
  package's.
- Cost (`total_cost_usd`) is not reported, because the CLI prices non-Anthropic models with Anthropic's rates.

## Tests

```
.venv/bin/pytest -q packages/claude-code
```

The fixtures are raw CLI streams recorded from real turns on the platform (deepseek-flash / deepseek-v4-pro):
write+bash, streamed thinking, Write+Edit+Bash, json_schema, max_turns, a blocked Bash plus ToolSearch, a provider
error, an MCP call, and an unreachable MCP server. Each one is fed through `Mapper`, and its output is validated with the
platform's own reader, `harnessgateway.domain.contract.check_output`. The tests also check the command line, the MCP
config and the `allowed_tools` hook built from `$HG_MCP`, the environment, the ignored fields, and run `map.py` end to
end against a fake `claude` (instructions and system/developer messages, placed files, a lost CLI session).
