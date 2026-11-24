# Harness packages

A harness is an agent CLI made runnable on the platform by a package. The platform knows no CLI: everything about
one is in its package, and every package is run the same way. An organization starts with two, `shell` and
`hg-fde`, and owns every package it has.

The platform speaks one schema, the OpenAI Responses API's. A caller sends a Responses request; the package's `run`
reads that request, exactly as it was sent, and prints Responses output; the caller receives it. Nothing stands
between the two but this page. What the request asks for, a package does or says it cannot: nothing is ignored
unsaid.

This is the whole contract. Every sandbox is given it, read-only, in `$HG_CONTRACT` (`/etc/hg/contract`): this
page, and the Responses API as schemas.

| | |
|---|---|
| `PACKAGES.md` | This page |
| `request.schema.json` | A request, as OpenAI's SDK describes one: what `run` reads |
| `response.schema.json` | The response a caller receives, as OpenAI's SDK describes it: what the platform makes of what `run` prints |
| `stream.schema.json` | The events of a response's stream, as OpenAI's SDK describes them |
| `lines.schema.json` | One line `run` may print: an output item or a stream event of those schemas, with what the platform fills in left optional |

The first three are OpenAI's own, unchanged. All four are generated from one pinned version of the SDK, which each
names in its first line and `GET /v1/platform` reports as `sdk`: that version is what "the Responses API" means
here, for a request and for every line.

## A package is a folder

```
install           runs once for each environment image
run               runs once a turn
…                 anything else your install or run needs
```

Two programs are a whole package. It travels as a gzip-compressed tar archive of that folder:
`tar -czf agent.tar.gz -C agent .`, then `PUT /v1/harnesses/agent --data-binary @agent.tar.gz`. The name in the
address is the harness's name (lower-case letters, digits and `-`), which a request names in `model`. `install`
and `run` must be executable, and may be written in any language the environment's image can run. The `shell`
package is the smallest that works.

## A package declares nothing

A package is what installs and runs an agent, and no more. Everything about where it runs is its environment's to
say, and an environment is the only door:

| The environment's | What it decides |
|---|---|
| `image`, and its limits | What `install` and `run` find to run with, and how much they may use |
| `harnesses` | Which packages a request may name with it |
| `network` | Every host a turn may reach, beyond its provider |
| `variables` | Values `run` finds under their own names: how an organization sets a package up |
| `secrets` | Values that never enter the sandbox, each with the hosts it may go to: `run` finds a placeholder under the secret's name |

