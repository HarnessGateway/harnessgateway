# The Responses wire

HarnessGateway speaks one protocol, the OpenAI Responses API, and nothing else. A client sends a
standard Responses request. The platform hands that payload, whole, to a harness package. The
package reads whatever fields it needs and invokes its CLI. The CLI's output comes back to the
package, and the package maps it onto a standard Responses response, which is what the client gets.

There is no HarnessGateway request schema and no HarnessGateway response schema. There is the
Responses schema, in both directions, and a mapping on each side of a CLI.

```
client ──(Responses request)──▶ platform ──(the same payload)──▶ harness package ──▶ CLI
client ◀─(Responses response)── platform ◀─(the mapped output)── harness package ◀── CLI
```

**On the way in**, the package parses the request payload. Every field in it is available: the
prompt text and attached files in `input`, standing `instructions`, the `model`, a
`previous_response_id` to continue a conversation, `reasoning.effort`, MCP servers in `tools`, and
so on. The package turns the fields its CLI can use into that CLI's flags, config files and
conventions, and the platform refuses the fields no harness can take.

**On the way out**, the package maps what the CLI produced onto the response payload. Every field
in it is available to fill: the final text as a `message`, the agent's thinking as `reasoning`,
each command it ran as a `shell_call` with its `shell_call_output`, each file it wrote as an
`apply_patch_call`, each MCP tool it used as an `mcp_call`, the token counts as `usage`, the
session it can resume later as `metadata`. What the CLI cannot supply, the package leaves out.

Any OpenAI SDK, in any language, talks to the platform unchanged. The platform's `model` field
carries the routing: `<harness>/<provider>/<model>`, for example
`claude-code/deepseek/deepseek-chat`. The harness name picks the package, the provider name picks
the endpoint and key the tenant registered, and the model name goes to the CLI.

## The request schema

The schema is OpenAI's. The platform validates every request against the SDK's own types and
restates nothing.

