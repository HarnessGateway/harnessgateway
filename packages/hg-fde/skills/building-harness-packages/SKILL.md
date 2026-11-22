---
name: building-harness-packages
description: Build a HarnessGateway harness package - find out the agent that is wanted, research the agent CLI it runs on, decide how the OpenAI Responses request and response map onto it, write harness.json, install, run and the agent's own folder, prove what the package does, and hand it over with a link that adds it. Use for any request to make, fix, change or bring up to date a harness package.
---

# Building a harness package

A harness package is a folder: `harness.json`, `install`, `run`. The platform knows no agent CLI; everything about
one is in its package. It speaks one schema, the OpenAI Responses API's: `run` reads the caller's request and
prints Responses output. **How much of that request the agent honours, and how much of what the agent does the
caller sees, is decided by the package you write.**

The contract and the four schemas are in your instructions, in full, and as files in `$HG_CONTRACT`. Two finished
packages are yours to read: the smallest one, in the contract ("The smallest `run`"), and the one you are running
from, `/harness/harness.json`, `/harness/install` and `/harness/run`, which drives a real agent CLI.

A package has two parts, and you keep them apart:

- **The adapter**: `harness.json`, `install` and `run`. How this CLI is started from a request, and how what it
  prints becomes Responses output. It is the same for every agent built on this CLI.
- **The agent**: a folder `agent/` in the package, holding what makes this agent this one: its instructions, its
  skills, its hooks, its tool servers, its settings. `run` gives it to the CLI. Someone who wants another agent on
  the same CLI changes `agent/` and nothing else.

Work through the six steps in order. Keep `/work/.notes/<name>.md` as you go: it is how you carry on in the next
turn without finding everything out again.

## 1. Find out the agent that is wanted

What it is for; which agent CLI, if they have one in mind; what it must be able to use (tools, tool servers,
skills) and what it must never do; and anything it must always be told. Ask only what changes what you build, and
ask by ending your reply. Given only a CLI's name, the agent wanted is that CLI as it is: build its adapter, with
no `agent/` folder, and ask nothing.

## 2. Research the CLI

From the CLI itself, in this order: its registry page and repository, its documentation, then **install it and run
its `--help`, and the help of every subcommand**, which wins over anything you read or remember. You can reach the
web: use `curl`. Write each answer in the notes, with where you learned it.

| Find out | It decides |
|---|---|
| The latest **stable** version, and its makers' own way to install it on Linux | `install`: pinned, under `$HG_HARNESS/tools`, the version checked |
| Whether its documentation is for the version that installs. A site often describes the next major version | Which of what you read you can use at all |
| Its headless mode: how one turn is run, and where the prompt goes (an argument, its standard input, a file) | `run`: the command |
| Its machine-readable output, and what each line of it means | `run`: what each thing it reports becomes |
| How it is given a base URL, a key and a model, and **which provider APIs it speaks**. Some speak only to their makers' own service | `apis` in `harness.json`, or none and a `secret` for the maker's key; the variables `run` sets |
| Where it keeps a session, under `$HOME` or in the working folder, and how one is continued: an id it reports, an id or a name you give it, a file | `mind` in `harness.json`; resuming in `run` |
| Where its system prompt goes for one run (a flag, a variable, a file), or that it has none | How a request's `instructions` reach it |
| Its door for each of: instruction files, skills, tool servers, hooks, settings, subagents. A door is a flag, a variable, a file under its own folder in `$HOME`, a file in the working folder, or a plugin folder | How `run` gives it the `agent/` folder |
| What it needs before it trusts and loads configuration when no person is there: a trust flag, a flag for hooks, a flag to run outside a repository | `run`'s flags. Without them it runs, and silently loads nothing |
| How it is told to act without asking, and how updates and telemetry are turned off | `run`'s flags and variables |
| Whether it has a flag for a thinking level, for an answer held to a JSON schema, for an image or a file | Which fields of the request it can honour |

Then run it once by hand, headless, against this session's provider (`$HG_PROVIDER_URL`, `$HG_PROVIDER_KEY`,
`$HG_MODEL`; the key is a placeholder that works only toward the provider), and keep what it printed in the notes:
the shape of a piece of text, of reasoning, of a command and its result, of a file written, of a tool call, of an
error, and of the end with its token counts.

**API shapes.** List every API the CLI speaks in `apis`, the one it does best first: `anthropic`,
`openai-responses`, `openai-chat`, `gemini`. The platform picks the first the request's provider also speaks and
tells `run` which in `$HG_PROVIDER_API`; `run` sets the CLI up for that one. You can run the CLI here only on the
API this session's provider speaks (`$HG_PROVIDER_API`). Write the others from the CLI's documentation, and say at
the hand-over which were run and which were not. A CLI that speaks only to its makers' service has `apis: []`, is
named `environment/<name>/none/none`, and takes its makers' key as a `secret` bound to that service's host.

