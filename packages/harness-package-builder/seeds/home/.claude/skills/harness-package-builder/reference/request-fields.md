# Mapping the Responses request onto a CLI

The request arrives whole; map what the CLI supports and document the rest as ignored in your README (and report
it with `out.ignored([...])`). Typical mappings (verify each against the CLI's own `--help`):

| Request field | hgkit reader | Typical CLI mechanism |
|---|---|---|
| `input` (string or messages) | `req.prompt` (the user text, joined) | the prompt argument or stdin |
| `input_file` / `input_image` parts | `req.place_inputs()` writes them into the workspace by name (by `file_id` from `$HG_FILES`, or inline `file_data` / data URLs) and returns the names | mention the names in the prompt, or pass them as `@file` / `-i image` |
| `instructions`; `system` / `developer` messages in `input` | `req.instructions` (joined) | a system-prompt flag (`--append-system-prompt`), or a section of the workspace's instruction file (`AGENTS.md`, `CLAUDE.md`) replaced each turn |
| `previous_response_id` | `req.state` (`$HG_STATE`): save the CLI's own session id on the first turn, read it on the next | the platform already picked the session (its workspace, `$HG_STATE` and the `persist` paths of `$HOME`); resume the CLI's own session with the saved id (`--resume <id>`, `exec resume <id>`, `--session-id`) |
| `model` | `req.model` (`$HG_MODEL`, already resolved) | pass it to the CLI and pin every alias the CLI uses to it |
| the provider | `req.provider.url`, `.token`, `.api` | the CLI's base URL and API key variables; `url` has no `/v1`, add it where the CLI expects one |
| `reasoning.effort` / `reasoning.summary` | `req.reasoning_effort` | the CLI's effort or thinking option, when it has one |
| `text.format` (json_schema) | `req.text_format` | a structured-output flag (`--json-schema`, `--output-schema`) |
| `tools` of type `mcp` | `req.mcp_servers()` (resolved by the platform from `$HG_MCP`: `[{label, url, transport, headers, allowed_tools}]`, header values holding placeholders) | the CLI's MCP config (server URL, headers) |
| `tools` of type `function` | — | client-executed tools: only if the CLI can surface a call and wait for the result on the next turn. Most CLIs cannot; ignore and document |
| `max_tool_calls` | `req.max_tool_calls` | a max-turns / step-budget flag |
| `max_output_tokens`, `temperature`, `top_p` | `req.get("...")` | the CLI's model settings, if exposed |
| `metadata` | `req.get("metadata")` | usually ignored by the CLI; echoed back by the platform |
| `store: false` | — | the platform will not keep the session; you may also tell the CLI not to persist its session |
| `stream`, `background` | — | platform concerns: nothing for the package to do. Always print events as they happen |

Anything else: `req.get(field)` / `req.body`. The package's own version is `req.package_version`
(`$HG_PACKAGE_VERSION`, `<CLI version>-<revision>`) and `req.cli_version`, for the metadata it reports.
