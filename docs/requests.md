# Requests

The platform's one API is `POST /v1/responses`, the OpenAI Responses API. Any OpenAI SDK talks to
it unchanged with an organization's key; `curl` does too.

```python
from openai import OpenAI
client = OpenAI(base_url="https://hg.example.com/v1", api_key="hg_…")
r = client.responses.create(model="claude-code", input="Create hello.py that prints hello and run it")
print(r.status, r.output_text)
for item in r.output:
    print(item.type)
```

```bash
hg ask claude-code "Create hello.py that prints hello and run it"
```

## The model path

| `model` | Harness | Provider | Model |
|---|---|---|---|
| `claude-code` | claude-code | the package's default | the package's default |
| `claude-code/anthropic` | claude-code | anthropic | the package's default |
| `claude-code/anthropic/claude-sonnet-4-5` | claude-code | anthropic | claude-sonnet-4-5 |

The response's `model` is the path the turn actually ran with. A request for a harness the member
cannot use, or for a provider the organization does not have, is a 404.

## What the platform reads

The body is validated against the SDK's own `ResponseCreateParams`; an unknown field or a wrong
type is a 400 that says so. Of the valid request the platform reads only `model`,
`previous_response_id`, `stream`, `background`, `store` and `metadata`, plus the text of `input`
for the session's title and the trace. Everything else is the package's: the request is written to
`request.json` for `request.sh` verbatim, and the package's `harness.json` says which fields it
honours (`accepts`). A field the package does not accept is not an error; the package ignores it.
That is what the catalog's packages do with the fields their CLI cannot take, and what you change
when you fork one.

## Streaming

`stream: true` streams the standard events as `text/event-stream`: `response.created`,
`response.in_progress`, then whatever the package emits (`response.output_item.added`,
`response.output_text.delta`, `response.output_item.done`, …) with sequence numbers the platform
assigns, and `response.completed` (or `failed`, `incomplete`) carrying the final response object.
`GET /v1/responses/{id}?stream=true` re-streams a running turn from its start, for a second client
or a reopened console. The `x-session-id` response header names the session either way.

## Continuing

`previous_response_id` continues the session that response belongs to: the same workspace, the
CLI's own state (`$HG_STATE`, where the package keeps the CLI's session id), the same harness
commit, and the same provider unless the request names one. A session runs one turn at a time;
a second request while one runs is a 409. A response made with `store: false` cannot be continued
and its session keeps no workspace.

## Closing the request

A foreground turn (no `background`) runs for as long as its request is open. Close the stream, or
stop waiting on a blocking call, and the platform stops the turn gracefully: the package's stop
signal and grace, `response.sh` if there is one, and a response that ends `cancelled` with
`metadata.incomplete_reason: client_disconnected` and whatever was produced. There is no endpoint
to call. `GET /v1/responses/{id}` still returns the response afterwards, and the session can
continue.

## Background turns

`background: true` returns at once with status `queued` or `in_progress`; poll `GET
/v1/responses/{id}`, or stream it with `?stream=true` (closing that stream changes nothing: a
background turn outlives its requests). `POST /v1/responses/{id}/cancel` stops a
turn: the package's `request.sh` gets `SIGTERM` (its whole process group), `response.sh` still
runs within the stop grace, and the response ends `cancelled` with what was produced.

## Files

`input_file` and `input_image` parts of a message, as `file_data` (a data URL, or the text itself)
with a `filename`, are decoded into `$HG_FILES` for the package before the turn; the trace keeps
their names and sizes, not their content. `hg ask … --file PATH` sends one.

## What comes back

The response object is the SDK's `Response`: `id`, `status`, `model`, `output`, `usage`,
`previous_response_id`, `metadata`, and `error` or `incomplete_details` when it applies. The
package decides the items (a shell command as `shell_call` and `shell_call_output`, a file edit as
`apply_patch_call`, a thought as `reasoning`, an MCP tool as `mcp_call`, text as `message`); the
platform validates each against the SDK's types and refuses a malformed one with status `failed`
and `metadata.error_code = package_output_invalid`. `metadata` always carries `session_id`,
`harness`, `version` (the commit), `turn` and `exit_code`, plus whatever the package added through
`response.sh`.

`GET /v1/responses/{id}/detail` adds the package's log (the non-event lines of `request.sh`'s
output), its stderr, the worker and the timings; `GET /v1/responses/{id}/request` returns the
request as sent.
