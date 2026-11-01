# Responses events cookbook

What to print, and in which order. hgkit's `Emitter` produces all of these; write them by hand only if you must.
Every event omits `sequence_number` (the platform adds it). Field lists are what the OpenAI SDK requires.

## A streamed assistant message

```json
{"type":"response.output_item.added","output_index":0,"item":{"id":"msg_1","type":"message","role":"assistant","status":"in_progress","content":[]}}
{"type":"response.content_part.added","output_index":0,"item_id":"msg_1","content_index":0,"part":{"type":"output_text","text":"","annotations":[]}}
{"type":"response.output_text.delta","output_index":0,"item_id":"msg_1","content_index":0,"delta":"Hel","logprobs":[]}
{"type":"response.output_text.delta","output_index":0,"item_id":"msg_1","content_index":0,"delta":"lo","logprobs":[]}
{"type":"response.output_text.done","output_index":0,"item_id":"msg_1","content_index":0,"text":"Hello","logprobs":[]}
{"type":"response.content_part.done","output_index":0,"item_id":"msg_1","content_index":0,"part":{"type":"output_text","text":"Hello","annotations":[]}}
{"type":"response.output_item.done","output_index":0,"item":{"id":"msg_1","type":"message","role":"assistant","status":"completed","content":[{"type":"output_text","text":"Hello","annotations":[]}]}}
```

hgkit: `out.text("Hel"); out.text("lo")` … the message closes on the next item or at the end.

## Items for what the agent did

Each is an `output_item.added` + `output_item.done` pair (hgkit `out.item(...)` and the helpers below).

| Agent action | Items | hgkit |
|---|---|---|
| thinking | `{"type":"reasoning","id":..,"summary":[{"type":"summary_text","text":..}]}` | `out.reasoning(text)` |
| shell command | `shell_call` `{"id","call_id","status":"completed","action":{"commands":[cmd]}}` then `shell_call_output` `{"id","call_id","status":"completed","output":[{"stdout","stderr","outcome":{"type":"exit","exit_code":0}}]}` | `out.shell(cmd, stdout=.., stderr=.., exit_code=0)` |
| file write / edit / delete | `apply_patch_call` `{"id","call_id","status":"completed","operation":{"type":"create_file"\|"update_file"\|"delete_file","path",("diff")}}` then `apply_patch_call_output` `{"id","call_id","status":"completed"\|"failed","output"}` | `out.patch("update_file", path, diff)` |
| MCP tool | `mcp_call` `{"id","server_label","name","arguments":"{...}","status":"completed"\|"failed","output","error"}`; `error` is an object such as `{"type":"mcp_tool_execution_error","content":[...]}`, never a string | `out.mcp(server, name, args, output=..)` or `error=".."` |
| any other tool | `function_call` `{"call_id","name","arguments":"{...}","status":"completed"}` then `function_call_output` `{"id","call_id","output","status"}` | `out.function(name, args, output)` |
| web search | `web_search_call` `{"id","status":"completed","action":{"type":"search","query":..}}` | `out.item({...})` |

Use the tool's own call id as `call_id` when the CLI has one, so a call and its output pair up.

## The terminal event

```json
{"type":"response.completed","response":{"usage":{"input_tokens":1200,"output_tokens":340,"total_tokens":1540,
  "input_tokens_details":{"cached_tokens":800,"cache_write_tokens":0},"output_tokens_details":{"reasoning_tokens":0}},
  "metadata":{"cli_session":"abc"}}}
{"type":"response.incomplete","response":{"incomplete_details":{"reason":"max_output_tokens"}}}
{"type":"response.failed","response":{"error":{"code":"rate_limit_exceeded","message":"provider returned 429"}}}
```

- `input_tokens` counts all input, cached included (Anthropic reports cache reads separately: add them).
- `incomplete_details.reason` must be one of the OpenAI values (`max_output_tokens`, `content_filter`, ...).
- `error.code`: use an OpenAI code when one fits (`rate_limit_exceeded`, `server_error`, `invalid_prompt`); any other
  string is allowed and reported in `metadata.hg_error_code`.
- `metadata` values are strings.

## Common mistakes the platform refuses

- A text delta without `logprobs`; an item without its required fields; a misspelled event type.
- Printing a second terminal event, or anything after it.
- Usage without `cache_write_tokens` or `reasoning_tokens` details.
- Exiting 0 without a terminal event because the CLI's stream ended early: always end with one.
