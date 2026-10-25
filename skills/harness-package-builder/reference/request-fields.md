# Mapping the Responses request onto a CLI

The request arrives whole; map what the CLI supports and document the rest as ignored in your README. Typical
mappings (verify each against the CLI's own `--help`):

| Request field | Typical CLI mechanism |
|---|---|
| `input` (string or messages) | the prompt argument. hgkit `Request.prompt` joins the input text. |
| `input_file` / `input_image` parts | inline (`file_data`, data URLs): hgkit `Request.inline_files` → write into the workspace or pass as `@file` / `-i image`. By `file_id`: already under `$HG_FILES` (`index.json`). |
| `instructions` | a system-prompt flag (`--append-system-prompt`), or a section of the workspace's instruction file (`AGENTS.md`, `CLAUDE.md`) replaced each turn. |
| `previous_response_id` | the platform already picked the session (same sandbox, workspace, HOME, state). Resume the CLI's own session with the id you saved in `$HG_STATE` (`--resume <id>`, `exec resume <id>`, `--session-id`). |
| `model` | already resolved into `$HG_MODEL`: pass it to the CLI and pin every alias the CLI uses to it. |
| `reasoning.effort` / `reasoning.summary` | the CLI's effort/thinking option, when it has one. |
| `text.format` (json_schema) | a structured-output flag (`--json-schema`, `--output-schema`). |
| `tools` of type `mcp` | the CLI's MCP config (server URL, headers, `authorization` as a bearer token). Add the server's host to `run.egress` only if your organization allows it; otherwise MCP over the network is blocked. |
| `tools` of type `function` | client-executed tools: only if the CLI can surface a call and wait for the result on the next turn. Most CLIs cannot; ignore and document. |
| `max_tool_calls` | a max-turns / step-budget flag. |
| `max_output_tokens`, `temperature`, `top_p` | the CLI's model settings, if exposed. |
| `metadata` | usually ignored by the CLI; echoed back by the platform. |
| `store: false` | the platform will not keep the session; you may also tell the CLI not to persist its session. |
| `stream`, `background` | platform concerns: nothing for the package to do. Always print events as they happen. |