So a package that needs a token for a tool server reads it from a variable (`$TOOL_TOKEN`, in a file of its own
or on its CLI's command line), and whoever sets up the environment gives it a secret of that name and the host it
goes to. Say in the package's `README.md` what its environment must give it: what the image must have, the
provider APIs its CLI speaks, and the variables, secrets and hosts it uses.

This is contract `3`, which `GET /v1/platform` reports as `contract`.

## The sandbox

Both programs run in a container of the environment's image, as a user that is not root, with a read-only root
file system. What can be written: `/mind` (which is `$HOME`) and `/work` during a turn, `/harness` during an
install, and `/tmp`, which is in memory and gone when the sandbox is. Nothing else survives a turn: not a running
process, not the sandbox. Of the two folders that persist, `~/.cache` is the exception, as its name says: it is
never saved with a session, so what need not be kept goes there.

The only way out is the egress proxy. The usual variables (`HTTPS_PROXY`, `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`
and the rest) are set, so most programs use it unasked. A turn reaches its provider and what its environment
allows; an install reaches the package managers (npm, PyPI, GitHub and the like) and what its environment allows.
A refused host answers `403` with a line saying why. A package adds no host: what its CLI needs beyond its provider
goes in its README, for whoever sets up the environment.

No secret enters the sandbox. The provider's key and every secret of the environment are placeholders; the proxy
puts the real value in on the way out, toward the hosts it is bound to.

## install

Runs in the environment's image, with `/harness` (this package, writable) as its working directory and `HOME` in
`/tmp`. It is given no provider, no variable and no secret, and 15 minutes. Install the CLI at a pinned version
under `$HG_HARNESS/tools`, by its makers' own method, and check the version it got. Name no CPU architecture. It
runs once for each image; after that `/harness` is read-only.

## run

Runs once a turn, in `/work`. It reads the request, starts its CLI, prints what the CLI does as Responses output
while it works, prints the stream's last event, and exits 0.

### What it finds

| | |
|---|---|
| `$HG_TURN/request.json` | The Responses request, as it was sent: every field of it |
| `$HG_TURN/files/<id>/<name>` | Each uploaded file the request's input names by `file_id` |
| `$HG_TURN/previous.json` | The response this request continues from (what its `previous_response_id` names), as the API returns it; `null` when it continues from none. Its `status` says how that turn ended, and its `output` holds what it asked its caller |
| `$HG_TURN/handover.txt` | The session's handover history, in words, when another harness worked on the session since this one last did; empty otherwise (see "Memory and handover") |
| `$HG_MIND` (`$HOME`), `$HG_WORK` | `/mind` and `/work`, the session's two folders: they persist from turn to turn, whichever harness runs it |
| `$HG_HARNESS` | `/harness`: the installed package |
| `$HG_CONTRACT` | `/etc/hg/contract`: this page, and the schemas |
| `$HG_STATE` | A folder under the mind for small values of the package's own: its CLI's session id, to resume it, and `memory`, its note of where its CLI keeps its record |
| `$HG_PROVIDER_ANTHROPIC`, `$HG_PROVIDER_OPENAI_RESPONSES`, `$HG_PROVIDER_OPENAI_CHAT`, `$HG_PROVIDER_GEMINI` | The provider's base URL for each API it speaks, as the organization wrote it: one variable for each, and none for an API it does not speak. `run` uses the one its CLI speaks, and ends the turn with `response.failed`, saying which it needs, when there is none |
| `$HG_MODEL`, `$HG_PROVIDER_KEY` | The model to call, and a placeholder for the provider's key. Both are empty when the request names no model (`none/none`) |
| `$HG_ENVIRONMENT`, `$HG_HARNESS_NAME`, `$HG_PROVIDER` | The names in the request's `model` |
| The environment's variables and secrets | Each variable under its own name, and a placeholder for each secret under its name |
| `$HG_SESSION`, `$HG_RESPONSE` | The ids of the session and of this turn's response |

### The request

`request.json` is the caller's body, checked only for being a Responses request: nothing in it is a field the API
does not have, or a value of the wrong kind. What a caller left out is not put in. The schema calls fields required
that OpenAI fills in when they are missing, so read each as OpenAI would have it: an image with no `detail` is
`auto`. A credential the request carries
for an MCP server it names (`authorization`, a value of `headers`) is a placeholder here, as every secret in a
sandbox is: the egress proxy puts the real one into requests to that server's host.

Every field of it is one of four things:

| | Fields | What `run` does |
|---|---|---|
| The platform's | `model`, `stream`, `stream_options`, `background`, `store`, `previous_response_id`, `metadata` | Nothing: the platform has acted on each. Streaming, storing, and continuing a session are the same to `run` whichever the caller chose |
| A label | `user`, `safety_identifier`, `prompt_cache_key` | Nothing: it changes nothing of the answer |
| Honoured | Whichever of the rest the CLI has a flag or a setting for | Gives it to the CLI |
| Not honoured | Whatever is left, and present | Ends the turn with `response.failed`, code `unsupported_parameter`, and a message that names the field and says to leave it out |

There is no fifth: a field is never left alone and passed over. A value the CLI cannot take (a tool of a type it
has no way to offer, an input part it cannot read, a `reasoning.effort` it has no level for) fails the turn the same
way, naming it. Say in the package's README which fields it honours.

| In the request | What honouring it means |
|---|---|
| `input` | The task: all of it. A string, or a list of items. A list may hold a whole conversation, when the caller keeps it and sends it each time: messages (`user`, `developer`, `system`, `assistant`) with `input_text`, `input_image`, and `input_file` parts, earlier calls with their outputs, and the items that answer what an earlier turn ended on (`function_call_output`, `mcp_approval_response`, …). Give the CLI every item, in order |
| `instructions` | The CLI's system prompt for this turn, and for this turn only: they do not carry from one response to the next, so a CLI that keeps a session's first ones must be given each turn's anew |
| `reasoning` | `effort`: how hard the CLI thinks. `summary`: whether its reasoning is printed |
| `text` | `format`: a JSON schema the final answer must fit. `verbosity` |
| `tools` | What the agent may use beyond its own tools. `mcp`: servers to connect its CLI to. `function` and `custom`: tools the caller runs (see "Asking the caller"). The rest (`web_search`, `shell`, `apply_patch`, …) name tools a CLI has of its own, to switch on or set up |
| `tool_choice`, `parallel_tool_calls`, `max_tool_calls` | Which tools it may call, and how many steps it may take |
| `max_output_tokens`, `temperature`, `top_p`, `truncation`, `context_management` | Passed to the CLI or its provider |

### What it prints

One JSON object a line, on standard output. Every line is a piece of an OpenAI response, in one of three forms.
Standard error is the turn's log, with whatever the CLI prints there.

**An output item, whole**: any item of the Responses API. The platform gives it its place in the output and, if
it has none, its `id`, and writes the stream's events for it.

```json
{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "Done.", "annotations": []}]}
{"type": "shell_call", "call_id": "c1", "status": "completed", "action": {"commands": ["ls"]}}
{"type": "shell_call_output", "call_id": "c1", "status": "completed", "output": [{"stdout": "a.txt\n", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}]}
```

**A stream event**, for what arrives in pieces: any event of the Responses stream but the ones about the response
as a whole, which are the platform's (`response.created`, `response.queued`, `response.in_progress`, `error`).

- A piece of text is `{"type": "response.output_text.delta", "delta": "…"}`, and a piece of reasoning
  `{"type": "response.reasoning_summary_text.delta", "delta": "…"}`. Printed like that, naming no item, each goes
  on a message (or a reasoning) the platform keeps: it begins with the first piece that has text and ends when
  anything else is printed.
- For anything else that arrives in pieces, print the item's own events: `response.output_item.added` with the
  item and an `id` of your making, the events that name it by `item_id`, then `response.output_item.done` with the
  item as it ended.

**The stream's last event**, once, last: how the run ended, with only what the package knows of the response.

```json
{"type": "response.completed", "response": {"usage": {"input_tokens": 1200, "output_tokens": 80}}}
{"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}}
{"type": "response.failed", "response": {"error": {"code": "no_auth", "message": "The provider refused the key."}}}
```

`usage` is optional in each, and may carry `input_tokens_details` (`cached_tokens`) and `output_tokens_details`
(`reasoning_tokens`). The `response` of `response.completed` may be left out. An error's `message` tells a person
what went wrong; its `code` is a short name of the package's own, which the caller finds in `metadata.hg_code`.
Whatever is printed after this event is ignored.

The bookkeeping is the platform's. It numbers the events and places the items, so a `sequence_number` or an
`output_index` in a line is replaced. Each line is checked against the OpenAI SDK's model of that item or event,
and the first one that is wrong fails the turn, with a message naming the field.

### What an agent does, as output items

Print a call when the CLI makes it, and what came of it when it returns, with the CLI's own id for the call as
the `call_id` of both: a step that takes long is then seen while it runs. An item that is one thing from its
start to its end (an `mcp_call`, a `web_search_call`) is printed as it begins and as it ends, with
`response.output_item.added` and `response.output_item.done`.

| The agent | The item |
|---|---|
| Says something | `message`. Its `phase` tells narration between steps (`commentary`) from the answer (`final_answer`); a `refusal` part is a refusal; an `output_text` part's `annotations` cite a web page (`url_citation`) or a file it made (`container_file_citation`) |
| Thinks | `reasoning`, with `summary` parts |
| Runs a command | `shell_call`, then `shell_call_output` with the same `call_id`, and the command's own exit code |
| Creates, changes or deletes a file | `apply_patch_call` (`operation.type`: `create_file`, `update_file`, `delete_file`; `operation.diff`: what changed), then `apply_patch_call_output` |
| Connects to an MCP server, and calls one of its tools | `mcp_list_tools` (with `error` when it could not connect), `mcp_call` |
| Searches the web, runs code, makes an image, uses a browser | `web_search_call`, `code_interpreter_call`, `image_generation_call`, `computer_call` |
| Shortens its own context | `compaction` |
| Uses any other tool of its own | `custom_tool_call`, then `custom_tool_call_output` |

`lines.schema.json` has every item and event, with every field.

### Asking the caller

The Responses API has no status for a response that waits. A turn that needs its caller ends as any other does,
`response.completed`, with the question as its last item; the answer comes in the next request's `input`, with
`previous_response_id`, and the CLI carries on from the session it saved. `previous.json` holds the response
that asked, so a package can match an answer to the call it answers.

A tool the caller runs is offered to the CLI as any other tool is: most take more tools from a tool server, so
`run` starts a small one inside the sandbox that lists the request's `function` tools. When the CLI calls one, that
server answers that the call is with the caller, `run` stops the CLI, and the call is printed. On the next turn
`run` gives the CLI the output and resumes its session. A package that cannot do this fails a request that has such
a tool, as it does any field it does not honour.

| The agent needs | The turn's last item | The next request's input holds |
|---|---|---|
| A tool the caller runs (a `function` or `custom` tool of the request) | `function_call`, or `custom_tool_call` | `function_call_output`, or `custom_tool_call_output`, with the same `call_id` |
| Leave to call an MCP tool | `mcp_approval_request` | `mcp_approval_response` |

### Memory and handover

A session's whole state is its two folders. The platform saves both after every turn and puts them back before the
next, whichever environment, harness, provider, or model that turn names. Everything else is gone with the sandbox.
So a package has two duties to its CLI's memory, and both are things only a package can know:

1. **Keep it inside the mind.** Most CLIs keep their record of a session under `$HOME`, which is the mind, and
   need nothing. One that keeps it anywhere else is pointed into the mind by its package. `~/.cache` is never
   saved.
2. **Say where it is.** Write `$HG_STATE/memory` every turn: a few lines, in your own words, of where your CLI
   keeps its record of this session, as paths under `/mind`, and what form it is in.

   ```
   /mind/.my-agent/sessions/0199a2.jsonl: this session's transcript, one JSON object a line, oldest first:
   the person's messages, the agent's, and each tool call with its result.
   ```

A session continues when `run` gives its CLI the CLI's own session id from the turn before: save it under
`$HG_STATE` the moment the CLI reports it, and resume with it. When it cannot be resumed, start a new session and
say so on standard error.

A session may change harness from one turn to the next. The platform hands the new harness nothing of what was
said. When another harness worked on the session since yours last did, `handover.txt` holds the session's handover
history: who worked on it, in order, the responses each gave, and each one's `memory` note. **When `handover.txt` is
not empty, give its text to your CLI with the request, before the person's words.** The CLI reads what it needs
from the other harness's memory by itself. A harness that returns to a session is handed over to as well.

### Stopping

When a turn is stopped (cancelled, or past its environment's time limit), `SIGTERM` is sent to `run` and to
everything it started, at once, and they have 15 seconds to end. The sandbox ends when `run` does,
so `run` must outlive its CLI: take the signal, do nothing with it, and go on reading until the CLI has ended,
which is what gives the CLI the time to save its session. Then exit; the platform says how a stopped turn ended,
whatever `run` printed. What it left is saved, and the next turn carries on from it: `previous.json` then has
`"status": "cancelled"`, which an agent that did not record being stopped can be told.

### The smallest `run`

```python
#!/usr/bin/env python3
import json, os, signal, subprocess

request = json.load(open(os.path.join(os.environ["HG_TURN"], "request.json")))
signal.signal(signal.SIGTERM, lambda *_: None)          # outlive the CLI when the turn is stopped
cli = subprocess.Popen(["my-agent", "--json"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
cli.stdin.write(request["input"])                       # a string here: an input may also be a list of items
cli.stdin.close()
for line in cli.stdout:                                 # the CLI's output, a line at a time
    event = json.loads(line)
    if event["type"] == "text":
        print(json.dumps({"type": "response.output_text.delta", "delta": event["text"]}), flush=True)
    elif event["type"] == "command":
        print(json.dumps({"type": "shell_call", "call_id": event["id"], "status": "completed",
                          "action": {"commands": [event["command"]]}}), flush=True)
print(json.dumps({"type": "response.completed"}), flush=True)
```

It is the shape, and not yet the whole contract: a finished `run` also fails a request with a field it does not
honour, gives its CLI `handover.txt`, resumes the CLI's session, and writes `$HG_STATE/memory`. The `shell` package
does each of them, in a few lines.
