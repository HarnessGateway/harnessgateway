# hermes

Nous Research's Hermes Agent behind the OpenAI Responses API. Each turn starts Hermes's own gateway (in 0.22 every
chat runs on it) and runs the chat client once, `hermes chat --query-file F --format stream-json`, in the session's
workspace with approvals off (the gVisor sandbox is the boundary), resuming the session's Hermes conversation after
the first turn. The client's JSONL becomes Responses stream events; once the turn has ended the gateway is stopped,
which writes the session store out before the sandbox is saved.

- **CLI:** Hermes Agent **0.22.5**, installed by its official installer, the one Linux method its installation docs
  give (https://hermes-agent.nousresearch.com/docs/getting-started/installation:
  `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`), downloaded to a file and run with its own
  options: `--commit bf0203a794a07aac3ff764972ab9087baa4e4f4e` (the v0.22.5 tag; the installer can only pin a commit),
  `--non-interactive --skip-browser --skip-computer-use`, with `HOME=$HG_TOOLS/home` so the checkout, the managed
  Python, Node and tools and the dependency environment all land in the tools layer. `hermes pm install --extra
  anthropic` then adds the Anthropic SDK, which Hermes otherwise installs lazily on first use. Through the sandbox's
  egress proxy some of uv's many parallel downloads time out connecting, and Hermes passes no concurrency setting on
  to uv; its remedy is to run the installer again, which resumes from the checkout and uv cache already there, so each
  step gets up to six tries. `install.sh` fails unless `hermes --version` reports v0.22.5. Install egress: `hermes-agent.nousresearch.com` (the installer),
  `github.com` and `release-assets.githubusercontent.com` (the checkout, uv), `releases.astral.sh` (Python),
  `pypi.org` and `files.pythonhosted.org` (dependencies), `nodejs.org` and `registry.npmjs.org` (Node and the TUI/web
  builds the installer always runs). The tools layer is about 2.7 GB.
- **Provider:** an Anthropic Messages endpoint (`anthropic_messages` transport) or an OpenAI Chat Completions endpoint
  (`chat_completions`): `provider.apis: ["anthropic", "openai"]`. Verified with DeepSeek's Anthropic-compatible and
  Chat Completions endpoints (`deepseek-flash`, the default); not verified against OpenAI.
- **Run egress:** none of its own; the organization's egress applies. Hermes asks the provider for `GET /v1/models`
  (model metadata) a few times per turn; DeepSeek's Anthropic endpoint answers a harmless 404. Title generation, the
  skill curator, the model catalog fetch, LSP, memory nudges and lazy installs are switched off, and nothing else
  leaves the sandbox.
- **Seeds:** none. `config.yaml` is the package's mechanics and is rewritten every turn (an overlay's seeded
  `~/.hermes/config.yaml` would be replaced); an organization adds its own Hermes files with an overlay's
  `seeds/home/.hermes/` (for example `SOUL.md`, skills).

## Files

| File | Role |
|---|---|
| `harness.json` | manifest (contract 1) |
| `install.sh` | runs the official installer, pinned, into `$HG_TOOLS`, adds the Anthropic SDK, checks the version |
| `run.sh` | `exec python3 "$HG_PACKAGE_DIR/map.py"` (the platform starts it in the workspace) |
| `map.py` | places input files, writes `config.yaml`, starts the gateway and the chat client, maps its JSONL (hgkit `Emitter`) |
| `tests/` | recorded Hermes streams from real turns (`fixtures/*.jsonl`) and `test_map.py` |

## The command

```
<env>/bin/hermes gateway run                                      (for the turn, in a process group of its own)
<env>/bin/hermes chat --query-file /tmp/hg-hermes/prompt.md --format stream-json --yolo --accept-hooks
                      [--resume <session id>]
```

`<env>` is the dependency environment Hermes's install record selects: `packages.venv.environment` in
`$HG_TOOLS/home/.hermes/installs/<install key>/facts.json`, which the installer and `hermes pm install` write. The
checkout's launcher (`~/.local/bin/hermes`) cannot run from a read-only tools layer: before it imports anything it
takes a lock and a lease in that same `installs/<install key>/` directory of the data root it was installed under,
and under any other `HERMES_HOME` it treats the checkout as borrowed and builds a dependency environment of its own
(PyPI downloads, about 25 s and 400 MB per new sandbox). The environment's own `hermes` runs the same pinned code with
a writable `HERMES_HOME`.

