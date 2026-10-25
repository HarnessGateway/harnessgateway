# codex — OpenAI Codex CLI on HarnessGateway

Runs the OpenAI Codex CLI **0.164.0** (`@openai/codex`, pinned in `install.sh`) headless: `codex exec --json
--skip-git-repo-check`, and `codex exec resume <thread>` on a continuation, against the organization's
OpenAI-shaped provider over the **Responses API** (`wire_api = "responses"`). Codex runs fully autonomous:
`approval_policy = "never"`, `sandbox_mode = "danger-full-access"`. The gVisor sandbox is the boundary, and Codex's own
landlock/seccomp sandbox would not work under gVisor anyway.

## Files

| file | role |
|---|---|
| `harness.json` | manifest: provider `openai`, default model `gpt-5.4-mini`, install egress `registry.npmjs.org`, no run egress |
| `install.sh` | `npm install -g @openai/codex@0.164.0` into `$HG_TOOLS`. It checks that the native linux x64 binary (the optional dependency `@openai/codex-linux-x64`) is installed and runs, then writes `$HG_TOOLS/codex-models.json` (see *Models*) |
| `request.sh` | runs `run.py` |
| `run.py` | Responses request → `$HOME/.codex/config.toml` + `codex exec` arguments; runs Codex and feeds its JSONL to the mapper; on SIGTERM it stops Codex and exits |
| `map.py` | Codex `exec --json` events → Responses events through `hgkit.Emitter` (`CodexMapper`) |
| `tests/` | `fixtures/*.jsonl`: raw Codex streams recorded from real turns on the platform. `test_map.py` runs them through the mapper and validates every line with `TurnOutput` and `outcome()` |

`CODEX_HOME` is `$HOME/.codex`, so Codex's sessions (rollouts) and state persist across turns. The config is rewritten
every turn. The provider points at `$HG_PROVIDER_URL/v1`, and its `env_key` names a variable that holds
`$HG_PROVIDER_TOKEN`. Update checks, analytics, feedback, plugins, apps and unbounded reconnects are off. Each raw
Codex line is also printed to the turn's log as `codex> ...` (a log line: never delivered to the client, visible in
`GET /v1/traces/<id>`).

## Request fields

