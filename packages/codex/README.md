# codex

OpenAI's Codex CLI behind the OpenAI Responses API. Each turn runs `codex exec --json` once in the session's workspace
(`codex exec resume <thread>` on a continuation), fully autonomous (`approval_policy = "never"`,
`sandbox_mode = "danger-full-access"`: the gVisor sandbox is the boundary, and Codex's own landlock/seccomp sandbox
would not work under gVisor anyway), against the organization's OpenAI-shaped provider over the Responses API, and
maps its JSONL events onto Responses stream events.

- **CLI:** `@openai/codex` **0.164.0**, pinned in `install.sh`, installed with npm as the openai/codex README
  documents (`npm install -g @openai/codex`, https://github.com/openai/codex), egress `registry.npmjs.org`. The
  package is a small node launcher; npm installs the native binary built for the machine's CPU (one of its optional
  dependencies) and the launcher runs it. `install.sh` fails unless `codex --version` reports 0.164.0, then writes
  `$HG_TOOLS/codex-models.json` (see *The command*).
- **Provider:** any OpenAI endpoint that serves the Responses API (`provider.apis: ["openai"]`,
  `wire_api = "responses"`); the default model is `gpt-6.1-sol`, Codex 0.164.0's own default (first in its bundled
  catalog). Verified with OpenAI (`gpt-5.4-mini`, `gpt-5.4`; `gpt-4.1-mini` runs on fallback metadata) and with
  DeepSeek's Responses-compatible endpoint (`deepseek-flash`). An Anthropic-shaped provider cannot be used: Codex
  speaks only the Responses API, and the package refuses it with `unsupported_provider`.
- **Run egress:** none of its own (the organization's applies; a turn's MCP servers are reached through it). Update
  checks, analytics, feedback, plugins, apps and unbounded reconnects are switched off.

## Files

| File | Role |
|---|---|
| `harness.json` | manifest (contract 1) |
| `install.sh` | installs the pinned CLI into `$HG_TOOLS` and writes its model catalog |
| `run.sh` | `exec python3 "$HG_PACKAGE_DIR/map.py"` |
| `map.py` | places input files, builds the command line and its `-c` overrides from the request and `$HG_MCP`, runs the CLI, maps its JSONL (hgkit `Emitter`) |
| `tests/` | recorded Codex streams from real turns (`fixtures/*.jsonl`) and `test_map.py` |

## The command

```
codex exec --json --skip-git-repo-check -m $HG_MODEL -c K=V... [--output-schema F] [--ephemeral] [-i IMAGE]... -- PROMPT
codex exec resume --json --skip-git-repo-check -m $HG_MODEL -c K=V... [--output-schema F] [--ephemeral] [-i IMAGE]... THREAD -- PROMPT
```

