# opencode

[OpenCode](https://github.com/sst/opencode), the open-source coding agent, behind the OpenAI Responses API. Each turn
starts `opencode serve` (its headless HTTP server) in the session's workspace, sends one prompt over its API and maps
its `/event` stream onto Responses stream events. The server is used rather than `opencode run --format json` because
`run` prints text only once a part is complete, while the event stream carries token deltas.

- **CLI:** npm `opencode-ai` **1.19.4**, pinned in `install.sh`, installed the way OpenCode's docs give for OpenCode 1
  (https://opencode.ai/docs, *Install*: `npm install -g opencode-ai`), egress `registry.npmjs.org`. opencode-ai's
  `postinstall` puts the native binary for the machine's CPU (chosen by npm among its optional dependencies) into
  `bin/opencode.exe` (a hard link). `install.sh` fails unless `opencode --version` reports 1.19.4.
- **Provider:** an Anthropic Messages endpoint, or an OpenAI one over the Responses API or Chat Completions
  (`provider.apis: ["anthropic", "openai"]`). Verified with DeepSeek's Anthropic-compatible and Chat Completions endpoints
  (`deepseek-flash`, the default) and with OpenAI over the Responses API (`gpt-5.4-mini`).
- **Run egress:** none of its own (the organization's applies; a turn's MCP servers are reached through it). The
  models.dev catalog download, auto-update, LSP downloads and sharing are switched off. At start, OpenCode installs
  its plugin library (`@opencode-ai/plugin`, the CLI's own version) into `~/.config/opencode` with npm, for local
  plugins: where the organization's egress allows `registry.npmjs.org` it is fetched (about 5 MB per new sandbox,
  seen in the door's trace), elsewhere the install fails and the turn goes on.

## Files

| File | Role |
|---|---|
| `harness.json` | manifest (contract 1); persists OpenCode's SQLite store |
| `install.sh` | installs the pinned CLI into `$HG_TOOLS` |
| `run.sh` | `exec python3 "$HG_PACKAGE_DIR/map.py"` |
| `map.py` | writes OpenCode's config for the turn, places input files in the workspace, runs `opencode serve`, opens or resumes the session, sends the prompt, approves permission asks, maps the event stream (hgkit `Emitter`), handles SIGTERM |
| `tests/` | raw event streams recorded from real turns (`fixtures/*.jsonl`) and `test_map.py` |

## The command

```
opencode serve --hostname 127.0.0.1 --port 0          (in the workspace, in a process group of its own)
GET  /event                                           (SSE, subscribed before anything else)
GET  /session/<id>  or  POST /session {"title": ...}  (the saved session if OpenCode still has it, else a new one)
POST /session/<id>/prompt_async {parts, model, system, variant, format}
POST /permission/<id>/reply {"reply": "once"}  |  POST /question/<id>/reject   (as asks arrive)
POST /session/<id>/abort                              (on SIGTERM)
```

The port is read from the server's `listening on http://...:<port>` line; the turn ends when the session's status is
`idle` again. `--hostname` and `--port` were checked against `opencode serve --help` for 1.19.4.

Environment: `OPENCODE_CONFIG=/tmp/hg-opencode/opencode.json`, `OPENCODE_DISABLE_MODELS_FETCH=1` (no models.dev
download: the provider is defined in the config), `OPENCODE_DISABLE_AUTOUPDATE=1`, `OPENCODE_DISABLE_LSP_DOWNLOAD=1`
and `NO_COLOR=1`.

The config file (mode 0600, rewritten every turn) has one provider, `hg`, and `enabled_providers: ["hg"]` (none of
OpenCode's built-in providers load, so none of their network calls happen):

- `$HG_PROVIDER_API=anthropic` → `@ai-sdk/anthropic` (Messages API).
- `$HG_PROVIDER_API=openai` → `@ai-sdk/openai` (Responses API) for `gpt-*`, `o<digit>*` and `codex*` models,
  `@ai-sdk/openai-compatible` (Chat Completions) for every other model; the setting `OPENCODE_OPENAI_API`
  (`responses` | `chat`, an environment variable of the turn) forces one.
- `baseURL: $HG_PROVIDER_URL/v1` (every AI-SDK package appends its own path), `apiKey: "{env:HG_PROVIDER_TOKEN}"`:
  OpenCode substitutes it at load time, so the token never lands on disk.
- The model entry declares tool calls, reasoning and image/PDF input; `model` and `small_model` are `hg/$HG_MODEL`,
  and sessions are created with a title, so OpenCode never calls another model to name them.
- `snapshot: false` (OpenCode's undo history is a git copy of the workspace), `share: "disabled"`,
  `autoupdate: false`, `permission: {"question": "deny"}` (nobody can answer mid-turn). Other permission asks (e.g.
  writes outside the workspace) are approved by the package, as `opencode run --auto` does.

OpenCode 1.19 keeps sessions, messages and parts in SQLite: `persist` lists `.local/share/opencode/opencode.db` and
its `-wal`/`-shm` files. The server is stopped before the turn ends, so the files are consistent when the platform
saves them. Logs (`.local/share/opencode/log`) and caches are per sandbox.

Seeds are not shipped; an organization adds its own with an overlay: `seeds/home/.config/opencode/` (`opencode.json`,
`AGENTS.md`, agents, skills, plugins) or `seeds/workspace/AGENTS.md`. OpenCode merges the global config with the
per-turn file and the workspace's own `opencode.json`.

## Request fields

| Request field | Where it goes |
|---|---|
| `input` user text | a text part of the prompt |
| `input_image` / `input_file` (file_id, `data:` URL or base64 `file_data`) | placed in the workspace by name (hgkit `place_inputs`), then attached as file parts `file:///workspace/<name>` (mime from the name). http(s) URLs are not fetched |
| `instructions`, `system`/`developer` messages | the prompt's `system`, appended to OpenCode's system prompt for this turn (after its own prompt and the `AGENTS.md` files) |
| `previous_response_id` | the OpenCode session id kept in `$HG_STATE/opencode_session`, reused when `GET /session/<id>` finds it in the persisted store, else a new session |
| `store: false` | a new session, not remembered |
| `reasoning.effort` | the model `variant`: anthropic `high`→`high`, `xhigh`/`max`→`max` (thinking budgets; `max_tokens` 32000 unless `max_output_tokens`), lower efforts leave the provider default; openai shapes: `none`…`xhigh` as `reasoningEffort` (`max`→`xhigh`) |
| `text.format` `json_schema` | the prompt's `format` (OpenCode's StructuredOutput tool); the structured result becomes the answer text |
| the turn's MCP servers (`$HG_MCP`: the package's catalog and the request's `tools[type=mcp]`, as the platform resolved them) | `mcp.<key>` remote servers (`oauth: false`; `<key>` is the label with every character outside `[A-Za-z0-9_-]` replaced by `_`); `headers` as they are (placeholders the door replaces); `allowed_tools` → `tools: {"<key>_*": false, "<key>_<tool>": true}`. The `mcp_call` items carry the server's label |
| `max_output_tokens` | the model's `limit.output` |
| `max_tool_calls` | `agent.build.steps` (OpenCode's step budget: model calls, which bounds tool rounds) |
| `temperature`, `top_p` | `agent.build.temperature` / `top_p` |
| `model` | `hg/$HG_MODEL` |

These fields are **ignored**, and each one present is named in `metadata.hg_ignored_fields`: `tool_choice`,
`parallel_tool_calls`, `truncation`, `prompt_cache_key`, `service_tier`, `top_logprobs`, `include`, `prompt`,
`text.verbosity`, `reasoning.summary` (OpenAI variants always ask for `auto`) and function tools (`tools.function`).
http(s) file and image URLs are not fetched. `stream`, `background`, `store` and `metadata` are handled by the
platform; MCP `require_approval` by the platform too (accepted and ignored: a harness runs unattended).

## Output mapping

| OpenCode event | Responses |
|---|---|
| `message.part.delta` (text part) | `response.output_text.delta`, token by token |
| `message.part.updated` text, ended | the message closes (any text the deltas missed first); `synthetic`/`ignored` parts are skipped |
| reasoning part, ended | a `reasoning` item (empty thinking, as DeepSeek often returns on tool turns, is dropped) |
| tool `bash` | `shell_call` (command, `timeout_ms`) when it runs; `shell_call_output` at the end with stdout and `metadata.exit`; a `<shell_metadata>` timeout note becomes a `timeout` outcome (the note goes to stderr) |
| `write` | `apply_patch_call` `create_file` (or `update_file` if it existed), content as `+` lines, + output |
| `edit` | `apply_patch_call` `update_file` with OpenCode's unified diff + output |
| `apply_patch` (OpenAI models) | one `apply_patch_call` per file of the V4A patch (`Add`/`Update`/`Delete File`) + output |
| `<mcp label>_<tool>` | `mcp_call` (server_label, name, arguments, output or error) |
| any other tool (`read`, `glob`, `grep`, `task`, `todowrite`, `webfetch`, `skill`, `StructuredOutput`, ...) | `function_call` when it runs + `function_call_output` at the end |
| `step-finish` tokens (this session and its sub-agent sessions) | summed into `usage`: `input_tokens` = input + cache read + cache write, `output_tokens` = output + reasoning |
| `session.error` / assistant `error` | `response.failed` (`rate_limit_exceeded` for 429, `context_length_exceeded`, else `provider_error`) with the provider's message and body; `MessageOutputLengthError` → `response.incomplete` (`max_output_tokens`) |
| server exits or the stream ends early | `response.failed`, `harness_error` with the server's stderr tail |
| session idle | `response.completed`, `metadata.opencode_session` |

The setting `OPENCODE_RAW_EVENTS=1` copies the raw event stream to the turn's stderr as `OPENCODE_RAW <json>`
lines (how `tests/fixtures` were recorded); it never reaches client-visible events. The server's own output goes to
stderr too.

On SIGTERM (cancel or timeout), `map.py` aborts the session (`POST /session/<id>/abort`), stops the server's process
group (the running command with it) and exits; `stop_grace_s` is 10.

## Known limits

- **MCP servers need egress:** a server's host must be in the organization's egress (or the catalog's, which joins
  the harness's). The mapping was verified with `mcp.deepwiki.com` through an egress proxy (fixture
  `deepseek_anthropic_mcp_deepwiki.jsonl`).
- Startup: `opencode serve` takes several seconds to load (a ~200 MB binary) before the first model call (8-15 s
  measured on the platform).
- A failed turn stays in OpenCode's session history: if a request carried something the model refuses (say, a file
  type it does not take), later turns of that session replay it and fail the same way; start a new session.
- Tool calls are reported when OpenCode runs them, not as the model streams their arguments.
- Text-only models: the model entry declares image and PDF input, so an image sent to a model without vision is
  refused by the provider.
- The tools layer is ~410 MB, ~200 MB more than the binary alone: npm installs every per-platform package whose
  os/cpu match (on x86_64 both the AVX2 and the baseline build). npm 12 ignores `--omit=optional` for a global
  install, no flag picks one of two matching builds without naming a CPU, and the installed tree is not pruned.
- OpenCode 2 (`@opencode/cli`) is a separate major with its own docs; this package stays on OpenCode 1.

## Tests

```
.venv/bin/pytest -q packages/opencode
```

The fixtures are raw `/event` streams (`OPENCODE_RAW_EVENTS=1`) from real turns of the same CLI: on DeepSeek's
Anthropic-compatible endpoint a write + bash turn with instructions and its recall, French with reasoning.effort, an
edit with exit code 3, json_schema, an MCP call, a cancelled `sleep 300` and an unknown model; on its Chat Completions
endpoint a write + bash turn; on OpenAI (Responses, gpt-5.4-mini) an apply_patch turn with reasoning. Each one is
fed through `Mapper`, and its output is read with `harnessgateway.domain.contract.check_output`, the platform's own
reader. The tests also check the config (AI-SDK package per provider shape, MCP servers from an `$HG_MCP` file), the
prompt body built from a request (files placed in the workspace, instructions with system/developer messages) and
the ignored fields.
