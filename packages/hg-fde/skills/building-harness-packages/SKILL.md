---
name: building-harness-packages
description: Build a HarnessGateway harness package for an agent CLI - research the CLI, write install and run, and hand it over with a link that adds it. Use for any request to make, fix or change a harness package.
---

# Building a harness package

A harness package is a folder with two programs, `install` and `run`. Its goal is to install and run one agent
harness CLI. The platform knows no CLI: everything about one is in its package. `run` reads the caller's OpenAI
Responses request and prints Responses output.

The method is **Building Agents Using Harness CLIs**: seven steps that turn a CLI someone uses at a terminal into
the backend of an agent, which a program starts, feeds, reads, and stops with nobody at the keyboard. Here the
program is the package, its JSON in is a Responses request, and its JSON out is Responses output.

The contract and the schemas are in your instructions, in full, and as files in `$HG_CONTRACT`. The package you are
running from is a finished one to read, written this way: `/harness/install` and `/harness/run`.

Do the steps in order. Keep `/work/.notes/<name>.md` as you go: every answer as the exact flag, variable, or file,
and where you found it (help, docs, or test). The next turn carries on from the notes.

## 1. Find the latest stable version

Look it up where the CLI is published: its package registry or its releases. Take the latest stable one, not a
preview, beta, or nightly. Where its makers mark a version as stable (a `stable` tag beside `latest`), that is the
one. Write the exact version down. Everything you learn is true of that version. You can
reach the web: use `curl`.

## 2. Install it

Install that exact version into a folder under `/tmp`, by its makers' own method for Linux, with nothing global.
Run it once to print its version, and check it is the one you chose.

## 3. Read its help

Run its help at the top level, then for every command and subcommand. Keep the output in the notes: it is the
installed version describing itself.

## 4. Read its documentation

Read it for what the help leaves out. Note every place the two disagree: **the help wins** over the documentation,
which is often for another version, and both win over what you remember.

## 5. Learn how to drive its primitives

Every harness has the same five primitives, each implemented its own way. Answer the framework's question for
each, then the platform's: what of the request and of the sandbox it has to carry.

| Primitive | The question | What it carries on the platform |
|---|---|---|
| **Input** | How do I send it a request? | The request's `input`, all of it: a string, or a list that may hold a whole conversation, with images and files. Uploaded files are at `$HG_TURN/files/<id>/<name>` |
| **Context** | How do I get it context that makes the request useful? | `instructions`, as its system prompt for this turn only. Its own session, resumed from the id kept in `$HG_STATE`. `$HG_TURN/handover.txt`, when another harness worked on the session. Where it keeps its record of a session, which must be under `/mind` |
| **Model interface** | How do I configure its requests to the model? | The provider's base URL (`$HG_PROVIDER_ANTHROPIC`, `$HG_PROVIDER_OPENAI_RESPONSES`, `$HG_PROVIDER_OPENAI_CHAT`, `$HG_PROVIDER_GEMINI`: one for each API the provider speaks), `$HG_PROVIDER_KEY`, `$HG_MODEL`. `reasoning`, and the sampling fields where it has a setting |
| **Output** | How do I handle what comes out? | Its machine-readable output, each line of it as a line of `lines.schema.json`. `text.format`, a schema the answer must fit. `tools` and `tool_choice`: what it may use |
| **Control flow** | How do I control its loop? | One turn, headless, acting without asking: the sandbox is what holds it. `SIGTERM`, which stops it. How a run is ended from inside a tool call, which is how a tool the caller runs is handed back |

Also find out what it needs to load its configuration with no person there, and to turn off its updates.

A CLI that speaks only to its makers' service is named `environment/<name>/none/none`, and takes its makers' key
from a secret of its environment.

## 6. Test it

Run the CLI on what you learned, the way a program would, and watch how it behaves. Only what you have run is
known. Use this session's provider: the base URL in the variable for the API the CLI speaks, `$HG_PROVIDER_KEY`,
and `$HG_MODEL`. The key is a placeholder that works only in a header or a query sent to the provider, through the
proxy the sandbox's variables already point at.

- One headless turn. Keep every line it printed in the notes: they are what `run` will read.
- A second turn that continues the first. Find where its record of the session was written.
- The edge cases: what it does when something is missing, wrong, refused, or stopped. A model that does not exist.
  A session id that is gone. `SIGTERM` in the middle of a step: whether it saves its session, and how long it takes.

Judge it as the backend of an agent in production, and write what you found in the notes.

## 7. Author the package

In `/work/<name>/`, where `<name>` is the package's name, and nothing else there.

**`install`**: a shell script. It checks the image has what is needed, installs the CLI at the pinned version under
`$HG_HARNESS/tools` by its makers' method, and checks the version it got. It names no CPU architecture.