## 3. Decide the mapping

Before writing `run`, fill in two tables in the notes. They are the design of the adapter.

**The request, field by field.** Go through `request.schema.json`. For each field, write how this CLI is given it
(a flag, a variable, a line of its config, its standard input), or "not honoured" and why. These are the ones that
most often have a door:

| Field of the request | Ask of the CLI |
|---|---|
| `input`: text, images, files, and messages of each role | How does it take a prompt, and can it take an image or a file by path? |
| `instructions` | Has it a system prompt for one run? If it has none, they go at the head of the prompt, or in an instruction file written for the turn |
| `reasoning.effort` | Has it a thinking level? |
| `text.format` | Can it be held to a JSON schema for its answer? |
| `tools` of type `mcp` | How is it given tool servers, with their headers, and can it be limited to `allowed_tools`? |
| `tools` of type `function` or `custom` | Can a tool the caller runs be offered to it (through a tool server your `run` starts beside it, say), and the turn ended when it calls one? |
| `tool_choice`, `max_tool_calls` | Can its tools be switched off or narrowed, and its steps limited? |
| `max_output_tokens`, `temperature`, `top_p` | Has it a setting that reaches the provider? |

`previous.json` is the response this request continues from. Use it for two things: when its `status` is not
`completed`, tell the agent its last turn did not finish, since many CLIs do not record being stopped and will
do the work again or forget it was asked; and when the request answers a call (`function_call_output` and the
like), find the call there.

**What it reports, thing by thing.** For each thing the CLI reports, write the output item it becomes. The table
"What an agent does, as output items" in the contract is the list, and `lines.schema.json` has every field; for one
item alone:

```
python3 /harness/skills/building-harness-packages/verify.py --shape shell_call_output
```

Use the item that says what happened, not a general one: a command is a `shell_call`, not a `custom_tool_call`; a
call to a tool server is an `mcp_call`, with the `server_label` the request or the agent gave that server; a web
search is a `web_search_call`. Text and reasoning that arrive in pieces are printed in pieces. A step limit or an
output limit the CLI reports is `response.incomplete`; an error of the CLI or of the provider is `response.failed`
with a message a person can act on, never a silent exit.

## 4. Write the package

In `/work/<name>/`, where `<name>` is the package's name (lower-case letters, digits and `-`):

- **`harness.json`**: only the fields the contract names. The version is the CLI's, then `-1`.
- **`install`**: a shell script. It checks the image has what is needed, installs the CLI at the pinned version
  under `$HG_HARNESS/tools` by its makers' method, and checks the version it got. It names no CPU architecture.
- **`run`**: in a language the image already has (Python 3, standard library only, unless there is a reason). It
  has the shape of `/harness/run`: read `$HG_TURN/request.json` and `previous.json`; the CLI's command and
  environment, from the request, as your first table says; each thing the CLI reports printed as a line of
  Responses output, as your second table says; the CLI's session id saved under `$HG_STATE` the moment it is known,
  and used to resume; a new session when the old one cannot be resumed; the stream's last event; and a stop
  outlived, as the contract says.
- **`agent/`**, when the agent wanted is more than the CLI as it is. In the forms most CLIs already read:
  `AGENTS.md` for what it is always told; `skills/<name>/SKILL.md`; `hooks/hooks.json` with the programs it names;
  `mcp.json` for its own tool servers; `settings.json` for what it may and may not do.
- **`README.md`**: what the agent is for; what it needs from an image; which APIs it speaks, and which were run;
  which fields of the request it honours and which it does not; what an environment must allow beyond the
  provider; what must be set; what was proven, and what could not be tried.

Only Responses output goes to standard output: a line that is anything else fails the turn. What the CLI prints
aside, and anything for the turn's log, goes to standard error. `run` and the CLI write nothing under `/harness`
during a turn: it is read-only there.

### Giving the CLI its agent

`/harness` is read-only during a turn, and `$HOME` is the session's, shared by every harness that works on it. So
there are three ways to give a CLI a part of `agent/`, best first:

