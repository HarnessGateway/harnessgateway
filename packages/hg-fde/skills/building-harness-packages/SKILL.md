---
name: building-harness-packages
description: Build a HarnessGateway harness package for an agent CLI - research the CLI, write install and run, and hand it over with a link that adds it. Use for any request to make, fix or change a harness package.
---

# Building a harness package

A harness package is a folder with two programs: `install` and `run`. The platform knows no agent CLI;
everything about one is in its package. `run` reads the caller's OpenAI Responses request and prints Responses
output.

The contract and the schemas are in your instructions, in full, and as files in `$HG_CONTRACT`. The package you are
running from is a finished one to read: `/harness/install` and `/harness/run`.

Work through the steps in order. Keep `/work/.notes/<name>.md` as you go, so the next turn carries on from it.

## 1. Research the CLI

Its registry page and repository, its documentation, then **install it and run its `--help`**, and the help of its
subcommands. Help wins over documentation, which is often for another version. You can reach the web: use `curl`.
Find out:

- The latest **stable** version, and its makers' own way to install it on Linux.
- How one turn is run headless, and where the prompt goes.
- Its machine-readable output, and what each line of it means.
- How it is given a base URL, a key and a model, and which provider APIs it speaks: `anthropic`,
  `openai-responses`, `openai-chat`, `gemini`. A CLI that speaks only to its makers' service is named
  `environment/<name>/none/none`, and takes its makers' key from a secret of its environment.
- Where it keeps its record of a session, in what form, and how a session is continued. If that place is not
  under `$HOME`, how it is moved there: only `/mind` (which is `$HOME`) and `/work` are kept between turns.
- How it is given more tools, and how one run of it is ended from inside a tool call: that is how a tool the
  caller runs is handed back.
- Where its system prompt goes for one run, and whether that still applies on a continued session.
- What it needs to act without asking, to load its configuration with no person there, and to turn off updates.

Then run it once by hand, headless, against this session's provider (`$HG_PROVIDER_ANTHROPIC` is its base URL,
`$HG_PROVIDER_KEY` its key and `$HG_MODEL` the model; the key is a placeholder that works only toward the
provider), and keep what it printed in the notes.

## 2. Write `install`

In `/work/<name>/`, where `<name>` is the package's name. A shell script. It checks the image has what is
needed, installs the CLI at the pinned version under `$HG_HARNESS/tools` by its makers' method, and checks the
version it got. It names no CPU architecture.

## 3. Write `run`

In a language the image already has (Python 3, standard library only, unless there is a reason). It has the shape
of `/harness/run`:

- Read `$HG_TURN/request.json`. Give the CLI what it can take of it: all of `input` as the prompt (a list may hold
  a whole conversation), `instructions` as its system prompt for this turn only, and any other field of
  `request.schema.json` the CLI has a flag or a setting for.
- **Honour or fail.** A field the package does not honour, and that is not the platform's or a label (the contract
  lists both), ends the turn with `response.failed`, code `unsupported_parameter`, and a message naming the field.
  So does a value the CLI cannot take. Find this before the CLI is started. Nothing is left alone unsaid.
- Start the CLI on the request's provider. For each API the provider speaks there is a variable with its base
  URL: `$HG_PROVIDER_ANTHROPIC`, `$HG_PROVIDER_OPENAI_RESPONSES`, `$HG_PROVIDER_OPENAI_CHAT`,
  `$HG_PROVIDER_GEMINI`. Use the first one the CLI speaks, with `$HG_PROVIDER_KEY` and `$HG_MODEL`; when the
  provider speaks none of them, end with `response.failed` and say which APIs the CLI needs.
- Print each thing the CLI reports as a line of `lines.schema.json`, using the item that says what happened: a
  command is a `shell_call`, a file written is an `apply_patch_call`, a call to a tool server is an `mcp_call`.
  Print a call when the CLI makes it, and what came of it when it returns.
  Only Responses output goes to standard output; everything else goes to standard error.
- Save the CLI's session id under `$HG_STATE` the moment it is known, and resume with it on the next turn.
- Write `$HG_STATE/memory`: a few lines saying where the CLI keeps its record of this session, as paths under
  `/mind`, and what form it is in. The harness that takes the session over is pointed at it.
- When `$HG_TURN/handover.txt` is not empty, give its text to the CLI before the person's words: another harness
  worked on the session, and the text says where its memory is.
- When `$HG_TURN/previous.json` says the turn before did not complete, tell the CLI so.
- Take `SIGTERM`, wait for the CLI to end, and print the stream's last event: `response.completed`, or
  `response.failed` with a message a person can act on.

A package declares nothing: what it may reach and hold is its environment's to say. So add a `README.md` that
says what its environment must give it: what the image must have, which provider APIs it speaks, which variables
and secrets it reads and the hosts each secret goes to, and which fields of the request it honours.

## 4. Hand over

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
