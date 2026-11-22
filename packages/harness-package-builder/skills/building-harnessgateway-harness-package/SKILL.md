---
name: building-harnessgateway-harness-package
description: Build a HarnessGateway harness package for an agent CLI - research the CLI, decide how the OpenAI Responses request and response map onto it, write harness.json, install and run, verify it with a hello world, and hand it over with a link that adds it. Use for any request to make, fix or change a harness package.
---

# Building a HarnessGateway harness package

A harness package is a folder: `harness.json`, `install`, `run`. The platform knows no agent CLI; everything about
one is in its package. It speaks one schema, the OpenAI Responses API's: `run` reads the caller's Responses request
exactly as it was sent, and prints Responses output. **How much of that request the agent honours, and how much of
what the agent does the caller sees, is decided by the package you write.** Your job is to make that as much as
this CLI allows.

The platform puts two files beside every installed package, and so beside you:

- `/harness/contract/PACKAGES.md`: the whole contract. Read it once before step 2.
- `/harness/contract/output.schema.json`: every output item and stream event a `run` may print, with every field.

The package you are running from is a complete, working one: `/harness/harness.json`, `/harness/install`,
`/harness/run`.

Work through the four steps in order. Keep `/work/.notes/<name>.md` as you go: it is how you carry on in the next
turn without finding everything out again.

## 1. Research the CLI

From the CLI itself, in this order: its registry page and repository, its documentation, then **install it and run
its `--help`** (and the help of its subcommands), which wins over anything you remember or read. You can reach
the web: use `curl`. Write each answer in the notes, with where you learned it.

| Find out | It decides |
|---|---|
| The latest **stable** version, and its makers' own way to install it on Linux | `install`: pinned, under `$HG_HARNESS/tools`, the version checked |
| Its headless mode, and its machine-readable output (JSON lines, a stream, a protocol on its standard input and output) | `run`: the command, and what each thing it reports becomes |
| How it is given a base URL, a key and a model, and **which provider APIs it speaks** | `apis` in `harness.json`; the variables `run` sets |
| Where under `$HOME` it keeps a session, and how one is continued by its id | `mind` in `harness.json`; resuming in `run` |
| How it is told to act without asking, and how updates and telemetry are turned off | `run`'s flags and variables |
| Where it reads instructions (an `AGENTS.md` or its own file), skills, MCP servers and settings | What the package gives it: see "What the CLI is given" |

Then run it once by hand, headless, against this session's provider (`$HG_PROVIDER_URL`, `$HG_PROVIDER_KEY`,
`$HG_MODEL`; the key is a placeholder that works only toward the provider), and keep what it printed in the notes:
the shape of a piece of text, of reasoning, of a command and its result, of a file written, of a tool call, of an
error, and of the end with its token counts.

**API shapes.** List every API the CLI speaks in `apis`, the one it does best first: `anthropic`,
`openai-responses`, `openai-chat`, `gemini`. The platform picks the first the request's provider also speaks and
tells `run` which in `$HG_PROVIDER_API`; `run` sets the CLI up for that one. You can run the CLI here only on the
API this session's provider speaks (`$HG_PROVIDER_API`). Write the others from the CLI's documentation, and say at
the hand-over which were run and which were not.

### Decide the mapping

Before writing `run`, fill in two tables in the notes. They are the design of the package.

**The request, field by field.** For each of these, write how this CLI is given it (a flag, a variable, a line of
its config, its standard input), or "not honoured" and why. `PACKAGES.md` says what each means.

| Field of the request | Ask of the CLI |
|---|---|
| `input`: text, images, files, and messages of each role | How does it take a prompt, and can it take an image or a file by path? |
| `instructions` | Has it a system prompt for one run (a flag, a file)? |
| `reasoning.effort` | Has it a thinking level? |
| `text.format` | Can it be held to a JSON schema for its answer? |
| `tools` of type `mcp` | How is it given MCP servers, with their headers, and can it be limited to `allowed_tools`? |
| `tools` of type `function` or `custom` | Can a tool the caller runs be offered to it (through an MCP server your `run` starts beside it, say), and the turn ended when it calls one? |
| `tool_choice`, `max_tool_calls` | Can its tools be switched off or narrowed, and its steps limited? |
| `max_output_tokens`, `temperature`, `top_p` | Has it a setting that reaches the provider? |

**What it reports, thing by thing.** For each thing the CLI reports, write the output item it becomes. The table
"What an agent does, as output items" in `PACKAGES.md` is the list; for the fields of any one of them:

```
python3 /harness/skills/building-harnessgateway-harness-package/verify.py --shape shell_call_output
```

Use the item that says what happened, not a general one: a command is a `shell_call`, not a `custom_tool_call`; a
web search is a `web_search_call`. Text and reasoning that arrive in pieces are printed in pieces. A step limit or
an output limit the CLI reports is `response.incomplete`; an error of the CLI or of the provider is
`response.failed` with a message a person can act on, never a silent exit.

