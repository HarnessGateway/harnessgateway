# hermes: Hermes Agent on HarnessGateway

[Hermes Agent](https://hermes-agent.nousresearch.com) by Nous Research, PyPI `hermes-agent` **0.19.0** (pinned with all of
its dependencies in `install.sh`), installed into a venv at `$HG_TOOLS/hermes` with the extras `anthropic`, `acp`, `mcp`.

## How a turn runs

`request.sh` runs `run.py`. `run.py` starts **`hermes acp`**, Hermes's Agent Client Protocol server (JSON-RPC 2.0 over
stdio, the protocol editors like Zed use), and sends it `initialize`, then `session/load` (or `session/new`), then
`session/set_mode dont_ask`, then `session/prompt`. `map.py` turns what comes back into Responses events with
`hgkit.Emitter`:

| Hermes reports | Responses output |
|---|---|
| `agent_message_chunk` updates (Hermes streams the provider's tokens) | `output_text` deltas, token by token |
| `agent_thought_chunk` updates (provider reasoning: DeepSeek thinking, etc.) | `reasoning` items |
| `terminal` tool calls | `shell_call` + `shell_call_output` (exact command, output, exit code) |
| `write_file` / `patch` tool calls | `apply_patch_call` + `apply_patch_call_output` (`create_file` with the new content, or `update_file` with the diff) |
| `mcp_<server>_<tool>` tool calls | `mcp_call` |
| every other tool (`read_file`, `search_files`, `process`, `execute_code`, `todo`, `memory`, `delegate_task`, ...) | `function_call` + `function_call_output` |
| the `session/prompt` result's `usage` | `usage` on the terminal event (input includes cache reads/writes, as OpenAI counts them; cache writes from the session's row in state.db) |
| a provider error (Hermes logs `[ERROR] agent.conversation_loop: ...`, no `Turn ended`) | `response.failed`, code `provider_error`, Hermes's error summary as the message |

**Why ACP, and why state.db too.** Hermes 0.19.0's CLI has no JSON output for `hermes chat` (`--format stream-json`
came later; `chat -q -Q` prints the final text only, `-z` has no usable resume). `hermes acp` is the richest
machine-readable interface 0.19.0 has: token streaming, reasoning, per-tool start/finish events, cancellation,
resumable sessions and token usage. But ACP renders tool calls for a human in an editor: arguments are summarised,
outputs truncated and formatted as Markdown, and a finished `write_file` is not always reported. So the tool items come
from Hermes's own transcript, the SQLite session store at `$HERMES_HOME/state.db` (table `messages`), which Hermes writes
during the turn. `run.py` reads the session's new rows whenever ACP reports tool activity, and again at the end of the
turn. That gives the exact JSON arguments and results; ACP's diff for an edit gives the old text for `update_file`.
If state.db cannot be read, the items come from ACP's own tool records.

Every record the mapper sees is also printed as a `hermes-raw [kind, value]` log line. Log lines are kept in the
turn's trace and never shown to the client, so any turn can be replayed (see `tests/`).

## Request fields