**`run`**: in a language the image already has (Python 3, standard library only, unless there is a reason). It has
the shape of `/harness/run`: one function for each primitive, which together turn the request into the CLI's
command, its variables, and what it is fed; and the reading of what the CLI prints.

```python
def as_input(request): ...         # input: the request's input, in the form the CLI takes it
def context(request, session): ... # context: instructions, its own session to carry on from, the handover
def model_interface(request): ...  # model interface: endpoint, key, model, and reasoning
def output(request): ...           # output: every step as a line of JSON, the schema, the tools it may use
def control_flow(): ...            # control flow: run once with nobody there, and exit
```

What the contract asks of every `run`, each of which the platform's own checks hold a package to:

- **Honour or fail.** Every field of the request is the platform's, a label (the contract lists both), one the
  package honours, or one that ends the turn with `response.failed`, code `unsupported_parameter`, and a message
  naming the field. So does a value the CLI cannot take. Find this before the CLI is started. A value that asks
  only for what `run` does anyway is honoured, not failed: `require_approval: "never"`, to a CLI that never asks.
  A field the schema calls required may be missing: read it as OpenAI would (an image with no `detail` is `auto`).
- **The provider.** Use the first API the CLI speaks that the provider has a variable for; when there is none, end
  with `response.failed` and say which APIs the CLI needs.
- **Responses output, and nothing else on standard output.** Print each thing the CLI reports as the item that
  says what happened: a command is a `shell_call`, a file written is an `apply_patch_call`, a call to a tool server
  is an `mcp_call`. Print a call when the CLI makes it, and what came of it when it returns. Text goes as
  `response.output_text.delta` as it arrives. Everything else goes to standard error.
- **Its session.** Save the CLI's session id under `$HG_STATE` the moment it is known, and resume with it on the
  next turn. When it cannot be resumed, start a new one and say so on standard error.
- **Its memory.** The CLI's record of the session must be under `/mind`, which is `$HOME`: point it there if it is
  kept anywhere else. Write `$HG_STATE/memory`: a few lines saying where that record is, as paths under `/mind`,
  and what form it is in. The harness that takes the session over is pointed at it.
- **The handover.** When `$HG_TURN/handover.txt` is not empty, give its text to the CLI before the person's words.
- **The turn before.** When `$HG_TURN/previous.json` says it did not complete, tell the CLI so.
- **A tool the caller runs.** Offer the request's `function` tools to the CLI as the contract describes ("Asking
  the caller"), and end the turn on the call. If the CLI gives no way to do it, fail a request that has one.
- **Stopping.** Take `SIGTERM`, wait for the CLI to end, and exit. End every other turn with the stream's last
  event: `response.completed`, or `response.failed` with a message a person can act on.

A package declares nothing: what it may reach and hold is its environment's to say. So add a `README.md` that says
what its environment must give it: what the image must have, which provider APIs it speaks, which variables and
secrets it reads and the hosts each secret goes to, and which fields of the request it honours.

**Then run it once the way the platform does**, and fix what you find:

```sh
cp -r /work/<name> /tmp/try && cd /tmp/try && HG_HARNESS=/tmp/try HOME=/tmp/home ./install
mkdir -p /tmp/turn /tmp/tried && echo null > /tmp/turn/previous.json && : > /tmp/turn/handover.txt
echo '{"model": "e/<name>/p/m", "input": "Say hello in three words."}' > /tmp/turn/request.json
cd /tmp/tried && HG_HARNESS=/tmp/try HG_TURN=/tmp/turn HG_STATE=/tmp/home/.harnessgateway/<name> HOME=/tmp/home /tmp/try/run
```

Every line it prints must be one of `lines.schema.json`, the last of them the stream's last event. Run it again with
another `input` to see the session continue, and once with a field it does not honour. Then check `instructions`:
a turn that has them must follow them, and the next turn of the same session, sent without them, must not.

## 8. Hand over

End your reply with this, the package's own values filled in. Write the link exactly as it is here, with only the
name changed.

> **`<name>` is ready to add.** It runs `<the CLI and its version>`, and speaks `<apis>`.
>
> **[Add `<name>` as a harness](/console/#/harnesses/add/<name>)**
>
> Or over the API: `PUT /v1/harnesses/<name>` with `{"session": "<this response's metadata.hg_session>", "path": "<name>"}`.
>
> Then send it a first message: `<environment>/<name>/<provider>/<model>`.

and after it, in a few lines: which fields of the request it honours, and what its environment must give it:
the image, and any variables, secrets and hosts. The person adds it and tries it on the platform: if it fails
there, they will paste the turn's log, and you fix the package in `/work/<name>/` and hand it over again.
