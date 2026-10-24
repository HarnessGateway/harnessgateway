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

**Around the run**, the package seeds the workspace and the home directory. A CLI reads far more
than its flags: instruction files, rules, MCP server lists, skills, hooks, subagent definitions,
permission settings and provider configuration, all from well-known paths. A package that writes
those files turns a general coding agent into an organization's own agent, and the platform does
nothing but put the files where the CLI looks and run the command. The [seeds](#seeds-what-a-package-puts-in-the-workspace-and-home)
section lists every such path for the ten harnesses.

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
cell in the tables that follow was taken from the CLI's own `--help`, run on the version named,
and then checked against the harness's official documentation (the pages are listed under
[Sources](#sources)). Where the two disagree, or where only one of them has the fact, the cell
says so: *docs* marks something the documentation describes that the help does not, *source*
marks something found only in the harness's published source code, and "not in docs" marks a
help flag the documentation does not mention.

| Harness | Version | Non-interactive invocation | Documentation |
|---|---|---|---|
| Hermes Agent | 0.19.0 | `hermes -z "PROMPT"` (final text only) or `hermes chat -q "PROMPT" --format stream-json` | hermes-agent.nousresearch.com/docs |
| Claude Code | 2.1.310 | `claude -p "PROMPT" --output-format stream-json --verbose --dangerously-skip-permissions` | code.claude.com/docs |
| Kilo Code | 7.8.12 | `kilo run "PROMPT" --format json --auto` | kilo.ai/docs |
| Cline | 3.0.73 | `cline "PROMPT" --json -y` | docs.cline.bot |
| Pi | 1.3.0 | `pi -p "PROMPT" --mode json` | github.com/earendil-works/pi (docs at tag v1.3.0) |
| Codex | 0.163.0 | `codex exec "PROMPT" --json --skip-git-repo-check` | learn.chatgpt.com/docs (developers.openai.com/codex redirects there) |
| Oh My Pi | 18.12.0 | `omp -p "PROMPT" --mode json --approval-mode yolo` | omp.sh/docs, github.com/can1357/oh-my-pi/docs |
| OpenClaw | 2026.10.6 | `openclaw agent exec "PROMPT" --json` | docs.openclaw.ai |
| DeepSeek Harness | 0.2.0-rc.2 | `dsh --profile headless "PROMPT" --json` | github.com/deepseek-ai/deepseek-harness (READMEs at tag dsh-v0.2.0-rc.2) |
| Command Code | 1.88.0 | `command-code -p "PROMPT" --output-format json --yolo` | commandcode.ai/docs |

How each harness is pointed at a provider, since the request never carries a URL or a key:

- **Claude Code** reads `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`
  for a bearer header) from the environment; `ANTHROPIC_DEFAULT_HAIKU_MODEL` and its siblings
  pin the models the aliases resolve to, and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` keeps
  everything else off the wire. The same keys can sit in `env` in a settings file.
- **Codex** takes `-c model_provider=ID` and a `[model_providers.ID]` table (`base_url`,
  `env_key`, `wire_api = "responses"`) in `config.toml`, which `-c key=value` can also write at
  invocation time; `openai_base_url` redirects the built-in provider.
- **Hermes** takes `model:` and `providers:` entries in `~/.hermes/config.yaml` (`base_url`,
  `transport chat_completions`, `anthropic_messages` or `codex_responses`), or
  `ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL`; `chat` also takes `--base-url` and `--api-key`.
- **Kilo Code** takes a `provider.<id>` block in `kilo.jsonc` (`npm` adapter, `options.baseURL`,
  `models`) and `KILO_API_KEY`-style environment overrides.
- **Cline** takes `cline auth -p ID -k KEY -m MODEL -b URL`, which writes `providers.json`; the
  base URL applies to its OpenAI-compatible providers only, and there is no Anthropic-compatible
  base URL (*source*).
- **Pi** and **Oh My Pi** take a providers file (`~/.pi/agent/models.json`,
  `~/.omp/agent/models.yml`) whose entries name an `api` (`openai-completions`,
  `openai-responses`, `anthropic-messages`, `google-generative-ai`), a `baseUrl` and an `apiKey`
  that may be an environment variable name.
- **OpenClaw** takes `models.providers.<id>` (`baseUrl`, `api`, `apiKey: "${ENV}"`) in
  `openclaw.json`, which `--config-override '<json>'` can supply per run.
- **DeepSeek Harness** takes configuration rows (`agent-default-model`, `llm-deepseek`,
  `llm-pi-ai`) in a `--patch` overlay; `DEEPSEEK_API_KEY` and `DEEPSEEK_BASE_URL` feed the
  default row.
- **Command Code** takes `~/.commandcode/providers.json` (`baseURL`, `api` as chat completions,
  responses or Anthropic messages, `apiKey: "$ENV"`); `COMMAND_CODE_API_KEY` authenticates its
  own backend.

## Request fields to invocation

How each request field reaches each harness. Where a harness offers more than one way, the cell
lists them all, flags first, then files the package writes, then environment. "workspace" means
the platform writes the file into the directory the CLI runs in and the prompt names it. "—" means
neither the help nor the documentation offers anything for the field; the platform refuses it for
that harness. Flags are the CLI's own.

| Request field | Hermes Agent | Claude Code | Kilo Code | Cline | Pi | Codex | Oh My Pi | OpenClaw | DeepSeek Harness | Command Code |
|---|---|---|---|---|---|---|---|---|---|---|
| `input` text | `-z PROMPT`; `chat -q PROMPT`; `chat --query-file PATH` (`-` for stdin) | positional with `-p`; piped stdin; `--input-format stream-json` for a message stream | `run [message..]`; piped stdin (*source*); `--command NAME` runs a saved command | positional prompt; piped stdin (both are concatenated) | positional with `-p`; piped stdin is prepended | positional, or stdin (`-`); with both, stdin is appended as context | positional with `-p`; piped stdin; further positionals become later turns | positional; `--message-file PATH` (`-` for stdin) | positional task; stdin when omitted or `-` | `-p [query]`; piped stdin |
| `input_file` | workspace (the file toolset); `--in DIR` sets the directory | workspace, named by path; or stdin | `run -f FILE` (repeatable); workspace | `@path` in the prompt attaches it; workspace | `@path` in the prompt; workspace | workspace; or stdin as context | `@path` in the prompt; workspace | workspace (`--cwd`) | workspace | `@path` in the prompt puts the path in the message; workspace |
| `input_image` | `chat --image FILE` (not on `-z`) | workspace (the Read tool opens PNG, JPEG, GIF, WebP); a base64 image block over `--input-format stream-json` | `run -f FILE` (PNG, JPEG, GIF, WebP, PDF; *source*) | `@image.png` in the prompt | `@image.png` in the prompt | `-i FILE[,FILE]` | `@image.png` in the prompt | workspace (image-capable models only) | workspace (not documented) | `@image.png` in the prompt |
| prior items in `input` (`function_call_output`, replayed messages) | — (history is the session) | — | — | — | — | — | — | — | — | — |
| `instructions` | context file in the workspace, first match wins: `.hermes.md`/`HERMES.md`, `AGENTS.override.md`, `AGENTS.md`, `CLAUDE.md`, `.cursorrules`; `agent.system_prompt` in config.yaml; `SOUL.md` in `$HERMES_HOME` only | `--append-system-prompt[-file]`; `--system-prompt[-file]` replaces; `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md`, `.claude/rules/*.md`; `AGENTS.md` and `.claude/AGENTS.md` when no CLAUDE.md exists (setting `instructionFiles`) | `AGENTS.md` (then `AGENT.md`), `CLAUDE.md`; `instructions` list in `kilo.jsonc` (paths, globs, URLs); `agent.<name>.prompt` | `-s TEXT` replaces the whole system prompt (rules dropped); `.clinerules/*.md`, `.cline/rules/*.md`, `AGENTS.md`, `.cursorrules` | `--append-system-prompt TEXT\|FILE`; `--system-prompt TEXT\|FILE` replaces; `.pi/SYSTEM.md`, `.pi/APPEND_SYSTEM.md`; `AGENTS.override.md`, `AGENTS.md`, `CLAUDE.md` in cwd and parents | `AGENTS.override.md` or `AGENTS.md`, project root down to cwd, concatenated (32 KiB cap); `-c developer_instructions=TEXT`; `-c model_instructions_file=PATH` replaces | `--append-system-prompt TEXT\|FILE`; `--system-prompt TEXT\|FILE` replaces the default block only; `--system-prompt-template FILE`; `.omp/SYSTEM.md`, `.omp/APPEND_SYSTEM.md`, `.omp/RULES.md`; `AGENTS.md`, `CLAUDE.md` | `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md` in the workspace, injected every run; no system-prompt flag or key (*docs*: "OpenClaw owns the system prompt") | `AGENTS.md`, `CLAUDE.md`, `AGENTS.local.md`, `CLAUDE.local.md` (64 KiB cap); `system-prompt` row (`personaPrefix`, `personaSuffix`) in a `--patch` | `AGENTS.md` or `.commandcode/AGENTS.md`, plus the user's `~/.commandcode/AGENTS.md`; no system-prompt flag or key; `CLAUDE.md` is not read |
| `previous_response_id` / `conversation` | `--resume ID\|latest`; `--continue` | `--resume ID\|NAME\|PATH`; `--continue`; `--fork-session` to branch; `--session-id UUID` names a new one | `run -s ID`; `--continue`; `--fork` | `--id ID` (*docs*; *source*: honoured in interactive mode only, a `--json` run always starts a new session) | `--session ID\|PATH`; `--session-id ID` (creates it if absent); `-c`; `--fork` | `exec resume THREAD_ID [PROMPT]`; `--last`; `exec fork THREAD_ID` | `--resume ID\|PATH`; `--continue`; `--fork`; `--session-dir DIR` | no resume flag on `agent exec` (*docs*); `--state-dir DIR` keeps state between runs; `agent --session-id ID` through the gateway | `--session-id ID` (same cwd and profile) | `--resume ID\|NAME`; `--continue` (in `-p`: the latest headless session); `--session PATH\|ID`; `--fork-session` |
| `model` | `-m MODEL`, `--provider ID`; `model:` in config.yaml; `HERMES_INFERENCE_MODEL` | `--model NAME`; `--fallback-model A,B`; `ANTHROPIC_MODEL`; `model` in settings | `run -m provider/model`; `model` in `kilo.jsonc`; `KILO_PROVIDER` | `-m MODEL`, `-P PROVIDER`, `-k KEY`; `providers.json` | `--model provider/id[:level]`, `--provider`, `--api-key`; `models.json` | `-m MODEL`; `-c model_provider=ID`; `model` in config.toml | `--model id\|role[:level]`; `--provider`; `modelRoles` in config.yml | `--model provider/model`; `--fallback` (repeatable); `agents.defaults.model` | `agent-default-model` row (`provider`, `model`) in a `--patch` | `-m MODEL` (`--list-models`); `model` in settings |
| `reasoning.effort` | `--reasoning LEVEL` (*docs*); `agent.reasoning_effort` in config.yaml | `--effort low\|medium\|high\|xhigh\|max\|ultracode`; `CLAUDE_CODE_EFFORT_LEVEL` outranks the flag | `run --variant NAME` (per-model variants, e.g. `high`, `max`, `minimal`) | `--thinking none\|low\|medium\|high\|xhigh` | `--thinking off\|minimal\|low\|medium\|high\|xhigh\|max`; `model:level` | `-c model_reasoning_effort=low\|medium\|high\|xhigh\|max\|ultra` | `--thinking off\|minimal\|low\|medium\|high\|xhigh\|max\|auto` | `--thinking off\|minimal\|low\|medium\|high\|xhigh\|adaptive\|max\|ultra` | `reasoningEffort` on the `agent-default-model` row; `llm-deepseek` row `reasoningEffort off\|low\|high\|max` (*docs*) | `--effort low\|medium\|high` (model dependent; `xhigh`, `max` in settings) |
| `reasoning.summary` | — | — (`showThinkingSummaries` is interactive only) | `run --thinking` shows blocks | — | — | `-c model_reasoning_summary=auto\|concise\|detailed\|none` | `--print-thoughts` (text mode only) | — | — | — |
| `text.format` json_schema | — | `--json-schema SCHEMA` (with `--output-format json`) | — | — | — | `--output-schema FILE` | — | — | — | — |
| `text.verbosity` | — | — | — | — | — | `-c model_verbosity=low\|medium\|high` | — | — | — | — |
| `tools` of type `mcp` | `mcp_servers:` in config.yaml (`hermes mcp add`) | `--mcp-config FILE\|JSON`, `--strict-mcp-config`; `.mcp.json` in the workspace; `~/.claude.json` | `mcp` key in `kilo.jsonc` (`kilo mcp add`) | `~/.cline/data/settings/cline_mcp_settings.json` (`cline mcp add`) | `.pi/mcp.json`, `~/.pi/agent/mcp.json` (`pi mcp add`); `--no-mcp` | `[mcp_servers.NAME]` in config.toml, or `-c` (`codex mcp add`) | `.omp/mcp.json`, `~/.omp/agent/mcp.json`; also reads `.claude/.mcp.json`, `.cursor/mcp.json`, `.codex/config.toml` | `mcp.servers.NAME` in openclaw.json, or `--config-override` (`openclaw mcp add`) | `@deepseek-ai/dsh-mcp-client` row in a `--patch` | `.mcp.json` in the project, `~/.commandcode/mcp.json`, `~/.commandcode/projects/<slug>/mcp.json` (`cmd mcp add`) |
| `tools` of type `shell`, `apply_patch`, `function` (the harness's own tools and approvals) | `-t TOOLSETS`; `-s SKILLS`; `--yolo` (set by `-z`); `approvals.deny` always wins | `--tools`, `--allowedTools`, `--disallowedTools`; `--permission-mode`; `--dangerously-skip-permissions`; `--restricted` | `--agent NAME`; `--auto` (without it, headless runs reject every prompt and exit 1); `permission` map in `kilo.jsonc` | `--auto-approve BOOL`; `-y` runs a reduced tool set with hooks off (README; not in the reference); a `PreToolUse` hook gates commands | `--tools`, `--exclude-tools`, `--no-tools`; `--skill`; `-a` trusts the folder | `-s read-only\|workspace-write\|danger-full-access`; `-c approval_policy=never`; `-c web_search=live` (`--search` and `-a` exist on `codex`, not on `codex exec`); `--add-dir` | `--tools`, `--no-tools`; `--skills`; `--approval-mode always-ask\|always-ask-cached\|write\|yolo` | `--code-mode direct\|auto\|code`; `tools.allow`, `tools.deny`, `tools.exec.mode` via `--config-override` | `DSH_PERMISSION_MODE read-only\|workspace-write\|danger-full-access`; tool rows set `disabled: true` in a `--patch` | `--tools-all`, `--tools-enable`; `--yolo` (in `-p`, `--permission-mode yolo` alone does not unblock writes); `permissions.allow\|ask\|deny` in settings; `--skill` |
| `tool_choice` | — | — | — | — | — | — | — | — | — | — |
| `parallel_tool_calls` | — | — | — | — | — | — | — | — | — | — |
| `max_tool_calls` | `chat --max-turns N`; `chat --run-budget SECONDS` | `--max-turns N`; `--max-budget-usd` | — | `--retries N` counts consecutive mistakes, not calls; `-t SECONDS` | — | — (`features.rollout_budget` is a token budget) | — (`--max-time`) | — (`--timeout`) | — | `--max-turns N` (model round trips; exit 8) |
| `max_output_tokens` | — | `CLAUDE_CODE_MAX_OUTPUT_TOKENS` (no flag) | — | — | `maxTokens` per model in `models.json` | — | `maxTokens` per model in `models.yml` | `params.maxTokens` per model in config | `maxTokens` on the `llm-deepseek` row | `maxOutput` per model in `providers.json` |
| `temperature`, `top_p`, `top_logprobs` | — | — | `temperature`, `top_p` per agent in `kilo.jsonc` | — | `samplingParams` in `models.json` | — | — | `params.temperature`, `params.topP` in config | — | — |
| `store: false` | — (everything persists to `state.db`) | `--no-session-persistence` | — (`KILO_DB=:memory:` is the nearest) | — (`--data-dir` isolates the state) | `--no-session` | `--ephemeral` | `--no-session` | omit `--state-dir` (the temporary state is removed after the run) | — | `--no-session` |
| `stream` | `chat -q --format stream-json` (JSONL; not on `-z`) | `--output-format stream-json --verbose`; `--include-partial-messages` for deltas | `--format json` | `--json` (`-v` adds `run_start`) | `--mode json` | `--json` | `--mode json` | `--json` is one envelope at the end; no event stream | `--json` | `--output-format json` |
| `truncation` / `context_management` | — | `--autocompact auto\|TOKENS` | `compaction` in `kilo.jsonc` | `--compaction agentic\|basic\|off` | `compaction.*` in settings.json | `-c model_auto_compact_token_limit=N` | `compaction.*` in config.yml | — | — | — |
| `service_tier` | `agent.service_tier` in config.yaml | — | — | — | — | — | `--service-tier none\|auto\|default\|flex\|scale\|priority\|ultrafast` | — | — | — |
| `access_programs.cyber` | — | — | — | — | — | `--cyber-access-program standard\|daybreak_blue\|daybreak_red` (not in docs; *source*) | — | — | — | — |
| `background` | — | — | — | — | — | — | — | — | — | — |
| `metadata` | — (echoed by the platform) | — | — | — | — | — | — | — | — | — |
| `include`, `prompt`, `prompt_cache_*`, `safety_identifier`, `user`, `moderation`, `stream_options` | — | — | — | — | — | — | — | — | — | — |

Three tiers follow from the table. Every harness takes the prompt, files, a model, a continuation
and a reasoning level, and all but OpenClaw emit a JSON event stream, so the platform maps those
itself and a package only supplies the flag or the file. Some harnesses take MCP servers (all ten,
each through its own file or config row), a JSON schema for the answer (two), a turn cap (three),
so a package's manifest declares which of these it accepts and the platform refuses the rest with
the API's error. No harness takes `tool_choice`, `parallel_tool_calls`, caller-run function tools,
prompt templates or prompt caching from a flag, and sampling knobs exist only as per-model config
in four of them, so the platform refuses those for every harness: a coding agent owns its sampling
and its tool loop.

Two cells deserve a warning. Cline's `--id` resumes a session in the documentation, but the source
of 3.0.73 passes it to the interactive path only, so a `--json` run cannot continue a conversation
until that changes. OpenClaw's `agent exec` has no way to name a prior session at all; continuity
means reusing a `--state-dir`, and a later run picks up whatever that directory holds.

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

## What each harness emits

What the CLI produces, by the names its documentation gives, which is what a package maps from.
Where the documentation stops short of the field names, the cell says what it does document and
marks the rest *source*.

| Harness | Structured output | Final answer | Session id for a later resume | Usage |
|---|---|---|---|---|
| Hermes Agent | `chat -q --format stream-json`: JSONL records `system` (init: `model`, `session_id`), `text` (delta), `tool_use` (`name`, `input`), `tool_result` (`name`, `output`, `duration_ms`, `is_error`), `result` (`session_id`, `exit_code`, `text`, `tokens`, `duration_ms`, `error`); `-z` prints the final text alone | `result.text`; the `-z` stdout | `result.session_id`; `chat` prints `Session: <id>` on exit | `result.tokens`; `-z --usage-file PATH` writes a JSON report (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `estimated_cost_usd`, `api_calls`, `model`, `session_id`, `turn_exit_reason`) |
| Claude Code | `--output-format stream-json`: `system` (`subtype: init`, `session_id`, `tools`, `mcp_servers`, `model`), `assistant` and `user` messages with `thinking`, `text`, `tool_use` and `tool_result` blocks, `result` (`subtype`, `is_error`, `num_turns`, `result`, `usage`, `total_cost_usd`, `structured_output`); `stream_event` deltas with `--include-partial-messages`; `--output-format json` gives one result object | `result.result`; `result.structured_output` with `--json-schema` | `session_id` on `system` init and `result` | `result.usage` (input, output, cache read, cache creation), `result.modelUsage` |
| Kilo Code | `run --format json`: one object per line, `{type, timestamp, sessionID, part}` with `type` in `step_start`, `step_finish`, `text`, `reasoning` (with `--thinking`), `tool_use`, `error`; `text` and `tool_use` arrive complete, not as deltas (*source*; the docs say "raw JSON events" and that a failed run ends with an `error` record); `kilo export ID` dumps the session as JSON | the last `text` part | `sessionID` on every event | `step_finish` part (*source*); `kilo stats` |
| Cline | `--json`: NDJSON `{type, ts}` lines: `agent_event` wrapping `content_start`, `content_update`, `content_end` (`contentType` text, reasoning or tool), `iteration_start`, `iteration_end`, `usage`, `notice`, `done` (`reason`, `text`), `error`; then `run_result` (`finishReason`, `iterations`, `usage`, `durationMs`, `model`, `text`); `run_aborted` | `run_result.text` | `run_start.sessionId`, emitted only with `-v`; otherwise `cline history --json` | `usage` events and `run_result.usage` (`inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`, `cost`) |
| Pi | `--mode json`: a `session` header, then `agent_start`, `turn_start`, `message_start`, `message_update` (`assistantMessageEvent`: `text_delta`, `thinking_delta`, `toolcall_start`, `toolcall_end`), `message_end`, `tool_execution_start`, `tool_execution_end` (`toolName`, `args`, `result`, `isError`), `turn_end`, `agent_end`, `agent_settled`; `--mode rpc` for a command channel | the last assistant `message_end` | `id` on the `session` header; `--session-id` sets it | `message_update.usage` and `message.usage` (`input`, `output`, `cacheRead`, `cacheWrite`, `cost`) |
| Codex | `--json`: JSONL `thread.started` (`thread_id`), `turn.started`, `item.started`, `item.updated`, `item.completed` with items `agent_message`, `reasoning`, `command_execution` (`command`, `aggregated_output`, `exit_code`, `status`), `file_change` (`changes[].path`, `kind`), `mcp_tool_call` (`server`, `tool`, `arguments`, `result`), `web_search`, `todo_list`, `error`; `turn.completed` (`usage`); `turn.failed`; `-o FILE` writes the last message; `--output-schema` shapes it | the last `agent_message.text`, or the `-o` file | `thread_id` on `thread.started` | `turn.completed.usage` (`input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`) |
| Oh My Pi | `--mode json`: a session header (absent with `--no-session`), then `agent_start`, `turn_start`, `message_start`, `message_update` (deltas via `assistantMessageEvent`), `message_end` (the complete message), `tool_execution_start`, `tool_execution_update`, `tool_execution_end`, `turn_end`, `agent_end`, plus compaction, retry and notice events; `--mode rpc` | the last assistant `message_end` | `id` on the session header | `usage` on the assistant message in `message_end` (`input`, `output`, `cacheRead`, `cacheWrite`, `cost`) |
| OpenClaw | `agent exec --json`: one envelope `{ok, status, final, payloads[].text, usage, costUsd, toolSummary, model, provider, sessionId, error}`; plain mode prints the final text; no event stream | `payloads[].text` | `sessionId` (but see above: `exec` cannot take it back) | `usage` (`input`, `output`, `total`), `costUsd` |
| DeepSeek Harness | `--json`: NDJSON `session` (`sessionId`, `cwd`), `status` (`turn_start`, `step_start`, `step_end` with `usage`, `turn_end` with `reason`), `text`, `thinking`, `tool_call` (`callId`, `tool`, `input`), `tool_result` (`callId`, `status`, `result`), `error`, `final` (`text`); text arrives per committed block, not per token; without `--json`, the final text on stdout | `final.text` | `session.sessionId` | `step_end.usage` (`inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`, `reasoningTokens`) |
| Command Code | `--output-format json`: NDJSON `{"type":"event","event":{...}}` lines (the docs name `tool_running`; the package also emits `text_delta`, `thinking_*`, `tool_completed`, `tool_errored`, `model_request_end` with usage, *source*), then one `{"type":"result","subtype","sessionId","stopReason","usage","durationMs","finalText","error"}` line | `result.finalText` | `result.sessionId` | `result.usage` (`inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`); no cost |

Claude Code's and Codex's event shapes have been recorded and mapped on this repository (see
`app.py`); the others are mapped once a run is recorded.

## Response fields from harness output

Where each response field comes from. "platform" means the platform fills it and no harness is
involved. For the `output` items, a cell names the harness event that carries that item's data.

| Response field | Hermes Agent | Claude Code | Kilo Code | Cline | Pi | Codex | Oh My Pi | OpenClaw | DeepSeek Harness | Command Code |
|---|---|---|---|---|---|---|---|---|---|---|
| `id`, `object`, `created_at`, `model`, `previous_response_id`, `instructions`, `store`, `metadata` (the caller's keys) | platform | platform | platform | platform | platform | platform | platform | platform | platform | platform |
| `status` (`completed`, `failed`, `incomplete`, `cancelled`) | `result.exit_code`; `-z` exits 0 with text, 1 without, 2 failed or budget hit, 130 interrupted | `result.subtype`, `is_error`; exit code | final `error` record; exit 0, 1 (error or rejected permission), 124 (timeout) | `run_result.finishReason` (`completed`, `max_iterations`, `mistake_limit`, `error`, `aborted`); exit 0 or 1 (*source*) | `agent_end`, `agent_settled`; `message.stopReason` (`stop`, `length`, `error`, `aborted`) | `turn.completed` or `turn.failed` | `agent_end`; nonzero exit on a terminal failure | `status` (`ok`, `error`, `timeout`); exit 0, 1, 2 | `status` `turn_end.reason`; exit 0, 1, 130 | `result.subtype` (`success`, `error`, `max_turns`); exit 0, 1, 3 to 10, 130 |
| `error` | `result.error`; stderr | `result.errors[]` | `error.error.data.message` | `error` event; `run_result` | `error` in `assistantMessageEvent` | `turn.failed.error.message`; `error` | `agent_end`, `notice` | `error.message`, `error.kind` | `error.message` | `result.error` |
| `incomplete_details` | `--max-turns` or `--run-budget` hit: exit 2, `turn_exit_reason` in the usage file | `error_max_turns`, `error_max_budget_usd` | exit 124 | `done.reason` `max_iterations` or `mistake_limit`; `-t` hit | `stopReason: length` | — | `--max-time` hit | `status: timeout` | `turn_end.reason` | `subtype: max_turns`, exit 8 |
| `output`: `message` (`output_text`) | `text` records; `result.text`; the `-z` stdout | `assistant` `text` blocks; `result.result` | `text` parts | `content_*` text events; `run_result.text` | `text_delta`; `message_end` | `agent_message` items | `message_update` text deltas; `message_end` | `payloads[].text` | `text`; `final.text` | `text_delta` events (*source*); `result.finalText` |
| `output`: `reasoning` | — | `thinking` blocks | `reasoning` parts with `--thinking` | `contentType: reasoning` events | `thinking_delta` | `reasoning` items (`model_reasoning_summary`) | `thinking` deltas | — | `thinking` | `thinking_*` events (*source*) |
| `output`: `shell_call` + `shell_call_output` | `tool_use` + `tool_result` for the terminal tool | `tool_use` Bash + `tool_result` | `tool_use` parts, `tool: bash` | tool events, `toolName: bash` | `tool_execution_start` + `tool_execution_end`, `bash` | `command_execution` items (command, output, exit code) | `tool_execution_*`, `bash` | `toolSummary` counts only; no per-call record | `tool_call` + `tool_result`, bash tool | `tool_running` + `tool_completed`, shell tool |
| `output`: `apply_patch_call` + `apply_patch_call_output` | `tool_use` + `tool_result` for file tools | `tool_use` Write / Edit + `tool_result` | `edit`, `write` parts | `editor`, `apply_patch` tool events | `edit`, `write` | `file_change` items (path, kind) | `edit`, `write` | counts only | fs tool events | edit / write tool events |
| `output`: `function_call` + `function_call_output` (any other tool) | other `tool_use` + `tool_result` | other `tool_use` + `tool_result` | other `tool_use` parts | other tool events | other tool events | `web_search`, `todo_list`, `collab_tool_call` items | other tool events | counts only | other tool events | other tool events |
| `output`: `mcp_call` | tools named `mcp_<server>_<tool>` | tools named `mcp__<server>__<tool>` | tools named `<server>_<tool>` | MCP tool events | tools named `mcp__<server>__<tool>` | `mcp_tool_call` items | tools named `mcp__<server>_<tool>` | counts only (`MCP.<server>.<tool>()` in code mode) | tools named `mcp__<server>__<tool>` | tools named `mcp__<server>__<tool>` |
| `output`: `web_search_call` | — | `tool_use` WebSearch | `websearch` parts | — | — | `web_search` items | — | — | web tool events | — |
| `output_text` (SDK convenience) | the `message` texts joined | same | same | same | same | same | same | same | same | same |
| `usage` (`input_tokens`, `output_tokens`, `cached_tokens`, `total_tokens`) | `result.tokens`; the `--usage-file` report | `result.usage` | `step_finish` (*source*); `kilo stats` | `usage` events; `run_result.usage` | `message_update.usage` | `turn.completed.usage` | `message_end` usage | `usage`, `costUsd` | `step_end.usage` | `result.usage` |
| `metadata.session_id` (platform addition: what to resume later) | `result.session_id` | `session_id` | `sessionID` | `run_start.sessionId` (with `-v`) | the `session` header `id` | `thread_id` | the session header `id` | `sessionId` (resume not available to `exec`) | `session.sessionId` | `result.sessionId` |
| stream events (`response.output_text.delta` and the rest) | `text` deltas from `stream-json` | `stream_event` deltas with `--include-partial-messages`; otherwise whole blocks | whole parts, no deltas | `content_*` events | `text_delta` and friends | whole items as `item.*`, no token deltas | `message_update` deltas | none; the response arrives whole | `text` per block, not per token | `text_delta` (*source*) |

Two things decide how good a harness's responses can be. A JSON event stream gives a package the
tool calls and their results as they happen, so `output` holds the whole turn and the client sees
it stream; nine of the ten have one, and at least five of those (Claude Code with `--include-partial-messages`,
Pi, Oh My Pi, Hermes, Command Code) deliver text token by token while Kilo Code, Codex and DeepSeek
Harness deliver it in completed blocks. OpenClaw's single envelope gives the final text, the token counts and tool
counts, so its `output` is a message and nothing else.

## Seeds: what a package puts in the workspace and home

A harness package is an adapter plus seeds. The adapter parses the request and reads the CLI's
output. The seeds are files the platform writes before the run, into the workspace the CLI runs
in and into the home directory it reads its configuration from, and they are where an
organization's harness actually lives: its standing instructions, its MCP servers, its skills,
its hooks and its permission rules. Every path below is documented by the harness (or marked
*source*), relative to the workspace unless it starts with `~` or a variable. Where several
harnesses read the same file, one seed serves them all.

| Seed | Hermes Agent | Claude Code | Kilo Code | Cline | Pi | Codex | Oh My Pi | OpenClaw | DeepSeek Harness | Command Code |
|---|---|---|---|---|---|---|---|---|---|---|
| Instructions, always loaded | first match: `.hermes.md`/`HERMES.md`, `AGENTS.override.md`, `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `.cursor/rules/*.mdc`; `~/.hermes/SOUL.md` (persona) | `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md` (cwd and ancestors, concatenated); `AGENTS.md`, `.claude/AGENTS.md` when no CLAUDE.md; `~/.claude/CLAUDE.md`; `/etc/claude-code/CLAUDE.md` | `AGENTS.md` (then `AGENT.md`), `CLAUDE.md`, `CONTEXT.md`, parents searched, first match; `~/.config/kilo/AGENTS.md` | `.clinerules/*.md`, `.cline/rules/*.md`, `AGENTS.md`, `.cursorrules`, `.windsurfrules`; `~/.cline/rules/`, `~/.agents/AGENTS.md` | `AGENTS.override.md`, `AGENTS.md`, `CLAUDE.md` in cwd and parents; `~/.pi/agent/AGENTS.md` | `AGENTS.override.md` or `AGENTS.md` per directory, root to cwd; `~/.codex/AGENTS.override.md` or `~/.codex/AGENTS.md` | `AGENTS.md`, `CLAUDE.md` (walk up); `.omp/AGENTS.md`; `.omp/RULES.md` (always applied); `~/.omp/agent/AGENTS.md`, `~/.omp/agent/RULES.md`; also `.claude/CLAUDE.md`, `.gemini/GEMINI.md`, `.github/copilot-instructions.md` | `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`, `BOOTSTRAP.md` (first run), `MEMORY.md` in the agent workspace; the exec folder's `AGENTS.md` | `AGENTS.md`, `CLAUDE.md`, `AGENTS.local.md`, `CLAUDE.local.md`, project root to cwd; `~/.dsh/AGENTS.md` | `AGENTS.md` or `.commandcode/AGENTS.md`; subdirectory `AGENTS.md` on demand; `~/.commandcode/AGENTS.md`; `@path` imports |
| Rules, path-scoped | `.cursor/rules/*.mdc` | `.claude/rules/**/*.md`, `~/.claude/rules/*.md` (`paths:` frontmatter) | `instructions` list in `kilo.jsonc`; `.kilocode/rules/` (legacy) | `.clinerules/*.md` with `paths:` frontmatter | — | `project_doc_fallback_filenames` in config.toml | `.cursor/rules/*.mdc`, `.cursorrules`, `.windsurf/rules/*.md`, `.clinerules`; `contextFiles.extra` | `hooks.internal.entries.bootstrap-extra-files` | — | — |
| System prompt, replace or append | `agent.system_prompt`, `agent.personalities` in `~/.hermes/config.yaml` | flags only (`--system-prompt[-file]`, `--append-system-prompt[-file]`); `.claude/output-styles/*.md` replaces the persona section | `agent.<name>.prompt` (text or `{file: PATH}`) in `kilo.jsonc`; agent markdown body | flag only (`-s`) | `.pi/SYSTEM.md` replaces, `.pi/APPEND_SYSTEM.md` appends; `~/.pi/agent/SYSTEM.md`, `APPEND_SYSTEM.md` | `model_instructions_file`, `developer_instructions` in config.toml | `.omp/SYSTEM.md`, `.omp/SYSTEM_TEMPLATE.md` (Handlebars), `.omp/APPEND_SYSTEM.md`; `~/.omp/agent/PERSONALITY.md` | none; `SOUL.md` and `IDENTITY.md` are the persona | `system-prompt` row (`personaPrefix`, `personaSuffix`) in `~/.dsh/cordis.patch.yml` or a `--patch` file | none (a mod can alter it) |
| MCP servers | `mcp_servers:` in `~/.hermes/config.yaml`: `{command, args, env}` or `{url, headers, transport}` | `.mcp.json`: `{"mcpServers": {name: {type, command, args, env, url, headers}}}`; `~/.claude.json` for user scope | `mcp` in `kilo.jsonc` or `.kilo/kilo.jsonc`: `{type: local, command: [..], environment}` or `{type: remote, url, headers}` | `~/.cline/data/settings/cline_mcp_settings.json`: `{"mcpServers": {name: {command, args, env, autoApprove}}}` or `{type: streamableHttp, url, headers}` | `.pi/mcp.json`, `~/.pi/agent/mcp.json`: `{"mcpServers": {name: {command, args, env}}}` or `{url, headers}`; `exposure` defaults to codemode | `[mcp_servers.NAME]` in `~/.codex/config.toml` or `.codex/config.toml`: `command`, `args`, `env` or `url`, `bearer_token_env_var` | `.omp/mcp.json`, `~/.omp/agent/mcp.json`: `{"mcpServers": {name: {type: stdio, command, args, env}}}` or `{type: http, url, headers}`; reads `.claude/.mcp.json`, `.cursor/mcp.json` too | `mcp.servers.NAME` in `~/.openclaw/openclaw.json`: `{command, args, env}` or `{url, transport, headers}` | insert row `@deepseek-ai/dsh-mcp-client` with `{serverName, transport: stdio or streamable-http, command, args, env, url, headers}` | `.mcp.json` (project), `~/.commandcode/mcp.json` (user), `~/.commandcode/projects/<slug>/mcp.json` (local): `{"mcpServers": {name: {command, args, env}}}` or `{transport: http, url, headers}` |
| Skills (`SKILL.md` with `name`, `description` frontmatter) | `~/.hermes/skills/<name>/SKILL.md`; `.hermes/skills/`, `.agents/skills/` at the git root (need `hermes skills trust`) | `.claude/skills/<name>/SKILL.md`; `~/.claude/skills/<name>/SKILL.md`; `.claude/commands/*.md` (legacy) | `.kilo/skills/<name>/SKILL.md`; `~/.kilo/skills/`; `.agents/skills/`, `~/.agents/skills/`; `.claude/skills/`, `~/.claude/skills/` | `.cline/skills/`, `.clinerules/skills/`, `.claude/skills/`, `.agents/skills/`; `~/.cline/skills/`, `~/.agents/skills/` | `.pi/skills/**/SKILL.md`, `.agents/skills/`; `~/.pi/agent/skills/`, `~/.agents/skills/` | `.agents/skills/<name>/SKILL.md` (cwd to repo root); `~/.agents/skills/`; `/etc/codex/skills` | `.omp/skills/<name>/SKILL.md`; `~/.omp/agent/skills/`; `.github/skills/`; `.claude/skills/`, `.codex/skills/`, `.agents/skills/` | `<workspace>/skills/<name>/SKILL.md`; `<workspace>/.agents/skills/`; `~/.agents/skills/`; `~/.openclaw/skills/` | `.dsh/skills/`, `.agents/skills/`; `~/.dsh/skills/`; `~/.agents/skills/` | `.commandcode/skills/<name>/SKILL.md`, `.agents/skills/`; `~/.commandcode/skills/`, `~/.agents/skills/`; `skills` list in settings |
| Hooks | `hooks:` in `~/.hermes/config.yaml`: `{<event>: [{command, timeout, matcher}]}`; events `pre_tool_call`, `post_tool_call`, `pre_llm_call`, `post_llm_call`, `on_session_start`, `on_session_end`; needs `--accept-hooks` or `HERMES_ACCEPT_HOOKS=1` headless | `hooks` in `.claude/settings.json`: `{"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": ".."}]}]}`; events `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop`, `PreCompact`, `SessionEnd` and more; run in `-p` | `plugin` modules in `kilo.jsonc` or `.kilo/plugin/*.ts` with `tool.execute.before`, `tool.execute.after`, `permission.ask` | one executable per event in `.cline/hooks/`, `.clinerules/hooks/`, `~/.cline/hooks/` (`PreToolUse`, `PostToolUse`, `TaskStart`, `TaskComplete`, `UserPromptSubmit`, `PreCompact`); JSON on stdin, `{cancel, errorMessage, contextModification}` on stdout; off under `-y` | extensions: `.pi/extensions/*.ts`, `~/.pi/agent/extensions/`, exporting `(pi) => pi.on("tool_call", ..)` | `.codex/hooks.json`, `~/.codex/hooks.json` or `[hooks]` in config.toml: `{"PreToolUse": [{"matcher", "hooks": [{"type": "command", "command"}]}]}`; events `PreToolUse`, `PermissionRequest`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `SessionStart`, `SessionEnd` and more; must be trusted | `.omp/hooks/pre/*.ts`, `.omp/hooks/post/*.ts`, `~/.omp/agent/hooks/`; same shape as extensions (`pi.on("tool_call", ..)`) | `~/.openclaw/hooks/<name>/HOOK.md` + `handler.ts`; `<workspace>/hooks/<name>/`; events `command:*`, `session:*`, `agent:bootstrap` | insert row `@deepseek-ai/dsh-hooks-claude-code` reading `.claude/hooks.json`, or `dsh-hooks-codex` reading `.codex/hooks.json` | `hooks` in `.commandcode/settings.json`: `{"PreToolUse": [{"matcher": "shell", "hooks": [{"type": "command", "command", "timeout"}]}]}`; events `PreToolUse`, `PostToolUse`, `Stop`, `SessionStart` |
| Subagents | — | `.claude/agents/**/*.md`, `~/.claude/agents/`; frontmatter `name`, `description`, `tools`, `model`, `permissionMode`, `maxTurns`, `skills`, `mcpServers`; or `--agents '<json>'` | `.kilo/agents/*.md`, `~/.config/kilo/agent/*.md`, or `agent.<name>` in `kilo.jsonc`; `description`, `mode`, `model`, `prompt`, `permission`, `variant` | `.cline/agents/*.md`, `~/.cline/agents/`; `name`, `description`, `tools`, `providerId`, `modelId` (*source*) | — | `collab_tool_call` only; no file format | `.omp/agents/*.md`, `~/.omp/agent/agents/`; `name`, `description`, `model`, `tools`, `thinking-level` | `agents.entries.<id>` in openclaw.json (own workspace, model, tools, identity) | `tool-subagent` row; bundles `dsh-subagent-codex`, `dsh-subagent-claude-code` | `.commandcode/agents/*.md`, `~/.commandcode/agents/`; `name`, `description`, `tools`, `model`, `reasoningEffort`, `maxTurns`, `permissionMode` |
| Prompt templates, slash commands | skills double as `/<skill>` | `.claude/commands/*.md`; skills with `user-invocable` | `.kilo/commands/*.md`, `~/.config/kilo/commands/`; `command.<name>` in `kilo.jsonc`; `kilo run --command NAME` | `.cline/workflows/*.md`, `~/.cline/workflows/` | `.pi/prompts/*.md`, `~/.pi/agent/prompts/`; `$1`, `$@` | skills via `$name` | `.omp/prompts/**/*.md`, `~/.omp/agent/prompts/` | `$skill-name` in the message | `tool-workflow` row | `.commandcode/commands/**/*.md`, `~/.commandcode/commands/`; `$ARGUMENTS`, `$1` |
| Settings and permissions | `~/.hermes/config.yaml`: `approvals.{mode, deny}`, `command_allowlist`, `agent.disabled_toolsets`, `terminal.backend` (`local`, `docker`, `ssh`, …), `security.*`; `~/.hermes/.env` for keys | `.claude/settings.json`, `.claude/settings.local.json`, `~/.claude/settings.json`, `/etc/claude-code/managed-settings.json`; `permissions.allow`, `ask`, `deny` as `Tool(pattern)`; `env`; `model`; `effortLevel`; precedence managed > `--settings` > local > project > user; in `-p`, project `permissions.allow` is not applied but `hooks` and `env` are | `kilo.jsonc`, `.kilo/kilo.jsonc`, `~/.config/kilo/kilo.jsonc`; `permission` map (`allow`, `ask`, `deny`, per tool, glob patterns, last match wins); `KILO_CONFIG`, `KILO_CONFIG_CONTENT`; project config cannot use `{env:VAR}` | `~/.cline/data/settings/global-settings.json` (`toolAutoApprove`, `disabledTools`, `compactionStrategy`, `planActMode`; keys *source*); `~/.cline` and `~/.cline/data` movable with `CLINE_DIR`, `--data-dir` | `.pi/settings.json`, `~/.pi/agent/settings.json`; `defaultTools`, `defaultThinkingLevel`, `compaction`, `packages`, `extensions`, `skills`, `defaultProjectTrust`; `~/.pi/agent/trust.json` | `~/.codex/config.toml`, `~/.codex/<profile>.config.toml`, `.codex/config.toml` (trusted projects); `approval_policy`, `sandbox_mode`, `[sandbox_workspace_write]`, `[projects."<path>"] trust_level`; rules `.codex/rules/*.rules` (Starlark `prefix_rule`); `/etc/codex/requirements.toml` | `~/.omp/agent/config.yml`, `.omp/config.yml`, `--config FILE` overlay; `tools.approvalMode`, `tools.approval.<tool>`, `modelRoles`, `skills.*`, `mcp.*`; `.omp/secrets.yml` redaction | `~/.openclaw/openclaw.json` (JSON5): `tools.profile`, `tools.allow`, `tools.deny`, `tools.exec.mode`, `agents.defaults.sandbox`, `skills`, `plugins`; `--config-override` per run | `~/.dsh/profiles/<name>/cordis.patch.yml`, `~/.dsh/cordis.patch.yml`, `--patch FILE` (YAML row patches; `disabled: true`); `DSH_PERMISSION_MODE`; `~/.dsh/.credentials.yaml` | `~/.commandcode/settings.json`, `.commandcode/settings.json`, `.commandcode/settings.local.json`; `permissions.{defaultMode, allow, ask, deny}` as `Tool(pattern)`; `~/.commandcode/config.json`; `--config key=value` |
| Provider and model configuration | `model:`, `providers:` in config.yaml (`base_url`, `api_key`, `key_env`, `transport`) | `env` in settings (`ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_*_MODEL`), `apiKeyHelper`, `model`, `fallbackModel` | `provider.<id>` in `kilo.jsonc` (`npm`, `options.baseURL`, `options.apiKey`, `models.<id>.{limit, variants, options}`), `model` | `~/.cline/data/settings/providers.json` (`provider`, `apiKey`, `model`, `baseUrl`) | `~/.pi/agent/models.json` (`providers.<id>.{api, baseUrl, apiKey, models[]}`); `~/.pi/agent/auth.json` | `[model_providers.<id>]` (`base_url`, `env_key`, `wire_api`), `model`, `model_provider`, `openai_base_url` in config.toml; `~/.codex/auth.json` | `~/.omp/agent/models.yml` (`providers.<id>.{api, baseUrl, apiKey, models[]}`), `modelRoles` in config.yml; `~/.omp/agent/agent.db` | `models.providers.<id>` (`baseUrl`, `api`, `apiKey`, `models[]`), `agents.defaults.model` in openclaw.json | rows `agent-default-model`, `llm-deepseek` (`apiKeyEnv`, `baseURL`), `llm-pi-ai` (`providers.<id>.{api, baseURL, models[]}`) | `~/.commandcode/providers.json` (`provider.<name>.{baseURL, api, apiKey, models}`), `~/.commandcode/auth.json`, `model` in settings |
| Environment the run needs | `HERMES_HOME`; `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`; `HERMES_YOLO_MODE=1`; `HERMES_ACCEPT_HOOKS=1`; `HERMES_IGNORE_RULES=1` | `CLAUDE_CONFIG_DIR`; `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_DEFAULT_*_MODEL`, `ANTHROPIC_CUSTOM_HEADERS`; `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`; `CLAUDE_CODE_EFFORT_LEVEL`; `CLAUDE_CODE_MAX_OUTPUT_TOKENS`; `MCP_TIMEOUT` | `KILO_CONFIG_DIR`, `KILO_CONFIG`, `KILO_CONFIG_CONTENT`, `KILO_DB`; `KILO_PROVIDER`, `KILO_API_KEY`, `ANTHROPIC_API_KEY`; `KILO_PURE`; `XDG_*` | `CLINE_DIR`, `CLINE_DATA_DIR`, `CLINE_MCP_SETTINGS_PATH`, `CLINE_HOOKS_DIR`; `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `CLINE_API_KEY` | `PI_CODING_AGENT_DIR`, `PI_CODING_AGENT_SESSION_DIR`; provider keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, …); `PI_OFFLINE` | `CODEX_HOME`; `CODEX_API_KEY`; the variable named by `env_key` (`OPENAI_API_KEY` by default); `CODEX_CA_CERTIFICATE`; `RUST_LOG` | `OMP_PROFILE`, `PI_CONFIG_DIR`, `PI_CONFIG_FILES`; provider keys and `ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`; `PI_SMOL_MODEL`, `PI_SLOW_MODEL`; `NULL_PROMPT` | `OPENCLAW_STATE_DIR`, `OPENCLAW_CONFIG_PATH`, `OPENCLAW_WORKSPACE_DIR`; provider keys; `--auth-env-only` restricts to env | `DSH_HOME`; `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`; `DSH_PERMISSION_MODE`; `DSH_TOOLS_MODE`; `DSH_AGENTS_HOME` | `COMMAND_CODE_API_KEY`; `CMD_LOCAL_ONLY=1`; `CMD_TOOLS_ALL_ENABLE`; `COMMANDCODE_SCRATCHPAD_BASE`; BYOK keys by the names `providers.json` references |
| Session store (what `previous_response_id` points at) | `~/.hermes/state.db` (SQLite) | `~/.claude/projects/<project>/<session-id>.jsonl` | `~/.local/share/kilo/kilo.db` (SQLite; `KILO_DB`) | `~/.cline/data/sessions/<id>/`, index `~/.cline/data/db/sessions.db` | `~/.pi/agent/sessions/--<cwd>--/<ts>_<id>.jsonl` (or `--session-dir`) | rollouts under `$CODEX_HOME`; `~/.codex/history.jsonl` | `~/.omp/agent/sessions/<encoded-cwd>/<ts>_<id>.jsonl` (or `--session-dir`) | `<state-dir>/agents/<id>/agent/openclaw-agent.sqlite` | `~/.dsh/sessions/--<cwd>--/<id>/session.v3.jsonl.zstd` | `~/.commandcode/projects/<slug>/<id>.jsonl` |
| Turn the seeds off | `--ignore-user-config`; `--ignore-rules` | `--bare` (no CLAUDE.md, hooks, skills, agents, plugins, MCP except `--mcp-config`); `--setting-sources`; `--settings FILE\|JSON` | `--pure` (no external plugins); `KILO_DISABLE_PROJECT_CONFIG` (*source*) | `--data-dir DIR` (fresh state) | `-nc` (no context files); `-ne`, `-ns`, `-np`, `--no-mcp` | `--ignore-user-config`; `--ignore-rules`; untrusted projects skip `.codex/` | `--no-rules`, `--no-extensions`, `--no-skills`, `--no-tools` | `--isolated` (ignore ambient config); `--config FILE` | a profile with only the bundles it needs | `--no-skills`; `--safe-mode` (no mods) |

What converges, which is what a portable package should seed first:

- **`AGENTS.md` in the workspace** is read by nine of the ten. Claude Code reads it only when no
  `CLAUDE.md` is present, which a package controls by not writing one. Command Code reads nothing
  else. Writing both `AGENTS.md` and `CLAUDE.md` with the same text covers every harness but
  changes nothing for the eight that read either.
- **`.agents/skills/<name>/SKILL.md`** with `name` and `description` frontmatter is read by Pi,
  Codex, Cline, Kilo Code, Oh My Pi, OpenClaw, DeepSeek Harness and Command Code; Claude Code reads
  `.claude/skills/`, which Cline, Kilo Code and Oh My Pi also read; Hermes reads `.agents/skills/`
  at the git root once trusted. One skills directory, written twice, reaches all ten.
- **`{"mcpServers": {...}}` JSON** with `command`/`args`/`env` or `url`/`headers` per server is the
  file shape for Claude Code (`.mcp.json`), Pi (`.pi/mcp.json`), Oh My Pi (`.omp/mcp.json`),
  Command Code (`.mcp.json`) and Cline (its settings file). Oh My Pi also reads Claude Code's and
  Codex's files. Kilo Code, Codex, Hermes, OpenClaw and DeepSeek Harness each want the same
  information in their own config format, so a package keeps one server list and renders it.
- **Hooks** gate tool calls in nine of the ten (Pi through extensions), but no two agree on the
  file: Claude Code, Codex and Command Code share the `{"PreToolUse": [{"matcher", "hooks": [{"type": "command", "command"}]}]}`
  shape, and DeepSeek Harness can read Claude Code's or Codex's hooks file directly.
- **The home directory is per run.** Every harness takes an alternate home or config directory
  (`HERMES_HOME`, `CLAUDE_CONFIG_DIR`, `KILO_CONFIG_DIR`, `CLINE_DIR`, `PI_CODING_AGENT_DIR`,
  `CODEX_HOME`, `OMP_PROFILE`, `OPENCLAW_STATE_DIR`, `DSH_HOME`, `HOME` for Command Code), so a
  package's home seeds and the session store live in a directory the platform creates for the
  tenant and keeps between turns, and a `previous_response_id` finds its session there.

## Precedence: which mechanism a package uses when there are several

Most request fields and most seeds can reach a harness more than one way. A package picks one,
and the pick follows a rule rather than taste, because the point of a package is that an
organization writes its instructions, skills, MCP servers and hooks once and runs them on any
harness with as little change as possible. The rule:

1. **Prefer the mechanism the most harnesses share.** A file nine harnesses read beats a flag one
   harness has, even when the flag is more precise, because the same template then serves nine
   packages.
2. **Among equals, prefer a file over a flag over an environment variable.** Files live in the
   package's git repository, diff cleanly and can be reviewed. Flags are built at invocation and
   leave no trace. Environment is invisible.
3. **Use a flag when the shared file is absent or documented as defective for that harness**, and
   record why in the package manifest.
4. **Per-request values are flags.** The prompt, the model, `previous_response_id`, the reasoning
   level and `stream` change on every call, so they go on the command line whatever rule 2 says.
   Standing organizational assets are files.

The request's `instructions` field is the one that straddles both: it arrives per request, but
the shared mechanism for it is a file. Rule 1 wins, so a package appends the request's
`instructions` to `AGENTS.md` under its own heading each turn, and uses `--append-system-prompt`
only where the file is not read.

The ladders that follow, best first:

| What | 1st | 2nd | 3rd | Last resort |
|---|---|---|---|---|
| Standing instructions | `AGENTS.md` in the workspace (nine of ten read it; Claude Code reads it when no `CLAUDE.md` exists, so a package never writes one) | the harness's own file (`.clinerules/*.md`, `SOUL.md`, `.omp/RULES.md`) | `--append-system-prompt TEXT` or its file form | `--system-prompt TEXT`, which replaces the prompt and drops rules on Cline and others |
| Request `instructions` | appended to `AGENTS.md` under a heading | `--append-system-prompt` | `-c developer_instructions=` (Codex), `system-prompt` row (DeepSeek Harness) | `-s` on Cline, which discards the rules |
| Skills | `.agents/skills/<name>/SKILL.md` (eight of ten) plus a copy at `.claude/skills/` (Claude Code; also read by Cline, Kilo Code, Oh My Pi), which together cover all ten | the harness's own directory (`.pi/skills/`, `.omp/skills/`, `<workspace>/skills/`) | `--skill PATH`, `-s SKILLS` | — |
| MCP servers | one server list in the package, written as `{"mcpServers": {}}` JSON where that is native (`.mcp.json` for Claude Code and Command Code, `.pi/mcp.json`, `.omp/mcp.json`, Cline's settings file) | the same list rendered into `kilo.jsonc`, `config.toml`, `config.yaml`, `openclaw.json`, a `dsh` patch | `--mcp-config JSON`, `-c mcp_servers.…`, `--config-override` | `cline mcp add`-style commands run before the turn |
| Hooks | the `{"PreToolUse": [{"matcher", "hooks": [{"type": "command", "command"}]}]}` shape, read by Claude Code, Codex, Command Code and, through its hooks rows, DeepSeek Harness | the same hooks rendered to Cline's per-event executables, Hermes's YAML list, OpenClaw's `HOOK.md` | Pi, Oh My Pi and Kilo Code TypeScript handlers (`pi.on("tool_call", ..)`, `tool.execute.before`) | — |
| Permissions and tool restriction | the platform sandbox (always on, harness independent) | the harness's deny rules: `permissions.deny` as `Tool(pattern)`, Kilo Code's `permission` map, Codex's `.codex/rules/*.rules` and `-s read-only`, OpenClaw's `tools.deny`, Hermes's `approvals.deny`, `DSH_PERMISSION_MODE` | a `PreToolUse` hook that blocks | tool allowlist flags (`--tools`, `-t TOOLSETS`, `--tools-enable`) |
| Provider and key | environment (`ANTHROPIC_BASE_URL` with `ANTHROPIC_API_KEY`, `OPENAI_BASE_URL` with `OPENAI_API_KEY`) where the harness honours it, since the key never touches disk | the provider file in the home seeds (`models.json`, `models.yml`, `providers.json`, `kilo.jsonc`, `config.toml`, `openclaw.json`, a `dsh` patch) with the key as a variable reference | `-c`, `--config-override`, `cline auth` | — |
| Continuation | the resume flag (`--resume`, `exec resume`, `-s`, `--session-id`), per request | a state directory kept per session (OpenClaw) | — | — |
| Home directory | one directory per tenant, named by the harness's variable (`HERMES_HOME`, `CLAUDE_CONFIG_DIR`, `KILO_CONFIG_DIR`, `CLINE_DIR`, `PI_CODING_AGENT_DIR`, `CODEX_HOME`, `OMP_PROFILE`, `OPENCLAW_STATE_DIR`, `DSH_HOME`, `HOME`), holding the home seeds and the session store | — | — | — |

Where the ladder lands on a defect, the package says so instead of pretending: Cline's `--id` does
not resume a `--json` run, so the Cline package refuses `previous_response_id` until that changes;
OpenClaw's `exec` cannot name a session, so its package keeps a `--state-dir` per session and
documents that continuity is the directory, not an id.

### Interrupt and steer

Every one of the ten writes its session to disk as it runs, so a cancelled request loses
nothing: the platform sends SIGINT, the response ends with `status: incomplete` and the session
id in `metadata`, and the next request with `previous_response_id` continues. That is the one
steering model the wire offers, because it works on all ten.

Live steering, a new instruction injected while the agent is still working, needs a long-lived
process with a command channel, and about half have one: Pi and Oh My Pi in `--mode rpc`
(`steer`, `follow_up`, `abort`, `abort_and_prompt`), Hermes through its API server
(`/v1/runs/{id}/steer`, `/stop`) and `hermes acp`, Claude Code by queuing further user messages
over `--input-format stream-json`, Cline and DeepSeek Harness in ACP mode, Codex through its app
server and Kilo Code through its own server (both still to be verified at the pinned versions).
OpenClaw and Command Code interrupt and resume only. Four of the ten ship an Agent Client
Protocol mode (Hermes, Cline, Oh My Pi, DeepSeek Harness), which is the candidate for a single
steering adapter if the platform later offers live steering behind the same cancel-and-continue
contract.

### Guardrails

Four layers, strongest first, and a package should use all four:

1. the platform sandbox: container, filesystem and egress rules the platform enforces, which no
   prompt and no harness setting can loosen;
2. the harness's deny rules, named in the permissions row above;
3. `PreToolUse` hooks, which block a tool call by exiting 2 (Claude Code, Codex, Command Code,
   DeepSeek Harness), by `cancel: true` (Cline), by `fail_closed` (Hermes) or by throwing in a
   handler (Pi, Oh My Pi, Kilo Code); Oh My Pi's `before_provider_request` is the one hook that
   can rewrite the model request itself;
4. `AGENTS.md`, which is soft but universal.

Two traps: Cline turns hooks off under `-y`, so a package with hooks uses `--auto-approve true`;
Codex and Hermes refuse untrusted hooks headlessly unless the package passes
`--dangerously-bypass-hook-trust` or sets `HERMES_ACCEPT_HOOKS=1`.

## Sources

The documentation each harness's cells were checked against, as of October 2026. Pi's and
DeepSeek Harness's are the files in their repositories at the version's tag; Hermes's, Kilo Code's,
Oh My Pi's and OpenClaw's sites are unversioned and describe their main branches; Codex's live
site describes a release newer than 0.163.0, so its cells were confirmed against the
`rust-v0.163.0` source where the two differed.

- **Hermes Agent**: [CLI commands](https://hermes-agent.nousresearch.com/docs/reference/cli-commands), [configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration), [configuring models](https://hermes-agent.nousresearch.com/docs/user-guide/configuring-models), [providers](https://hermes-agent.nousresearch.com/docs/integrations/providers), [context files](https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files), [MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp), [MCP config reference](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference), [skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills), [hooks](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks), [sessions](https://hermes-agent.nousresearch.com/docs/user-guide/sessions), [environment variables](https://hermes-agent.nousresearch.com/docs/reference/environment-variables), [security](https://hermes-agent.nousresearch.com/docs/user-guide/security).
- **Claude Code**: [CLI reference](https://code.claude.com/docs/en/cli-reference), [headless](https://code.claude.com/docs/en/headless), [memory](https://code.claude.com/docs/en/memory), [MCP](https://code.claude.com/docs/en/mcp), [skills](https://code.claude.com/docs/en/skills), [subagents](https://code.claude.com/docs/en/sub-agents), [hooks](https://code.claude.com/docs/en/hooks), [settings](https://code.claude.com/docs/en/settings), [settings reference](https://code.claude.com/docs/en/settings-reference), [permissions](https://code.claude.com/docs/en/permissions), [sessions](https://code.claude.com/docs/en/sessions), [environment variables](https://code.claude.com/docs/en/env-vars), [model configuration](https://code.claude.com/docs/en/model-config), [LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect), [SDK message types](https://code.claude.com/docs/en/agent-sdk/typescript).
- **Kilo Code**: [CLI](https://kilo.ai/docs/code-with-ai/platforms/cli), [CLI reference](https://kilo.ai/docs/code-with-ai/platforms/cli-reference), [custom models](https://kilo.ai/docs/code-with-ai/agents/custom-models), [OpenAI compatible](https://kilo.ai/docs/ai-providers/openai-compatible), [AGENTS.md](https://kilo.ai/docs/customize/agents-md), [custom instructions](https://kilo.ai/docs/customize/custom-instructions), [rules](https://kilo.ai/docs/customize/custom-rules), [MCP in the CLI](https://kilo.ai/docs/automate/mcp/using-in-cli), [skills](https://kilo.ai/docs/customize/skills), [agents](https://kilo.ai/docs/customize/custom-modes), [permissions](https://kilo.ai/docs/customize/agent-permissions), [commands](https://kilo.ai/docs/customize/workflows), [plugins](https://kilo.ai/docs/automate/extending/plugins), [settings](https://kilo.ai/docs/getting-started/settings), [session history](https://kilo.ai/docs/code-with-ai/agents/session-history). Event field names: [`run.ts`](https://github.com/Kilo-Org/kilocode/blob/main/packages/opencode/src/cli/cmd/run.ts).
- **Cline**: [CLI reference](https://docs.cline.bot/cli/cli-reference), [CLI overview](https://docs.cline.bot/usage/cli-overview), [config](https://docs.cline.bot/getting-started/config), [rules](https://docs.cline.bot/customization/cline-rules), [skills](https://docs.cline.bot/customization/skills), [MCP](https://docs.cline.bot/mcp/mcp-overview), [plugins](https://docs.cline.bot/customization/plugins), [hook example](https://docs.cline.bot/customization/clineignore#enforcing-clineignore-with-a-hook), [CLI README](https://github.com/cline/cline/blob/main/apps/cli/README.md). Session and exit-code behaviour: [`apps/cli/src`](https://github.com/cline/cline/tree/main/apps/cli/src).
- **Pi**: docs at tag v1.3.0 under [`packages/coding-agent/docs`](https://github.com/earendil-works/pi/tree/v1.3.0/packages/coding-agent/docs): `cli.md`, `cli-integration.md`, `json.md`, `rpc.md`, `configuration.md`, `settings.md`, `models.md`, `providers.md`, `mcp.md`, `skills.md`, `extensions.md`, `prompt-templates.md`, `sessions.md`, `session-format.md`, `security.md`, `environment-variables.md`.
- **Codex**: [CLI commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli), [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode), [config reference](https://learn.chatgpt.com/docs/config-file/config-reference), [basic](https://learn.chatgpt.com/docs/config-file/config-basic) and [advanced](https://learn.chatgpt.com/docs/config-file/config-advanced) config, [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md), [MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), [skills](https://learn.chatgpt.com/docs/build-skills), [rules](https://learn.chatgpt.com/docs/agent-configuration/rules), [hooks](https://learn.chatgpt.com/docs/hooks), [sandboxing](https://learn.chatgpt.com/docs/sandboxing), [environment variables](https://learn.chatgpt.com/docs/config-file/environment-variables). Event shapes at 0.163.0: [`exec_events.rs`](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/exec/src/exec_events.rs); exec flags: [`exec/src/cli.rs`](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/exec/src/cli.rs).
- **Oh My Pi**: [`docs/`](https://github.com/can1357/oh-my-pi/tree/main/docs): `cli-reference.md`, `rpc.md`, `session.md`, `settings.md`, `models.md`, `providers.md`, `mcp-config.md`, `context-files.md`, `system-prompt-customization.md`, `skills.md`, `hooks.md`, `extension-loading.md`, `custom-tools.md`, `task-agent-discovery.md`, `secrets.md`, `environment-variables.md`; [omp.sh/docs](https://omp.sh/docs/cli).
- **OpenClaw**: [`openclaw agent`](https://docs.openclaw.ai/cli/agent), [agent workspace](https://docs.openclaw.ai/concepts/agent-workspace), [system prompt](https://docs.openclaw.ai/concepts/system-prompt), [custom providers](https://docs.openclaw.ai/gateway/config-tools/custom-providers), [models](https://docs.openclaw.ai/gateway/config-agents/models), [thinking](https://docs.openclaw.ai/tools/thinking), [MCP](https://docs.openclaw.ai/tools/mcp), [tool policy](https://docs.openclaw.ai/gateway/config-tools/tool-policy), [exec approvals](https://docs.openclaw.ai/tools/exec-approvals), [skills](https://docs.openclaw.ai/tools/skills), [hooks](https://docs.openclaw.ai/automation/hooks), [configuration](https://docs.openclaw.ai/gateway/configuration), [environment](https://docs.openclaw.ai/help/environment), [sessions](https://docs.openclaw.ai/cli/sessions).
- **DeepSeek Harness**: READMEs at tag dsh-v0.2.0-rc.2 in [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.2.0-rc.2): `apps/cli/README.md`, `apps/cli/reference/README.md`, `packages/bundle/headless/README.md`, `packages/context/agent-instructions/README.md`, `packages/core/system-prompt/README.md`, `packages/mcp/mcp-client/README.md`, `packages/skill/skill-filesystem/README.md`, `docs/config-catalog.md`, `docs/user/guide/providers.md`; site [deepseek-harness.github.io](https://deepseek-harness.github.io/deepseek-harness/en/).
- **Command Code**: [CLI reference](https://commandcode.ai/docs/reference/cli), [headless](https://commandcode.ai/docs/headless), [memory](https://commandcode.ai/docs/memory), [sessions](https://commandcode.ai/docs/sessions), [BYOK](https://commandcode.ai/docs/byok), [settings](https://commandcode.ai/docs/settings), [permissions](https://commandcode.ai/docs/permissions), [MCP](https://commandcode.ai/docs/mcp), [skills](https://commandcode.ai/docs/skills), [hooks](https://commandcode.ai/docs/hooks), [agents](https://commandcode.ai/docs/agents), [mods](https://commandcode.ai/docs/mods), [slash commands](https://commandcode.ai/docs/reference/slash-commands), [errors](https://commandcode.ai/docs/troubleshooting/errors). Event names beyond `tool_running` come from the published npm package.