## 2. Write the package

In `/work/<name>/`, where `<name>` is the package's name (lower-case letters, digits and `-`):

- **`harness.json`**: only the fields the contract names. The version is the CLI's, then `-1`.
- **`install`**: a shell script. It checks the image has what is needed, installs the CLI at the pinned version
  under `$HG_HARNESS/tools` by its makers' method, and checks the version it got. It names no CPU architecture.
- **`run`**: in a language the image already has (Python 3, standard library only, unless there is a reason). It
  has the shape of `/harness/run`: read `$HG_TURN/request.json`; the CLI's command and environment, from the
  request, as your first table says; each thing the CLI reports printed as a line of Responses output, as your
  second table says; the CLI's session id saved under `$HG_STATE` the moment it is known, and used to resume; a
  new session when the old one cannot be resumed; the stream's last event; and a stop outlived, as the contract
  says.
- **`README.md`**: what it is; what it needs from an image; which APIs it speaks, and which of them were run;
  **which fields of the request it honours and which it does not**; what an environment must allow beyond the
  provider; what must be set; what could not be tried.

Only Responses output goes to standard output: a line that is anything else fails the turn. What the CLI prints
aside, and anything for the turn's log, goes to standard error. `run` and the CLI write nothing under `/harness`
during a turn: it is read-only there.

### What the CLI is given

A package may give its CLI instructions, skills, MCP servers and settings of its own. `/harness` is read-only
during a turn and the mind belongs to the session, so there are three ways, best first:

1. **Kept in the package and pointed at**, when the CLI has an option or a variable for it (a config file, a
   plugin or skills folder, a system-prompt file). Nothing is copied, so nothing drifts.
2. **Copied into the mind by `run`** at the start of a turn, when the CLI reads only under `$HOME`. Copy what is
   missing or different; never overwrite what the session wrote.
3. **Written each turn** from the request and the manifest: the model, the provider, the request's MCP servers.

What an organization may set is a `setting` in `harness.json` (it reaches `run` as a variable); a value that must
not enter the sandbox is a `secret`, with the hosts it may go to. No key, token or placeholder is in any file.

## 3. Verify it

```
python3 /harness/skills/building-harnessgateway-harness-package/verify.py /work/<name>
```

It lays the package out as the platform does; runs its `install`; then runs a hello-world turn in a new session
and holds every line to the schema the platform checks a `run` by. It ends with one of:

- **`VERIFIED`**: go on to the turns below.
- **`NOT VERIFIED`**, and why, naming the line and the field: fix it, and verify again. Read what the turn
  printed; do not guess.
- **`INSTALL VERIFIED ONLY`**: the CLI speaks none of the APIs this session's provider speaks, so its hello world
  cannot run here. Go to step 4 and say so plainly: the person's first message to the new harness is its hello
  world.

Give the command ten minutes: your shell tool stops a command after two unless you ask for more. Then try what
your tables promise, one more turn at a time in the same session:

```
python3 /harness/skills/building-harnessgateway-harness-package/verify.py /work/<name> --say "What did I just ask you?"
python3 /harness/skills/building-harnessgateway-harness-package/verify.py /work/<name> --say "Write hello.txt, then list the folder."
python3 /harness/skills/building-harnessgateway-harness-package/verify.py /work/<name> --request /work/.notes/request.json
```

The first shows it remembers; the second that a command and a file are printed as the items they are; and
`--request` sends a Responses request of your writing (`instructions`, `reasoning`, `tools`, an `input` that is a
list), to try each field you said the package honours. `--stop-after 5` stops a turn the way a cancelled one is
stopped. A field you could not try is "not tried" in the README, not "honoured". When it is verified, remove
`/work/.trial`: an installed CLI is hundreds of megabytes saved with every turn.

## 4. Hand over

End your reply with this, the package's own values filled in. Write the link exactly as it is here, with only
the name changed: it names no session, because the page that shows your reply knows which session it is. Never
type a session's id: copied by hand, one is easily got wrong.

> **`<name>` `<version>` is ready.** It speaks `<apis>`; verified here on `<the API that was run>`.
>
> **[Add `<name>` as a harness](/console/#/harnesses/add/<name>)**
>
> Or over the API: `PUT /v1/harnesses/<name>` with `{"session": "<this response's metadata.hg_session>", "path": "<name>"}`.
>
> Then send it a first message: `<environment>/<name>/<provider>/<model>`.

and after it, in a few lines: which fields of the request it honours; what its environment's image must have; what
the environment must allow, if anything beyond the provider; what must be set; and what was not tried. If the
person comes back with a failure from the platform, they will paste its log: fix the package in `/work/<name>/`,
verify again, and hand over again.