1. **Pointed at, where it is**, when the CLI has a flag or a variable for it (a plugin folder, a config file, a
   skill's path, a system-prompt file). Nothing is copied, so nothing drifts, and a new version of the package is
   the agent's next turn.
2. **Copied by `run` into the CLI's own folder under `$HOME`**, at the start of every turn, when it reads only
   there. Copy what the package has over what is there, so a new version of the package takes effect; never touch
   what the session itself wrote.
3. **Copied into a place several CLIs read** (a shared skills folder in `$HOME`, the working folder) only when the
   CLI has neither. Another harness on the same session will read it too, and the working folder is the caller's:
   say so in the README.

Decide whether the working folder may configure the agent. Most CLIs read instructions, settings and hooks from
the folder they work in, and the agent can write there: unless that is wanted, use the CLI's flag that limits what
it loads. What the request asks for joins what the agent has: a request's tool servers beside the agent's own; a
request's `instructions` after the agent's.

What an organization may set is a `setting` in `harness.json` (it reaches `run` as a variable); a value that must
not enter the sandbox is a `secret`, with the hosts it may go to. No key, token or placeholder is in any file.

## 5. Prove it

```
python3 /harness/skills/building-harness-packages/verify.py /work/<name> --suite
```

It lays the package out as the platform does; runs its `install`; runs a hello-world turn in a new session, and
holds every line to the schema the platform checks a `run` by. It ends with one of:

- **`VERIFIED`**, and then the suite's results.
- **`NOT VERIFIED`**, and why, naming the line and the field: fix it, and verify again. Read what the turn
  printed; do not guess.
- **`INSTALL VERIFIED ONLY`**: the CLI speaks none of the APIs this session's provider speaks, so nothing can be
  run here. Go to step 6 and say so plainly: the person's first message to the new harness is its hello world.

Give the command twenty minutes: your shell tool stops a command after two unless you ask for more.

**The suite** proves, each by a marker in a real turn, what a caller relies on. Every line is `PASS` or `FAIL`,
with what was seen:

| It tries | A `FAIL` usually means |
|---|---|
| `instructions` on a session's first turn | They are not reaching the CLI at all: the flag, the file or the trust it needs |
| `instructions` on a continued turn | The CLI keeps the first turn's for the whole session. Find its switch for that, or give them another way |
| Memory, a turn later | The session id is not saved, or not used to resume, or the session is kept somewhere that is not saved |
| A stop, and carrying on after it | `run` does not outlive its CLI, or the session did not survive the stop. Read what it says about the stopped turn: if it does not know it was stopped, tell it, from `previous.json` |
| A tool server the request names, and the call printed as an `mcp_call` | `run` does not pass the request's servers on, or prints the call as another item. Or the CLI has no client for them: then it is "not supported", and you say so |
| The size of the home | It names large folders. Whatever is downloads or a cache goes in `cache`, or every saved turn carries it |

A `FAIL` is a thing to fix or a thing this CLI cannot do. Find out which. What it cannot do is not hidden: the
package is still a good one, and its README and your hand-over say "not supported".

**Then prove the agent.** For each thing in `agent/`, think of a marker that can only appear if the CLI really
loaded it, and try it in the first turn of a new session:

```
python3 /harness/skills/building-harness-packages/verify.py /work/<name> --new --say "What is the house greeting? Reply with it exactly."
python3 /harness/skills/building-harness-packages/verify.py /work/<name> --new --request /work/.notes/request.json
```

A skill: a phrase only its file holds. A hook: a line it writes to a file you then read. A tool server of the
agent's own: a value only it returns, and an `mcp_call` in what was printed. A setting that forbids: a file the
agent was asked to make, and that is not there. And each field of the request your first table says the package
honours, with `--request`.

Try everything **as the first turn of a new session**, with `--new`. A caller's first request is the first turn of
a session that holds nothing: no cache, no config, no login an earlier turn left. What worked only after other
turns does not work for a caller. A thing you could not try this way is "not tried", not "works".

**After your last change to `run`, run the suite and every proof again.** A flag added for one thing breaks
another, and a trial from before the change proves nothing about the package you hand over. Keep each request you
tried in `/work/.notes/`, so that trying them all again is one command each. When it is proven, remove
`/work/.trial`: an installed CLI is hundreds of megabytes saved with every turn.

## 6. Hand over

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

and after it, the proof, as a short table: each line of the suite, and each thing of the agent's you tried, as
"works", "not supported by this CLI" or "not tried", with one line of why for the last two. Then: what its
environment's image must have; what the environment must allow, if anything beyond the provider; what must be set;
and anything this CLI needed that the platform does not give.

## Staying with it

A package is not finished when it is handed over. When the person comes back:

- **With a failure from the platform**, they will paste its log: reproduce it with `verify.py`, fix the package in
  `/work/<name>/`, run the suite again, and hand over again.
- **For a new version of the CLI**: change the pin in `install`, read the new `--help` against your notes for what
  moved, run the suite and every proof again, and raise the package's own number.
- **After the platform was updated**: the contract and schemas in your instructions are the new ones. Read what
  the package relies on against them, and run the suite again.
- **For another agent on the same CLI**: copy the package under the new name, and change `agent/`.