- Reference: [Create a model response](https://platform.openai.com/docs/api-reference/responses/create)
- Input items: [Input items](https://platform.openai.com/docs/api-reference/responses/input-items)
- The SDK's definition, openai-python 3.35.1: [`response_create_params.py`](https://github.com/openai/openai-python/blob/v3.35.1/src/openai/types/responses/response_create_params.py)
- The Python type the platform validates with: `openai.types.responses.ResponseCreateParams`

A request has these fields, every one optional on the wire; `model` and `input` are what a request
needs in practice:

`model`, `input`, `instructions`, `previous_response_id`, `conversation`, `metadata`, `store`,
`stream`, `stream_options`, `background`, `include`, `tools`, `tool_choice`, `parallel_tool_calls`,
`max_tool_calls`, `reasoning` (`effort`, `summary`), `text` (`format`, `verbosity`), `temperature`,
`top_p`, `top_logprobs`, `max_output_tokens`, `truncation`, `prompt`, `prompt_cache_key`,
`prompt_cache_retention`, `prompt_cache_options`, `context_management`, `service_tier`,
`safety_identifier`, `user`, `moderation`, `access_programs`.

`input` is a string or a list of items: `message` items whose content is text, `input_text`,
`input_file` and `input_image` parts; prior tool calls and their outputs; `reasoning` items; and
`item_reference`. `tools` entries are `function`, `mcp`, `shell`, `apply_patch`, `local_shell`,
`custom`, `file_search`, `web_search`, `code_interpreter`, `image_generation`, `computer`,
`namespace` and `tool_search`.

## The harnesses

The ten below are the ten most used harnesses in a public ranking at the time of writing. Every
cell in both tables was taken from the CLI's own `--help`, run on the version named, not from its
documentation. The non-interactive invocation each package builds on:

| Harness | Version | Non-interactive invocation |
|---|---|---|
| Hermes Agent | 0.19.0 | `hermes -z "PROMPT"` (one-shot, final text only) or `hermes chat -q "PROMPT" --yolo` |
| Claude Code | 2.1.310 | `claude -p "PROMPT" --output-format stream-json --verbose --dangerously-skip-permissions` |
| Kilo Code | 7.8.12 | `kilo run "PROMPT" --format json --auto` |
| Cline | 3.0.73 | `cline "PROMPT" --json -y` |
| Pi | 1.3.0 | `pi -p "PROMPT" --mode json` |
| Codex | 0.163.0 | `codex exec "PROMPT" --json --skip-git-repo-check` |
| Oh My Pi | 18.12.0 | `omp -p "PROMPT" --mode json --auto-approve` |
| OpenClaw | 2026.10.6 | `openclaw agent exec "PROMPT" --json` |
| DeepSeek Harness | 0.2.0-rc.2 | `dsh --profile headless "PROMPT" --json` |
| Command Code | 1.88.0 | `command-code -p "PROMPT" --output-format json --yolo` |

Hermes, Kilo Code, Cline, Pi, Oh My Pi, OpenClaw and Command Code have no documented way to point
the CLI at a provider by flag alone; the provider's URL and key go through their config files or
their standard environment variables (`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL` and the matching
keys), which the platform sets before the turn. Codex takes its provider from `config.toml`, which
`-c key=value` can write at invocation time. DeepSeek Harness takes everything from a profile and
its `--patch` overlays. Claude Code reads the environment directly.

## Request fields to invocation

How each request field reaches each harness. "workspace" means the platform writes the file into
the directory the CLI runs in, and "AGENTS.md" means the package writes the text into that file in
the workspace, which the CLI reads on its own. "—" means the CLI's help offers nothing for the
field; the platform refuses it for that harness. Flags are the CLI's own.

| Request field | Hermes Agent | Claude Code | Kilo Code | Cline | Pi | Codex | Oh My Pi | OpenClaw | DeepSeek Harness | Command Code |
|---|---|---|---|---|---|---|---|---|---|---|
| `input` text | `-z PROMPT` / `chat -q` | positional with `-p` | `run [message..]` | positional prompt | positional with `-p` | positional `PROMPT`, or stdin | positional with `-p` | positional, `--message-file` (`-` for stdin) | positional task, or `-` for stdin | `-p [query]` |
| `input_file` | workspace | workspace; `--file file_id:path` for file ids | `run -f FILE` attaches; workspace | workspace | `@file` in the message | workspace | `@file` in the message | workspace (`--cwd`) | workspace | workspace |
| `input_image` | `chat --image FILE` | workspace (its Read tool opens images) | `run -f FILE` | workspace | `@image.png` | `-i FILE` | `@image.png` | workspace | workspace | workspace |
| prior items in `input` (`function_call_output`, replayed messages) | — (history is the session) | — | — | — | — | — | — | — | — | — |
| `instructions` | AGENTS.md / SOUL.md (auto-injected) | `--append-system-prompt`; `--system-prompt` replaces | AGENTS.md | `-s, --system` (replaces) | `--append-system-prompt`; `--system-prompt` replaces | AGENTS.md | `--append-system-prompt`; `--system-prompt` replaces | AGENTS.md in `--cwd` | AGENTS.md | AGENTS.md (`/init` creates it) |
| `previous_response_id` / `conversation` | `--resume SESSION` | `--resume SID`; `--fork-session` to branch | `run -s SESSION`; `--continue`; `--fork` | `--id SESSION` | `--session ID`, `--session-id ID`; `--fork` | `exec resume SESSION_ID` | `--resume ID`; `--continue` | `--state-dir DIR` reuses state; `agent --session-id ID` via the gateway | `--session-id ID` | `--resume`, `--session`, `--continue`; `--fork-session` |
| `model` | `-m MODEL`, `--provider` | `--model`; `--fallback-model` | `run -m provider/model` | `-m MODEL`, `-P provider` | `--model provider/id`, `--provider` | `-m MODEL` | `--model`, `--provider` | `--model provider/model`; `--fallback` | profile `--patch` overlay | `-m MODEL` |
| `reasoning.effort` | — | `--effort low…max` | `run --variant high/max/minimal` | `--thinking none…xhigh` | `--thinking off…max`, or `model:level` | `-c model_reasoning_effort=…` | `--thinking off…auto` | `--thinking off…ultra` | — | `--effort low/medium/high` |
| `reasoning.summary` | — | — | `run --thinking` shows blocks | — | — | `-c model_reasoning_summary=…` | `--print-thoughts` | — | — | — |
| `text.format` json_schema | — | `--json-schema SCHEMA` | — | — | — | `--output-schema FILE` | — | — | — | — |
| `text.verbosity` | — | — | — | — | — | `-c model_verbosity=…` | — | — | — | — |
| `tools` of type `mcp` | `mcp_servers` in config.yaml (`hermes mcp`) | `--mcp-config JSON`, `--strict-mcp-config` | `kilo mcp` config | `cline mcp` config | config (`pi mcp`); `--no-mcp` | `[mcp_servers.*]` in config.toml, via `-c` | config | `openclaw mcp` config | profile | `command-code mcp` config |
| `tools` of type `shell`, `apply_patch`, `function` (the harness's own tools) | `-t TOOLSETS`; `-s SKILLS` | `--tools`, `--allowedTools`, `--disallowedTools`; `--restricted` | `--agent` | `--auto-approve`, `-y` | `--tools`, `--exclude-tools`, `--no-tools`; `--skill` | `-s read-only/workspace-write/danger-full-access`; `--search` | `--tools`, `--no-tools`; `--skills` | `--code-mode direct/auto/code` | profile | `--tools-all`, `--tools-enable`; `--skill` |
| `tool_choice` | — | — | — | — | — | — | — | — | — | — |
| `parallel_tool_calls` | — | — | — | — | — | — | — | — | — | — |
| `max_tool_calls` | `chat --max-turns N` | — | — | `--retries COUNT` (consecutive mistakes) | — | — | — | — | — | `--max-turns N` |
| `max_output_tokens` | — | — (`--max-budget-usd` caps spend) | — | — | — | — | — | — | — | — |
| `temperature`, `top_p`, `top_logprobs` | — | — | — | — | — | — | — | — | — | — |
| `store: false` | — | `--no-session-persistence` | — | — | `--no-session` | `--ephemeral` | `--no-session` | (`exec` is isolated by default) | — | `--no-session` |
| `stream` | — (text) | `--output-format stream-json`; `--include-partial-messages` | `--format json` | `--json` | `--mode json` | `--json` | `--mode json` | `--json` | `--json` | `--output-format json` |
| `truncation` / `context_management` | — | `--autocompact auto\|tokens` | — | `--compaction agentic/basic/off` | — | config `model_auto_compact_token_limit` | — | — | — | — |
| `service_tier` | — | — | — | — | — | — | `--service-tier` | — | — | — |
| `access_programs.cyber` | — | — | — | — | — | `--cyber-access-program` (same literals) | — | — | — | — |
| `background` | — | — | — | — | — | — | — | — | — | — |
| `metadata` | — (echoed by the platform) | — | — | — | — | — | — | — | — | — |
| `include`, `prompt`, `prompt_cache_*`, `safety_identifier`, `user`, `moderation`, `stream_options` | — | — | — | — | — | — | — | — | — | — |

Three tiers follow from the table. Every harness takes the prompt, files, a model and a
continuation, and all but Hermes and DeepSeek Harness take a JSON event stream, so the platform
maps those itself and a package only supplies the flag. Some harnesses take `reasoning.effort`
(eight of ten), MCP servers (all, each through its own config), a JSON schema for the answer (two),
a turn cap (three), so a package's manifest declares which of these it accepts and the platform
refuses the rest with the API's error. No harness takes `temperature`, `top_p`, `tool_choice`,
`parallel_tool_calls`, caller-run function tools, prompt templates, prompt caching or service
tiers, so the platform refuses those for every harness: a coding agent owns its sampling and its
tool loop.

## The response schema

The schema is OpenAI's. The platform builds every response from the SDK's own types.

- Reference: [The response object](https://platform.openai.com/docs/api-reference/responses/object)
- Output items: [Output items](https://platform.openai.com/docs/api-reference/responses/object#responses/object-output)
- Streaming: [Streaming events](https://platform.openai.com/docs/api-reference/responses-streaming)
- The SDK's definitions, openai-python 3.35.1: [`response.py`](https://github.com/openai/openai-python/blob/v3.35.1/src/openai/types/responses/response.py), [`response_output_item.py`](https://github.com/openai/openai-python/blob/v3.35.1/src/openai/types/responses/response_output_item.py), [`response_stream_event.py`](https://github.com/openai/openai-python/blob/v3.35.1/src/openai/types/responses/response_stream_event.py)
- The Python types the platform builds with: `openai.types.responses.Response`, `ResponseOutputItem`, `ResponseStreamEvent`

A response has: `id`, `object`, `created_at`, `status`, `error`, `incomplete_details`, `model`,
`output`, `usage`, `instructions`, `previous_response_id`, `metadata`, `store`, `text`,
`reasoning`, `tools`, `tool_choice`, `parallel_tool_calls`, `truncation`, `temperature`, `top_p`,
`max_output_tokens`, `user`, `service_tier`, `safety_identifier`, `prompt_cache_key`,
`background`, `conversation`. `output` is a list of items in the order they happened: `message`,
`reasoning`, `shell_call` and `shell_call_output`, `apply_patch_call` and
`apply_patch_call_output`, `function_call` and `function_call_output`, `mcp_call`,
`mcp_list_tools`, `web_search_call`, `file_search_call`, `code_interpreter_call`,
`image_generation_call`, `computer_call`, `local_shell_call`, `custom_tool_call` and their outputs.
Streaming sends the same object as events: `response.created`, `response.output_item.added`,
`response.output_text.delta`, `response.function_call_arguments.delta`,
`response.output_item.done`, `response.completed`, and the rest.

## What each harness can emit

What the CLI's own help offers as output, which is what a package has to map from:

| Harness | Structured output | Final answer | Session id for a later resume | Usage |
|---|---|---|---|---|
| Hermes Agent | none; `chat -q` prints a text transcript | `-z` prints the final text alone | `chat` prints `Session: <id>` on exit; `--pass-session-id` puts it in the prompt | `-z --usage-file PATH` writes a JSON report (tokens, cost, model, api_calls) |
| Claude Code | `--output-format stream-json`: `system` (init, `session_id`), `assistant` and `user` messages with `thinking`, `text`, `tool_use` and `tool_result` blocks, `result`; `--include-partial-messages` adds text deltas; `--output-format json` gives one result object | `result.result` | `session_id` on the `system` init and `result` events | `result.usage` (input, output, cache read, cache write) |
| Kilo Code | `run --format json`: raw JSON events; `kilo export SID` dumps the whole session as JSON | the last assistant event | the session id in the events and in `export` | `kilo stats` (tokens and cost), `export` |
| Cline | `--json`: the messages as JSON | the last assistant message | the session id (`--id` takes it back; `history` lists them) | in the messages, where the provider reports it |
| Pi | `--mode json`: an event stream; `--mode rpc`; the session `.jsonl` file on disk; `--export` to HTML | the last assistant message | `--session-id` sets it; the session file's id | in the session file |
| Codex | `--json`: JSONL events `thread.started`, `turn.started`, `item.started` / `item.completed` for `agent_message`, `reasoning`, `command_execution`, `file_change`, `mcp_tool_call`, `web_search`, then `turn.completed`; `-o FILE` writes the last message; `--output-schema` shapes it | `agent_message`, or the `-o` file | `thread_id` on `thread.started` | `turn.completed.usage` |
| Oh My Pi | `--mode json` / `rpc` / `rpc-ui`: an event stream; `--print-thoughts` includes thinking; the session `.jsonl`; `--export` to HTML | the last assistant message | the session file's id (`--resume` takes it back) | `omp stats`, `omp usage`, the session file |
| OpenClaw | `agent exec --json`: "the stable agent-exec JSON envelope"; `agent --json` for gateway runs | the envelope's reply | `--state-dir` keeps state; `agent --session-id` on the gateway | in the envelope, where the provider reports it |
| DeepSeek Harness | `--json`: newline-delimited run events; without it, the final message alone on stdout, diagnostics on stderr | stdout | `--session-id` adopts a persisted session; the id is in the events | in the events, where the provider reports it |
| Command Code | `--output-format json`: an NDJSON event stream plus a final result line; exit 8 when `--max-turns` is hit | the final result line | `/session-file` shows it; `--session` takes a transcript path or id | in the result line, where reported |

Claude Code's and Codex's event shapes have been recorded and mapped on this repository (see
`app.py`); the others are confirmed to exist by their help and are mapped once a run is recorded.

## Response fields from harness output

Where each response field comes from. "platform" means the platform fills it and no harness is
involved. For the `output` items, a cell names the harness event that carries that item's data.

| Response field | Hermes Agent | Claude Code | Kilo Code | Cline | Pi | Codex | Oh My Pi | OpenClaw | DeepSeek Harness | Command Code |
|---|---|---|---|---|---|---|---|---|---|---|
| `id`, `object`, `created_at`, `model`, `previous_response_id`, `instructions`, `store`, `metadata` (the caller's keys) | platform | platform | platform | platform | platform | platform | platform | platform | platform | platform |
| `status` (`completed`, `failed`, `incomplete`, `cancelled`) | exit code | `result.subtype`, `is_error`, exit code | last event, exit code | last message, exit code | last event, exit code | `turn.completed` / `turn.failed`, exit code | last event, exit code | envelope status, exit code | last event, exit code | result line; exit 8 means `incomplete` (turn cap) |
| `error` | stderr on failure | `result.result` when `is_error` | event | message | event | `turn.failed` / `error` event | event | envelope error | stderr / event | result line |
| `incomplete_details` | — | — | — | `--timeout` hit | — | — | `--max-time` hit | `--timeout` hit | — | `--max-turns` hit (exit 8) |
| `output`: `message` (`output_text`) | the `-z` text, or the transcript's boxed reply | `assistant` `text` blocks; `result.result` | assistant text events | assistant message | assistant text events | `agent_message` items | assistant text events | the envelope's reply | the final message / message events | the final result line; text events |
| `output`: `reasoning` | — | `thinking` blocks | with `--thinking` | — | thinking events (level by `--thinking`) | `reasoning` items (`model_reasoning_summary`) | with `--print-thoughts` | — | — | — |
| `output`: `shell_call` + `shell_call_output` | tool lines in the transcript (text) | `tool_use` Bash + `tool_result` | tool events | tool messages | tool events (`bash`) | `command_execution` items (command, output, exit code) | tool events (`bash`) | tool records in the envelope | run events | tool events |
| `output`: `apply_patch_call` + `apply_patch_call_output` | tool lines (text) | `tool_use` Write / Edit + `tool_result` | tool events | tool messages | tool events (`edit`, `write`) | `file_change` items (path, kind) | tool events (`edit`, `write`) | tool records | run events | tool events |
| `output`: `function_call` + `function_call_output` (any other tool) | tool lines (text) | any other `tool_use` + `tool_result` | tool events | tool messages | tool events | other items | tool events | tool records | run events | tool events |
| `output`: `mcp_call` | MCP tool lines (text) | `tool_use` named `mcp__server__tool` | MCP tool events | MCP tool messages | `mcp__server__tool` events | `mcp_tool_call` items | MCP tool events | MCP tool records | run events | MCP tool events |
| `output`: `web_search_call` | — | `tool_use` WebSearch | — | — | — | `web_search` items (`--search`) | — | — | — | — |
| `output_text` (SDK convenience) | the `message` texts joined | same | same | same | same | same | same | same | same | same |
| `usage` (`input_tokens`, `output_tokens`, `cached_tokens`, `total_tokens`) | `--usage-file` report | `result.usage` | `stats` / events | messages, if reported | session file | `turn.completed.usage` | `stats` / session file | envelope, if reported | events, if reported | result line, if reported |
| `metadata.session_id` (platform addition: what to resume later) | `Session:` line | `session_id` | session id | session id | session id | `thread_id` | session id | state dir / session id | session id | session id |
| stream events (`response.output_text.delta` and the rest) | none; the response arrives whole | from `stream-json`, deltas with `--include-partial-messages` | from `--format json` events | from `--json` messages | from `--mode json` events | from `--json` events | from `--mode json` events | none; `exec --json` is one envelope | from `--json` events | from the NDJSON stream |

Two things decide how good a harness's responses can be. A JSON event stream gives a package the
tool calls and their results as they happen, so `output` holds the whole turn and the client sees
it stream; eight of the ten have one. A text-only CLI gives a package the final answer and, with
a parser, the tool lines; Hermes is the one of the ten whose one-shot mode prints the answer
alone, and its `--usage-file` is the one place among the ten where the token counts come as a
separate, documented file.
