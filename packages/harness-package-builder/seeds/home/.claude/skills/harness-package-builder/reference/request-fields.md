# Mapping the Responses request onto a CLI

The request arrives whole; map what the CLI supports and report the rest with `out.ignored([...])` (and in the
README). Typical mappings, each to be verified against the CLI's own `--help`:

| Request field | hgkit reader | Typical CLI mechanism |
|---|---|---|
| `input` (string or messages) | `req.prompt` (the user text, joined) | the prompt argument or stdin |
| `input_file` / `input_image` parts | `req.place_inputs()` writes them into the workspace by name (by `file_id` from `$HG_FILES`, or inline data) and returns the names | mention the names in the prompt, or pass them as `@file` / `-i image` |
| `instructions`; `system` / `developer` messages in `input` | `req.instructions` (joined) | a system-prompt flag (`--append-system-prompt`), or a section of the workspace's instruction file replaced each turn |
| `previous_response_id` | `req.state` and `req.history()` | the platform already picked the session (its workspace, `$HG_STATE`, the `persist` paths of `$HOME`). Resume the CLI's own session with the id saved in `req.state` the moment the CLI reported it (`--resume <id>`, `exec resume <id>`, `--session-id`); when there is none, or the CLI refuses it, start a fresh CLI session and give it the conversation from the history (`req.transcript()`, or the CLI's own transcript format). See SKILL.md, "How a conversation continues" |
| `model` | `req.model` (`$HG_MODEL`, already resolved) | pass it to the CLI and pin every alias the CLI uses to it |
| the provider | `req.provider.url`, `.token`, `.api` | the CLI's base URL and API key variables; `url` has no `/v1`, add it where the CLI expects one |
| `reasoning.effort` | `req.reasoning_effort` | the CLI's effort or thinking option, when it has one |
| `text.format` (json_schema) | `req.text_format` | a structured-output flag (`--json-schema`, `--output-schema`) |
| `tools` of type `mcp` | `req.mcp_servers()` (resolved by the platform from `$HG_MCP`: `[{label, url, transport, headers, allowed_tools}]`, header values holding placeholders) | the CLI's MCP config (server URL, headers) |
| `tools` of type `function` | — | client-executed tools: only if the CLI can surface a call and wait for the result on the next turn. Most cannot; ignore and document |
| `max_tool_calls` | `req.max_tool_calls` | a max-turns / step-budget flag |
| `max_output_tokens`, `temperature`, `top_p` | `req.get("...")` | the CLI's model settings, if exposed |
| `metadata` | `req.get("metadata")` | usually ignored by the CLI; echoed back by the platform |
| `store: false` | — | the platform keeps no session and delivers no history; tell the CLI not to persist either |
| `stream`, `background` | — | platform concerns: nothing for the package to do. Always print events as they happen |

Anything else: `req.get(field)` / `req.body`. The package's own version is `req.package_version`
(`$HG_PACKAGE_VERSION`, `<CLI version>-<revision>`) and `req.cli_version`, for the metadata it reports.
