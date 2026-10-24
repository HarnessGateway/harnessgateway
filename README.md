# HarnessGateway

The OpenAI Responses API, backed by Claude Code running on DeepSeek. One FastAPI endpoint: a
Responses request in, Claude Code runs the turn, a Responses response out, with Claude Code's
tool calls as the schema's own output items. Any OpenAI SDK talks to it unchanged.

    npm install -g @anthropic-ai/claude-code
    pip install -e .[dev]
    export DEEPSEEK_API_KEY=…
    uvicorn app:app --port 8000

A request:

    curl -s http://127.0.0.1:8000/v1/responses -H 'content-type: application/json' \
      -d '{"model":"claude-code","input":"Create hello.py that prints hello and run it"}'

The same from the SDK:

    from openai import OpenAI
    client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="hg_anything")
    r = client.responses.create(model="claude-code", input="Create hello.py that prints hello and run it")
    print(r.output_text)
    for item in r.output: print(item.type)
    again = client.responses.create(model="claude-code", previous_response_id=r.id, input="Now make it print it twice.")

`stream: true` streams the standard events. `previous_response_id` continues the conversation in
the same workspace. `model` is `claude-code` for the default DeepSeek model, or
`claude-code/deepseek-reasoner` and the like.

[The Responses wire](docs/responses-wire.md) explains the idea, links the official schema, and maps every request
field to the invocation of the ten most used harnesses and every response field to their output.

## What comes back

Claude Code's events become the schema's output items, nothing invented:

| Claude Code did | items |
|---|---|
| thought | `reasoning` |
| ran a command (Bash) | `shell_call`, then `shell_call_output` with stdout and the exit code |
| wrote or edited a file (Write, Edit) | `apply_patch_call`, then `apply_patch_call_output` |
| used any other tool | `function_call`, then `function_call_output` |
| said something | `message` with `output_text` |

`response.output_text` is the answer; `response.output` is the whole turn in order; `usage` has
the token counts DeepSeek reported. `metadata.session_id` is Claude Code's session, and
`metadata.workspace` the directory it worked in.

## Settings

| Variable | Meaning |
|---|---|
| `DEEPSEEK_API_KEY` | required |
| `DEEPSEEK_BASE_URL` | default `https://api.deepseek.com/anthropic` |
| `HG_MODEL` | the model when the request says just `claude-code`; default `deepseek-chat` |
| `HG_DATA_DIR` | the workspaces and Claude Code's home; default a temporary directory |

`pytest` runs without a key or the CLI, on recorded Claude Code events; with both present it
also makes one real turn.