Environment: `CODEX_HOME=$HOME/.codex` (created if absent; its `sessions/` rollouts are what the manifest persists)
and `HG_CODEX_API_KEY=$HG_PROVIDER_TOKEN` (the provider's `env_key`).

The package writes no configuration file. `$HOME/.codex` belongs to the organization (an overlay's
`seeds/home/.codex/`: `config.toml`, `AGENTS.md`, skills); the turn's mechanics go on the command line as `-c`
overrides on top of it: `model_provider = "hg"` with `model_providers.hg = {base_url = "$HG_PROVIDER_URL/v1",
env_key = "HG_CODEX_API_KEY", wire_api = "responses", request_max_retries = 2, stream_max_retries = 2}`; `model`,
`approval_policy = "never"`, `sandbox_mode = "danger-full-access"`, `check_for_update_on_startup = false`,
`hide_agent_reasoning = false`, `model_catalog_json`, the reasoning, verbosity, instructions and web search keys from
the request, `analytics.enabled` and `feedback.enabled` `false`, and `features.unbounded_connection_retries`,
`plugins`, `apps`, `remote_plugin`, `daemon_auto_start` and `in_app_updates` all `false`; plus
`mcp_servers.<name> = {url, startup_timeout_sec = 30, http_headers, enabled_tools}` per MCP server of the turn
(`<name>` is the label with every character outside `[A-Za-z0-9_-]` replaced by `_`, since a `-c` path cannot hold a
dot).

**Models.** 0.164.0's bundled model catalog lists only newer models (gpt-5.5, gpt-5.6-*, gpt-6-*). For any other
model, Codex falls back to generic metadata: no apply_patch tool (files are written through the shell), no reasoning
summaries, and a warning. `install.sh` therefore writes the bundled catalog (`codex debug models --bundled`) plus
entries for `gpt-5.4`, `gpt-5.4-mini` and `gpt-5.4-nano`, copied from the gpt-5.5 entry, to
`$HG_TOOLS/codex-models.json`, and `map.py` points `model_catalog_json` at it.

Every flag above was checked against `codex exec --help` and `codex exec resume --help` for 0.164.0 (`exec resume`
takes `--json`, `--skip-git-repo-check`, `-m`, `-c`, `--output-schema`, `--ephemeral` and `-i` too).

## Request fields

| Request field | Where it goes |
|---|---|
| `input` user text | the prompt (positional argument after `--`) |
| `input_image`, `input_file` (file_id, `data:` URL or inline `file_data`) | placed in the workspace by name (hgkit `place_inputs`). Images (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`) are also attached with `-i <file>`; the prompt ends with `Attached files: ./name` for the others. http(s) URLs are not fetched; the URL is named in the prompt |
| `instructions`, `system`/`developer` messages | `developer_instructions` (Codex adds it as a developer message after its own base instructions and the organization's `AGENTS.md`). A resumed thread keeps the developer instructions it started with, so new or changed instructions on a continuation are prepended to that turn's prompt as `<developer_instructions>…</developer_instructions>` |
| `previous_response_id` | `codex exec resume <thread id>`. The id comes from `thread.started` and is kept in `$HG_STATE/codex_thread` |
| `model` (`codex/<provider>/<model>`) | `-m $HG_MODEL` and `model = …` |
| `reasoning.effort` | `model_reasoning_effort` (none, minimal, low, medium, high, xhigh) |
| `reasoning.summary` | `model_reasoning_summary` (auto, concise, detailed, none). Defaults to `auto`, so reasoning items appear |
| `text.verbosity` | `model_verbosity` |
| `text.format` `json_schema` | `--output-schema <file>`. The final message is the JSON |
| the turn's MCP servers (`$HG_MCP`: the package's catalog and the request's `tools[type=mcp]`, as the platform resolved them) | `mcp_servers.<name>` (streamable HTTP): `url`; `headers` (placeholders the door replaces) as `http_headers`; `allowed_tools` as `enabled_tools`. The `mcp_call` items carry the server's label |
| `tools` of type `web_search` / `web_search_preview` | `web_search = "live"` (Codex's hosted search runs on the provider side). Without it, `web_search = "disabled"` |
| `store: false` | `--ephemeral` |

These fields are **ignored**, and each one present is named in `metadata.hg_ignored_fields`: `tool_choice`,
`parallel_tool_calls`, `max_tool_calls`, `max_output_tokens`, `temperature`, `top_p`, `top_logprobs`, `truncation`,
`service_tier`, `prompt`, `prompt_cache_key`, `include`, and function tools (`tools.function`). Replayed
assistant/tool items in `input` are not used either (the history lives in Codex's own thread). `stream`,
`background`, `store` and `metadata` are handled by the platform.

## Output mapping

| Codex `exec --json` | Responses |
|---|---|
| `thread.started` | thread id into `$HG_STATE/codex_thread`; `metadata.codex_thread_id` |
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

Every raw Codex line is also printed to the turn's log, prefixed `codex> `. Log lines go to the trace
(`GET /v1/traces/<id>`) and are never shown to the client. The test fixtures came from those log lines.

On SIGTERM (cancel or timeout), `map.py` passes the signal to Codex, waits up to 10 s for it to exit (then kills it)
and exits without a terminal event.

## Known limits

- Text is not token-streamed: `codex exec --json` emits each agent message whole (token deltas exist only in
  `codex app-server`).
- http(s) image URLs are not fetched. Codex speaks streamable HTTP to MCP servers only: a server of `transport`
  `sse` is configured the same way and fails to start.
- Codex's `update_plan` tool is not offered in `exec` mode, so `todo_list` items are rare.
- Only an OpenAI provider that serves the Responses API can run a turn.

## Tests

```
.venv/bin/pytest -q packages/codex
```

The fixtures are raw Codex streams (the `codex> ` log lines) recorded from real turns on the platform with
gpt-5.4-mini: a file-creating first turn and its resumed second turn, an image and a file as input, json_schema,
web search with edits, a cancelled `sleep 300`, and an unknown model. Each one is fed through `Mapper`, and every
printed line is read with `harnessgateway.domain.contract.check_output`, the platform's own reader. The tests also
check the `-c` overrides and command line built from a request (instructions with system/developer messages, MCP
servers from an `$HG_MCP` file, ignored fields, attachments), item types the recordings do not have (streamed agent
text, MCP calls, todo lists, other tools, rate limits), and run `map.py` end to end as `run.sh` does, against a fake
`codex` that replays a recorded stream.
