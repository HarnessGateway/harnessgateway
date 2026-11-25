---
name: building-harness-packages
description: Build a HarnessGateway harness package for any harness CLI - research the CLI, write install and run against the OpenAI Responses schema, and hand it over. Use for any request to make, fix, or change a harness package.
---

# Building a harness package

The method is **Building Agents Using Harness CLIs**: seven steps that turn a CLI someone uses at a terminal into
the backend of an agent. Every harness has the same five primitives, each implemented its own way, so the steps
are the same for any CLI. Here the program you author is the package: its JSON in is a Responses request, and its
JSON out is Responses output.

Do the steps in order. Keep notes as you go: every answer as the exact flag, variable, or file, and where you
found it (help, documentation, or test). The next turn carries on from the notes.

## 1. Find the latest stable version

Look it up where the CLI is published. Take the latest stable one, not a preview. Everything you learn is true of
that version.

## 2. Install it

Install that exact version into a folder, by its makers' own method, with nothing global. Check the version it
reports.

## 3. Read its help

At the top level, then for every command. It is the installed version describing itself.

## 4. Read its documentation

For what the help leaves out. Where the two disagree, the help wins, and both win over what you remember.

## 5. Learn how to drive its primitives

Answer one question for each primitive. Then find where each thing the request carries lands in this CLI: a flag,
a setting, a file, or the message itself. No two CLIs take them the same way.

| Primitive | The question | What the request carries for it |
|---|---|---|
| **Input** | How do I send it a request? | `input`: a string, or a list of items. Messages (`user`, `developer`, `system`, `assistant`) with `input_text`, `input_image`, and `input_file` parts. Items an earlier response returned. Answers to its calls: `function_call_output`, `custom_tool_call_output`, `mcp_approval_response` |
| **Context** | How do I get it context that makes the request useful? | `instructions`, for this response only. Its own session, resumed. `handover.txt`. What the package itself carries: instructions, skills, tools |
| **Model interface** | How do I configure its requests to the model? | The provider's base URL, key, and model. `reasoning`, `temperature`, `top_p`, `max_output_tokens` |
| **Output** | How do I handle what comes out? | What it prints, as output items and stream events, and the tokens it used. `text.format`: a schema the answer must fit. `tools` and `tool_choice`: what it may use, and which tools are the developer's to run |
| **Control flow** | How do I control its loop? | One turn, with nobody there, acting without asking: the sandbox is what holds it. `parallel_tool_calls`, `max_tool_calls`. A stop. How a run is ended from inside a tool call, to hand a call back to the developer |

Where a CLI has no place of its own for something that changes with every response, the message is the place.

## 6. Test it

Run the CLI on what you learned, the way a program would. Only what you have run is known: a CLI's help can list
what it then refuses. Use this session's provider: the base URL for the API the CLI speaks, `$HG_PROVIDER_KEY`,
and `$HG_MODEL`.

Test it as a developer would use it: the session in `/harness/requests`, request by request.

Then the edges: what it does when something is missing, wrong, refused, or stopped. When an answer is wrong, run
it again before you change anything: a model's mistake looks like a fault of the harness.

## 7. Author the package

In `/work/<name>/`: `README.md`, `install`, and `run`, as `/harness/example` has them. That is a whole package. Add
a file of its own only for what was asked for beyond the CLI as it comes: who the agent is, a skill, a tool
server, a setting. Each is a file in the package, and one line of `run` that points the CLI at it.

**`install`** puts the CLI, at the version you tested, under `$HG_HARNESS/tools`, and checks the version it got.

**`run`** is five functions, one for each primitive, that together turn the request into the CLI's command, and
then the reading of what the CLI prints:

```python
def as_input(request): ...         # input: the request's input, in the form the CLI takes it
def context(request, session): ... # context: instructions, its own session to carry on from, the handover
def model_interface(request): ...  # model interface: endpoint, key, model, and reasoning
def output(request): ...           # output: every step as a line of JSON, the schema, the tools it may use
def control_flow(): ...            # control flow: run once with nobody there, and exit
```

Each thing the CLI reports is printed as the item that says what it was:

| The agent | The item |
|---|---|
| Says something | `message`, or `response.output_text.delta` as the words arrive |
| Thinks | `reasoning`, or `response.reasoning_summary_text.delta` |
| Runs a command | `shell_call`, then `shell_call_output` |
| Changes a file | `apply_patch_call`, then `apply_patch_call_output` |
| Calls a tool server | `mcp_list_tools`, `mcp_call` |
| Searches the web | `web_search_call` |
| Uses another tool of its own | `custom_tool_call`, then `custom_tool_call_output` |
| Calls a tool that is the developer's | `function_call` or `custom_tool_call`, last, and the turn ends `response.completed` |

The last line says how the run ended, with the tokens it used.

**`README.md`** says what the package honours of a request, and what its environment must give it: what the image
must have, which provider APIs the CLI speaks, and the variables, secrets, and hosts it uses.

Then run the session of step 6 through the package, the way the platform does, and fix what you find.

## 8. Hand over

End your reply with the package's name, the CLI and version it runs, the provider APIs it speaks, and this link,
with only the name changed:

> **[Add `<name>` as a harness](/console/#/harnesses/add/<name>)**

The person adds it and tries it on the platform. If it fails there, they come back with the turn's log.