| field | how Codex gets it |
|---|---|
| `input` text (user/system/developer messages) | the prompt (positional argument after `--`) |
| `input_image` (file_id or `data:` URL) | `-i <file>`. http(s) image URLs cannot be fetched (no run egress), so the URL is named in the prompt |
| `input_file` (file_id or inline `file_data`) | copied into the workspace. The prompt ends with `Attached files: ./name` |
| `instructions` | `developer_instructions` in config.toml (Codex puts it first in its developer message). A resumed thread keeps the developer instructions it started with, so new or changed instructions on a continuation are prepended to that turn's prompt as `<developer_instructions>…</developer_instructions>` |
| `previous_response_id` | `codex exec resume <thread id>`. The id comes from `thread.started` and is kept in `$HG_STATE/codex_thread` |
| `model` (`codex/<provider>/<model>`) | `-m $HG_MODEL` and `model = …` |
| `reasoning.effort` | `model_reasoning_effort` (none, minimal, low, medium, high, xhigh) |
| `reasoning.summary` | `model_reasoning_summary` (auto, concise, detailed, none). Defaults to `auto`, so reasoning items appear |
| `text.verbosity` | `model_verbosity` |
| `text.format` `json_schema` | `--output-schema <file>`. The final message is the JSON |
| `tools` type `mcp` | `[mcp_servers.<server_label>]` (streamable HTTP): `url`; `authorization` as `bearer_token_env_var`; `headers` as `http_headers`; `allowed_tools` as `enabled_tools` |
| `tools` type `web_search` / `web_search_preview` | `web_search = "live"` (Codex's hosted search runs on the provider side). Without it, `web_search = "disabled"` |
| `store: false` | `--ephemeral` |
| `stream`, `background`, `metadata` | handled by the platform |
| ignored | `tool_choice`, `parallel_tool_calls`, `max_tool_calls`, `max_output_tokens`, `temperature`, `top_p`, `top_logprobs`, `truncation`, `service_tier`, `prompt`, `prompt_cache_key`, `include`, function tools, replayed assistant/tool items in `input` (the history lives in Codex's own thread) |

## Output mapping

| Codex event | Responses |
|---|---|
| `thread.started` | thread id into `$HG_STATE`; `metadata.codex_thread_id` |
| `item.completed` `agent_message` | a `message`: `output_text` delta(s), then done. `item.updated` partial text would stream as deltas, but Codex 0.164.0's `exec --json` sends each message whole, so each message arrives as one delta |
| `item.completed` `reasoning` | `reasoning` item (summary text) |
| `item.started` / `item.completed` `command_execution` | `shell_call` (added `in_progress` when it starts, done when it ends) + `shell_call_output` (aggregated output, exit code) |
| `item.completed` `file_change` | an `apply_patch_call` + `apply_patch_call_output` per change (`add` → `create_file`, with the created file's content as the diff; `update` → `update_file`; `delete` → `delete_file`). Paths are relative to the workspace |
| `item.completed` `mcp_tool_call` | `mcp_call` (output text, or an `mcp_tool_execution_error`) |
| `item.completed` `web_search` | `web_search_call` (`search` / `open_page` / `find_in_page`) |
| `item.completed` `todo_list` | `function_call` `update_plan` + output |
| other item types (e.g. `collab_tool_call`) | `function_call` + `function_call_output` |
| `item.completed` `error` | a log line (Codex's non-fatal warnings) |
| `turn.completed` | `response.completed` with usage. `cached_input_tokens` → `cached_tokens`, `cache_write_input_tokens` → `cache_write_tokens`, `reasoning_output_tokens` → `reasoning_tokens`. On `exec resume`, Codex reports the thread's cumulative usage, so the mapper subtracts the total it recorded after the previous turn (`$HG_STATE/codex_usage`) |
| `turn.failed`, or exit without `turn.completed` | `response.failed` with Codex's message (code `model_not_found`, `rate_limit_exceeded`, `provider_auth_error` or `provider_error`). A top-level `error` alone is a retry notice and only fails the turn if no `turn.completed` follows |

## Models

0.164.0's bundled model catalog lists only newer models (gpt-5.5, gpt-5.6-*, gpt-6-*). For any other model, Codex
falls back to generic metadata: no apply_patch tool (files are written through the shell), no reasoning summaries, and
a warning. `install.sh` therefore writes the bundled catalog plus entries for `gpt-5.4`, `gpt-5.4-mini` and
`gpt-5.4-nano`, copied from the gpt-5.5 entry, and `run.py` points `model_catalog_json` at it.

Verified on the platform (2026-10-25):

- `openai`/`gpt-5.4-mini` (default): all checks.
- `openai`/`gpt-5.4`: a simple turn.
- `openai`/`gpt-4.1-mini`: works on fallback metadata.
- `deepseek-openai`/`deepseek-flash`: a simple turn.
- The anthropic-shaped `deepseek` provider cannot be used: Codex speaks only the Responses API.

## Known limits

- Text is not token-streamed: `codex exec --json` emits each agent message whole (token deltas exist only in
  `codex app-server`).
- MCP servers and http(s) image URLs need network access the sandbox does not have (`run.egress` is empty). An MCP
  server is reachable only if the platform can route to it.
- Codex's `update_plan` tool is not offered in `exec` mode, so `todo_list` items are rare.
- Cancel: the package exits within about a second of SIGTERM (checked in the sandbox). The platform's `signal_group`
  currently never delivers the signal (see the report), so a cancelled turn ends only when the stop grace (10 s) runs
  out.
