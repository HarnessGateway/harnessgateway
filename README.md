# HarnessGateway

The OpenAI Responses API as the wire for coding-agent harnesses: a request in the Responses
schema goes to a harness package, the package runs its CLI and reports what it did as Responses
output items, and the response comes back in the same schema. Any OpenAI SDK talks to it unchanged.

This repository holds the prototype of that wire, in one file, around one template package.

    pip install openai pytest
    python3 wire.py          # the walkthrough: a request, what the harness got, the response, the stream, the refusals
    pytest                   # the same, as tests

## The pieces

**`wire.py`** is the platform's side, top to bottom:

1. `parse_request` validates a request with the SDK's `ResponseCreateParams` and resolves it into
   the fixed subset a harness gets: prompt, files, instructions, continuation, model, reasoning
   effort, MCP tools. Anything the package's manifest does not accept is refused with the API's
   error shape.
2. `stage` writes that subset where the package reads it: files into the workspace, the prompt and
   `request.json` into the run directory. That is all a package ever sees of a request.
3. `run_turn` is the template, the runner every package shares: the adapter's `configure.sh`
   (seeds become the CLI's config), then `run.sh` (the CLI, its native events relayed live) piped
   through `items.py` (the package's filter: native events become Responses items). Every item is
   validated against the SDK's `ResponseOutputItem`.
4. `response_object` and `events` put the items back on the wire as the SDK's `Response` and its
   stream event classes. A small HTTP server serves `POST /v1/responses`, and the demo and the
   tests read it back with the real `openai` client.

**`foobarness/`** is the template package, around a stand-in CLI called foobar that has its own
flags, config file, session state and event format, as every real agent CLI does:

| File | Role |
|---|---|
| `harness.json` | the manifest: defaults, and `accepts`, what the package takes of a request |
| `install.sh` | puts the CLI into `$HG_TOOLS`, once per package version |
| `adapter/configure.sh` | the seeds, in neutral shapes, become the CLI's own configuration |
| `adapter/run.sh` | the staged request becomes the CLI's flags; the CLI's native events go to stdout |
| `adapter/items.py` | the filter: native events become Responses output items, one JSON line each |
| `seeds/` | the customer's: `instructions.md`, `mcp.json`, `skills/*/SKILL.md` |
| `bin/foobar` | the stand-in CLI |

A package for a real CLI replaces `install.sh`, `run.sh` and `items.py`; the template and the
seed shapes stay.

## The items

The Responses schema already has item types for what a coding agent does, so the filter emits
those and nothing invented:

| the agent did | items |
|---|---|
| wrote a file | `apply_patch_call` (`create_file`, `update_file`, `delete_file`), then `apply_patch_call_output` |
| ran a command | `shell_call` with the commands, then `shell_call_output` with stdout, stderr and the exit code |
| called an MCP tool | `mcp_call` with arguments and output |
| thought | `reasoning` with a summary |
| answered | `message` with `output_text` |
| anything else | `function_call`, then `function_call_output` |

A client reads `response.output_text` for the answer and walks `response.output` for the rest,
exactly as it would against OpenAI.