Environment: `HERMES_HOME=$HOME/.hermes`, `NO_COLOR=1`, `PYTHONPATH` removed, and, for the client only,
`HERMES_EPHEMERAL_SYSTEM_PROMPT` (the request's instructions). The provider is the platform's relay at
`$HG_PROVIDER_URL` (`http://hg-door:8080`, already in `NO_PROXY`). `config.yaml` is rewritten every
turn (as JSON, which YAML accepts): one custom provider, `harnessgateway`, whose `base_url` is `$HG_PROVIDER_URL`
(`anthropic_messages`) or `$HG_PROVIDER_URL/v1` (`chat_completions`), whose key is read from `HG_PROVIDER_TOKEN`
(`key_env`) and whose model is `$HG_MODEL`; `approvals.mode: off` (with `single_query_mode`/`unattended_mode:
approve`), `hooks_auto_accept`, `security.allow_lazy_installs: false`, title generation, curator, model catalog and
LSP off, memory nudges off, `agent.environment_hint` (the sandbox: workspace, no one to ask, run in the foreground),
the request's reasoning effort and tool budget, and the turn's MCP servers.

Every flag above was checked against `hermes chat --help` for 0.22.5; `--format stream-json` and the gateway are
documented in Hermes's CLI reference.

## Request fields

| Request field | Where it goes |
|---|---|
| `input` (string, or the `input_text` parts of user messages) | the query, in `--query-file` |
| `input_file` / `input_image` by `file_id` or inline (`file_data`, data: URL) | written into the workspace by name (hgkit `place_inputs`; a name taken by other bytes gets a numeric suffix), and the query lists the names |
| `instructions`, and `system`/`developer` messages in `input` | `HERMES_EPHEMERAL_SYSTEM_PROMPT` on the client: Hermes adds it to its standing system prompt at call time and never stores it in the history |
| `previous_response_id` | `--resume <id>`. The Hermes session id is kept in `$HG_STATE/hermes_session` and the session in `$HOME/.hermes/state.db`, which the manifest persists (with its `-wal`/`-shm` files and `memories/`). If the session has gone, the turn starts a new one |
| `reasoning.effort` | `agent.reasoning_effort` (`none`, `minimal`, `low`, `medium`, `high`, `xhigh` are all Hermes levels) |
| `max_tool_calls` | `agent.max_turns` (Hermes's tool-calling iterations per turn; parallel calls in one iteration count once) |
| `text.format` `json_schema` / `json_object` | a paragraph of the query asking for JSON (matching the schema); Hermes has no structured-output option |
| `tools` of type `mcp` | resolved by the platform with the package's catalog (none) into `$HG_MCP`; each server becomes `mcp_servers.<label>` in config.yaml: `url`, `headers` (placeholders the door replaces), `transport: sse` for SSE servers, `allowed_tools` as `tools.include` (an empty list leaves the server out) |
| `model` | `harness/provider/model`. The platform passes the model as `$HG_MODEL` |

**Ignored**, and named in `metadata.hg_ignored_fields` when the request sets them: `tools` of other types (Hermes uses
its own tools plus MCP), `tool_choice`, `parallel_tool_calls`, `temperature`, `top_p`, `top_logprobs`,
`max_output_tokens`, `truncation`, `include`, `prompt_cache_key`, `service_tier`, `reasoning.summary`,
`text.verbosity`. Also not acted on: the assistant/tool items of an `input` list (the resumed Hermes session supplies
the history). `metadata`, `store`, `background`, `stream` and MCP `require_approval` are the platform's.

## Output mapping

| Hermes stream-json | Responses |
|---|---|
| `text` | `response.output_text.delta`, token by token, one assistant `message` per stretch of text between tool calls (leading blank lines dropped) |
| `tool_use` `terminal` + its `tool_result` | `shell_call` (emitted when called) + `shell_call_output` (`output` as stdout, `error` as stderr, `exit_code`; 130 for an interrupted command) |
| `tool_use` `write_file` + result | `apply_patch_call` `create_file` with every line `+` (Hermes does not say whether the file existed) + `apply_patch_call_output` (`failed` if the tool errored) |
| `tool_use` `patch` + result | `apply_patch_call` per file in the result (`files_created` / `files_modified` / `files_deleted`), each with its hunks from Hermes's unified diff, + `apply_patch_call_output` |
| `tool_use` `mcp__<server>__<tool>` + result | `mcp_call`: added `in_progress` when called, done `completed` with the result, or `failed` with an `mcp_tool_execution_error`. The server label maps back to the request's |
| any other `tool_use` (read_file, search_files, process, execute_code, todo, memory, tool_describe, ...) + result | `function_call` + `function_call_output` (`incomplete` if the tool errored) |
| `result` `session_id` | `$HG_STATE/hermes_session`, and `metadata.hermes_session_id`, `hermes_version` |
| `result` `text` | the end of the reply: the open message's final text is Hermes's stored reply (the live stream can drop a paragraph break), and a reply that was not streamed (after the tool budget ran out, or a replay gap) becomes a message |
| `result` `tokens` | terminal `usage`: `input_tokens` = Hermes's `input` (which already counts cache reads and writes, checked against the provider exchanges), `cached_tokens` = `cache_read`, `cache_write_tokens` = `cache_write`. `reasoning_tokens` is 0: the stream does not report it |
| `result` `exit_code` 0 | `response.completed` (with `metadata.hg_ignored_fields` when the request set fields Hermes ignores) |
| `result` `exit_code` 1 with no `error` | `response.incomplete` with `incomplete_details.reason = "max_messages"` (the turn stopped at `max_tool_calls`) and the work done so far |
| `result` `exit_code` 130 | `response.failed` `cancelled` (the gateway stopped the turn) |
| `result` `error` | `response.failed`: the message is Hermes's (`HTTP 400: The supported API model names are ...`), and the code is `provider_error` for the provider's HTTP errors, otherwise `cli_error`. Hermes's explanation for the user is not streamed as model text |
| no `result` (the client crashed) | `response.failed` `cli_error` with the exit code and the tail of stderr |

Every raw client line is also printed to the turn's log, prefixed `hermes-stream `; the client's and the gateway's
stderr go to the turn's stderr. Log lines go to the trace and are never shown to the client. The test fixtures came
from those log lines.

On SIGTERM (cancel or timeout), `map.py` stops the gateway, which interrupts the running command
(`[Command interrupted]`, exit 130), saves the session and exits within a second or two, then the client, and SIGKILLs
whatever is left of either process group. A cancelled turn prints no terminal event; the platform marks it
`cancelled`.

## Known limits

- **Instructions are fixed per Hermes session.** Hermes freezes a session's client environment when the session is
  created, so the instructions of the first turn apply to the whole conversation; a later turn's different
  instructions do not replace them. The same holds for the model: a resumed session keeps the model and provider it
  was created with, whatever `$HG_MODEL` a later turn names (checked: a `deepseek-v4-pro` turn resuming a
  `deepseek-flash` session still called `deepseek-flash`).
- **Tool output is capped.** Hermes's stream carries at most 5,000 characters of a tool's output; longer outputs end
  in `...` (and a terminal result that was cut is passed on as raw text, its exit code taken from `is_error`).
- **No reasoning items.** The stream-json protocol does not carry the model's reasoning, so none is reported.
- **MCP servers need egress.** The package has no catalog and no `run.egress`, so a request can add only a server whose
  host the organization's egress already allows (the platform refuses others with `mcp_server_not_allowed`). The
  mapping of an MCP call was recorded against `mcp.deepwiki.com` outside the platform (fixture `mcp_call.jsonl`).
- **Images are files.** `--image` is refused on the gateway path, so an input image is saved into the workspace and
  named in the query; a model without vision cannot look at it.
- Each turn starts a gateway and a client: 11 to 15 s in gVisor before the first provider call. The first turn on a new
  tools layer runs the installer (several minutes). Background processes a tool starts do not outlive the turn.
- On the platform's filesystem Hermes keeps `state.db` in `journal_mode=DELETE` (it warns about a cross-VM
  filesystem); the session store is still complete when the gateway stops.

## Tests

```
.venv/bin/pytest -q packages/hermes
```

The fixtures are raw `hermes chat --format stream-json` output from real Hermes 0.22.5 turns, recorded with the
previous revision's `map.py` in throwaway containers of the sandbox image (tools layer read-only, `HOME` fresh per
turn, only `persist` carried between turns) against DeepSeek: a file-creating first turn on the Anthropic and the Chat
Completions wire, the recall turn after it on a new sandbox, a `patch` edit, an input file with parallel tool calls,
instructions, `max_tool_calls` reached, a provider error, an MCP call, and a command interrupted by stopping the
gateway. Each one is fed through `Mapper`, and its output is validated with the platform's
`harnessgateway.domain.contract.check_output`. The tests also check the config (MCP servers from an `$HG_MCP` file),
the instructions (with system and developer messages), the ignored fields, the command line and environment built
from a request, and run `map.py` end to end against a fake `hermes` (an input file placed, a lost session, and SIGTERM
stopping the gateway).
