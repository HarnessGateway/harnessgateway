---
name: building-harness-packages
description: Build a HarnessGateway harness package for an agent CLI - research the CLI, write harness.json, install and run, check that it runs, and hand it over with a link that adds it. Use for any request to make, fix or change a harness package.
---

# Building a harness package

A harness package is a folder: `harness.json`, `install`, `run`. The platform knows no agent CLI; everything about
one is in its package. `run` reads the caller's OpenAI Responses request and prints Responses output.

The contract and the schemas are in your instructions, in full, and as files in `$HG_CONTRACT`. The package you are
running from is a finished one to read: `/harness/harness.json`, `/harness/install`, `/harness/run`.

Work through the steps in order. Keep `/work/.notes/<name>.md` as you go, so the next turn carries on from it.

## 1. Research the CLI

Its registry page and repository, its documentation, then **install it and run its `--help`**, and the help of its
subcommands. Help wins over documentation, which is often for another version. You can reach the web: use `curl`.
Find out:

- The latest **stable** version, and its makers' own way to install it on Linux.
- How one turn is run headless, and where the prompt goes.
- Its machine-readable output, and what each line of it means.
- How it is given a base URL, a key and a model, and which provider APIs it speaks: `anthropic`,
  `openai-responses`, `openai-chat`, `gemini`. A CLI that speaks only to its makers' service has `apis: []` and
  takes its makers' key as a `secret`.
- Where it keeps a session, and how one is continued.
- Where its system prompt goes for one run, and whether that still applies on a continued session.
- What it needs to act without asking, to load its configuration with no person there, and to turn off updates.

Then run it once by hand, headless, against this session's provider (`$HG_PROVIDER_URL`, `$HG_PROVIDER_KEY`,
`$HG_MODEL`; the key is a placeholder that works only toward the provider), and keep what it printed in the notes.

## 2. Write `harness.json`

In `/work/<name>/`, where `<name>` is the package's name. Only the fields the contract names. The version is the
CLI's, then `-1`. List in `apis` every API the CLI speaks, the one it does best first. `mind` is the folder under
`$HOME` where it keeps its session; `cache` is any folder under `$HOME` that holds downloads.

## 3. Write `install`

A shell script. It checks the image has what is needed, installs the CLI at the pinned version under
`$HG_HARNESS/tools` by its makers' method, and checks the version it got. It names no CPU architecture.

## 4. Write `run`

In a language the image already has (Python 3, standard library only, unless there is a reason). It has the shape
of `/harness/run`:

- Read `$HG_TURN/request.json`. Give the CLI what it can take of it: `input` as the prompt, `instructions` as its
  system prompt, and any other field of `request.schema.json` the CLI has a flag or a setting for.
- Start the CLI with the provider the platform chose (`$HG_PROVIDER_API`, `$HG_PROVIDER_URL`, `$HG_PROVIDER_KEY`,
  `$HG_MODEL`).
- Print each thing the CLI reports as a line of `lines.schema.json`, using the item that says what happened: a
  command is a `shell_call`, a file written is an `apply_patch_call`, a call to a tool server is an `mcp_call`.
  Only Responses output goes to standard output; everything else goes to standard error.
- Save the CLI's session id under `$HG_STATE` the moment it is known, and resume with it on the next turn.
- When `$HG_TURN/previous.json` says the turn before did not complete, tell the CLI so.
- Take `SIGTERM`, wait for the CLI to end, and print the stream's last event: `response.completed`, or
  `response.failed` with a message a person can act on.

Add a `README.md`: what it needs from an image, which APIs it speaks, which fields of the request it honours, and
what must be set.

## 5. Check that it runs

```
python3 /harness/skills/building-harness-packages/verify.py /work/<name>
```

It runs the package's `install`, then a hello-world turn, and holds every line to the schema the platform checks a
`run` by. It ends with `VERIFIED`, with `NOT VERIFIED` and why, or with `INSTALL VERIFIED ONLY` when this
session's provider speaks none of the CLI's APIs. Give it ten minutes: your shell tool stops a command after two
unless you ask for more. Fix what it names and run it again.

Then try a second turn, and anything else the README says the package does:

```
python3 /harness/skills/building-harness-packages/verify.py /work/<name> --say "What did I just ask you?"
python3 /harness/skills/building-harness-packages/verify.py /work/<name> --new --request /work/.notes/request.json
```

`--say` is one more turn in the same session; `--request` sends a Responses request of your writing; `--new`
starts a new session; `--stop-after 5` stops a turn the way a cancelled one is stopped; `--shape <type>` prints
the fields of any item or event. When it is verified, remove `/work/.trial`.

## 6. Hand over

End your reply with this, the package's own values filled in. Write the link exactly as it is here, with only the
name changed.

> **`<name>` `<version>` is ready.** It speaks `<apis>`; verified here on `<the API that was run>`.
>
> **[Add `<name>` as a harness](/console/#/harnesses/add/<name>)**
>
> Or over the API: `PUT /v1/harnesses/<name>` with `{"session": "<this response's metadata.hg_session>", "path": "<name>"}`.
>
> Then send it a first message: `<environment>/<name>/<provider>/<model>`.

and after it, in a few lines: which fields of the request it honours, what its environment's image must have, what
must be set, and what was not tried.
