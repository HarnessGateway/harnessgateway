# What a package prints

One JSON object per stdout line, each a Responses stream event; any other line is a log line. The platform checks
every event against the OpenAI SDK's model for its `type` and refuses the turn on the first bad one. hgkit's `Emitter`
prints all of these for you; the raw events are shown so you know what the platform sees, and for a `run` written in
another language. Every event omits `sequence_number` (the platform adds it).

## With hgkit

```python
out = Emitter()                                  # Emitter(validate=True) checks each event as it is printed
out.think("Looking at the tests")                # a reasoning item, streamed delta by delta
out.say("Running them.")                         # an assistant message, streamed delta by delta
run = out.ran("pytest -q")                       # a shell call, now; its output when it comes:
run.done(stdout="3 passed", exit=0)              #   (or both at once: out.ran("ls", stdout="a\n", exit=0))
out.wrote("hello.py", "print(1)\n")              # a file created (the diff is made for you)
out.edited("app.py", diff)                       # a file changed: a unified diff, or old="..", new=".."
out.deleted("tmp.txt")
call = out.called("github", "get_issue", {"n": 1})   # an MCP tool call, in progress; then:
call.done(result)                                #   or call.failed(error); one-shot: out.called(.., output=result)
out.used("WebSearch", {"q": "x"}, result)        # any other tool (one-shot, or a handle like called)
out.ignored(["temperature"])                     # request fields this turn did not act on
out.done(input=1200, output=340, cached=800)     # exactly one terminal event, last; or:
out.stopped("max_output_tokens")                 #   the agent stopped short (an OpenAI incomplete reason)
out.failed("rate_limit_exceeded", "provider returned 429")
out.item({...})                                  # any other Responses output item, as a dict
out.emit({...})                                  # any other Responses event, as a dict
out.log("claude-stream " + raw_line)             # a log line: kept in the turn's trace, never sent to the client
```

An open message or reasoning item is closed when anything else is emitted, or the turn ends; `out.close()` closes it
by hand (a CLI that sends one message per event: `out.say(text); out.close()`).

## The raw events

A streamed assistant message is this sequence (`out.say("Hel"); out.say("lo")`):

```json
{"type":"response.output_item.added","output_index":0,"item":{"id":"msg_1","type":"message","role":"assistant","status":"in_progress","content":[]}}
{"type":"response.content_part.added","output_index":0,"item_id":"msg_1","content_index":0,"part":{"type":"output_text","text":"","annotations":[]}}
{"type":"response.output_text.delta","output_index":0,"item_id":"msg_1","content_index":0,"delta":"Hel","logprobs":[]}
{"type":"response.output_text.delta","output_index":0,"item_id":"msg_1","content_index":0,"delta":"lo","logprobs":[]}
{"type":"response.output_text.done","output_index":0,"item_id":"msg_1","content_index":0,"text":"Hello","logprobs":[]}
{"type":"response.content_part.done","output_index":0,"item_id":"msg_1","content_index":0,"part":{"type":"output_text","text":"Hello","annotations":[]}}
{"type":"response.output_item.done","output_index":0,"item":{"id":"msg_1","type":"message","role":"assistant","status":"completed","content":[{"type":"output_text","text":"Hello","annotations":[]}]}}
```

Reasoning streams the same way with `reasoning_summary_part` / `reasoning_summary_text` events into a
`{"type":"reasoning","id":..,"summary":[{"type":"summary_text","text":..}]}` item.

Everything the agent did is an item: `response.output_item.added` then `response.output_item.done`, with the next free
`output_index`. Use the CLI's own call id as `call_id` so a call and its output pair up.

| Agent action | Items | hgkit |
|---|---|---|
| shell command | `shell_call` `{"id","call_id","status":"completed","action":{"commands":[cmd]}}` then `shell_call_output` `{"id","call_id","status":"completed","output":[{"stdout","stderr","outcome":{"type":"exit","exit_code":0}}]}` | `out.ran(cmd, stdout=.., stderr=.., exit=0)` |
| file write / edit / delete | `apply_patch_call` `{"id","call_id","status":"completed","operation":{"type":"create_file"\|"update_file"\|"delete_file","path",("diff")}}` then `apply_patch_call_output` `{"id","call_id","status":"completed"\|"failed","output"}` | `out.wrote(path, content)`, `out.edited(path, diff)`, `out.deleted(path)` |
| MCP tool | one `mcp_call` `{"id","server_label","name","arguments":"{...}","status":"in_progress"→"completed"\|"failed","output","error"}`; `error` is an object such as `{"type":"mcp_tool_execution_error","content":".."}`, never a string | `out.called(server, name, args)` → `call.done(output)` / `call.failed(error)` |
| any other tool | `function_call` `{"call_id","name","arguments":"{...}","status":"completed"}` then `function_call_output` `{"id","call_id","output","status"}` | `out.used(name, args, output)` |
| web search | `web_search_call` `{"id","status":"completed","action":{"type":"search","query":..}}` | `out.item({...})` |

## The terminal event

```json
{"type":"response.completed","response":{"usage":{"input_tokens":1200,"output_tokens":340,"total_tokens":1540,
  "input_tokens_details":{"cached_tokens":800,"cache_write_tokens":0},"output_tokens_details":{"reasoning_tokens":0}},
  "metadata":{"cli_session":"abc"}}}
{"type":"response.incomplete","response":{"incomplete_details":{"reason":"max_output_tokens"}}}
{"type":"response.failed","response":{"error":{"code":"rate_limit_exceeded","message":"provider returned 429"}}}
```

- `input_tokens` counts all input, cached included (Anthropic reports cache reads separately: add them).
  `out.done(input=.., output=.., cached=.., cache_write=.., reasoning=..)` builds the whole block.
- `incomplete_details.reason` must be one of the OpenAI values (`max_output_tokens`, `content_filter`, ...).
- `error.code`: use an OpenAI code when one fits (`rate_limit_exceeded`, `server_error`, `invalid_prompt`); any other
  string is allowed and reported in `metadata.hg_error_code`.
- `metadata` values are strings. `out.ignored([...])` adds `hg_ignored_fields` to them.

## Checking it

`python3 -m hgkit check out.jsonl` reads a recorded output exactly as the platform does and prints the first line
and field it would refuse, or `ok: N events, M log lines, completed with K output items`. In Python,
`hgkit.check(text)` returns `(events, outcome)` or raises `OutputError` with the same message; package tests call it.

Common refusals: a text delta without `logprobs`; an item without its required fields; a misspelled event type; a
second terminal event, or anything after it; usage without `cache_write_tokens` or `reasoning_tokens` details;
exiting 0 without a terminal event because the CLI's stream ended early (always end with one).