| Field | Used as |
|---|---|
| `input` text (and system/developer messages in it) | the prompt (`session/prompt` text block) |
| `input_image` (file_id or data URL) | an ACP image block, which Hermes sends to the model; an http(s) URL is named in the prompt |
| `input_file` (file_id or inline `file_data`) | copied into `/workspace/<filename>`; the prompt names it |
| `instructions` | a section of Hermes's system prompt, through `HERMES_ENVIRONMENT_HINT`, which Hermes provides for embedders. Hermes freezes a session's system prompt, so when the instructions change between turns `run.py` clears the stored prompt and Hermes rebuilds it. The workspace's own `AGENTS.md` / `.hermes.md` still apply as project context. |
| `previous_response_id` | continues the same Hermes session: its ACP session id is kept in `$HG_STATE/hermes_session`, and Hermes's history is in `~/.hermes/state.db` |
| `model` (`hermes/<provider>/<model>`) | `HG_MODEL`, set for this turn even on a resumed session, so the model can change between turns |
| `reasoning.effort` | `agent.reasoning_effort` in config.yaml |
| `max_tool_calls` | `agent.max_turns` (Hermes's limit on tool-calling iterations per turn) |
| `text.format` `json_schema` / `json_object` | a line in the prompt asking for JSON matching the schema (Hermes has no structured-output option) |
| `tools` of type `mcp` | `mcp_servers` in config.yaml (`server_url`, `headers`, `authorization` as a Bearer header, `allowed_tools` as `tools.include`). The server's host must be reachable from the sandbox, and `run.egress` is empty. |
| `stream`, `background`, `store`, `metadata` | handled by the platform |
| `temperature`, `top_p`, `max_output_tokens`, `tool_choice`, `parallel_tool_calls`, `truncation`, `service_tier`, `prompt_cache_key`, `safety_identifier`, `user`, `include`, other tool types | ignored |

## Configuration (written every turn)

`$HERMES_HOME=$HOME/.hermes` persists with the session. `config.yaml` is rewritten each turn (as JSON, which YAML
accepts) and sets the following:
- one custom provider, `harnessgateway`. Its `base_url` is `$HG_PROVIDER_URL` (with `anthropic_messages`) or
  `$HG_PROVIDER_URL/v1` (with `chat_completions`), and its API key is read from `HG_PROVIDER_TOKEN` (`key_env`). The
  `context_length` is 128000, so Hermes does not look up model metadata.
- `approvals.mode: off` plus `HERMES_YOLO_MODE=1`, `hooks_auto_accept`, and the ACP session mode `dont_ask`, so edits
  are approved too. Any `session/request_permission` still sent is answered "allow".
- turned off: title generation (an extra LLM call), the curator, the model catalog fetch, LSP, memory and skill nudges,
  verify-on-stop nudges, checkpoints, lazy pip installs at run time, and Tirith scanning.

When a session is resumed, Hermes restores the session's provider, base URL and model from its row in state.db. `run.py`
first points that row at this turn's provider, transport and model. Without that step, Hermes 0.19.0 restores a
resumed ACP session's `custom` provider by host and falls back to environment keys.

## Providers verified (2026-10-25, platform `rebuild`, Hermes 0.19.0)

| Provider (shape) | Models | Turn 1 + resume |
|---|---|---|
| `deepseek` (anthropic, `/v1/messages`) | `deepseek-flash` (default), `deepseek-v4-pro` | yes |
| `deepseek-openai` (openai, `/v1/chat/completions`) | `deepseek-flash`, `deepseek-v4-pro` | yes |
| `openai` (openai, `/v1/chat/completions`) | `gpt-5.4-mini` | yes |

## Cancel

Hermes runs in a session of its own. When `run.py` gets SIGTERM, it sends `session/cancel`. Hermes then interrupts the
running command (`[Command interrupted]`, exit 130), saves the session and returns `cancelled`. `run.py` waits at most 6
seconds for this, prints the terminal event and makes sure Hermes has exited, all well inside the 20 s stop grace.
0.19.0's ACP server can answer that cancelled `session/prompt` with an internal error; it is treated as cancelled.

## Known limits

- Tool items appear when the tool has finished, not when it starts, and the text of a turn streams live.
- The first turn on a new tools layer installs Hermes (about 1 to 2 minutes). Each turn starts `hermes acp` again,
  which takes about 4 s in gVisor before the first provider call.
- Background processes that Hermes starts (`terminal` with `background=true`) are not watched after the turn ends.
  The system prompt asks the model to run commands in the foreground.
- Hermes's browser, web-search and image tools have no backend in the sandbox and are left out by Hermes itself.
- Hermes does not report reasoning token counts for every provider (`reasoning_tokens` is often 0).
- Once in about ten runs, DeepSeek's Anthropic endpoint (`deepseek-flash`, with no thinking requested) put its
  reasoning in the visible text and repeated the answer. Hermes's own transcript has the same text, so it comes from
  upstream, not from the mapping.
- On this platform build, a cancel does not reach the sandbox. The node's `signal_group` runs
  `kill -TERM -- "-$p"` under `sh`, which is dash in the sandbox image, and dash rejects `--` ("Illegal number: -").
  The turn is marked `cancelled` after the grace period, but `run.py`, Hermes and its command keep running. The same
  signal sent with bash stops all of them in about 2 s.
- Hermes probes `GET /v1/models` on the provider once per turn. It is harmless (DeepSeek's Anthropic endpoint answers
  404).

## Tests

`tests/test_map.py` replays recorded turns (`tests/fixtures/*.jsonl`, the `hermes-raw` lines from real traces) through
`map.py`. It checks every line with `harnessgateway.domain.wire.TurnOutput` and checks the outcome. Run it with:

    .venv/bin/python -m pytest packages/hermes/tests -q
