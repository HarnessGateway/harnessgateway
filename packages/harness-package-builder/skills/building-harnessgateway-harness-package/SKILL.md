---
name: building-harnessgateway-harness-package
description: Build a HarnessGateway harness package for an agent CLI - research the CLI, write harness.json, install and run, verify it with a hello world, and hand it over with a link that adds it. Use for any request to make, fix or change a harness package.
---

# Building a HarnessGateway harness package

A harness package is a folder: `harness.json`, `install`, `run`. The platform knows no agent CLI; everything about
one is in its package. The platform puts two things beside every installed package, and so beside you:
`/harness/kit/PACKAGES.md`, the whole contract (read it once before step 2), and `/harness/kit/hgkit.py`, the kit
a `run` may use. The package you are running from is a complete, working one: `/harness/harness.json`,
`/harness/install`, `/harness/run`.

Work through the four steps in order. Keep `/work/.notes/<name>.md` as you go: it is how you carry on in the next
turn without finding everything out again.

## 1. Research the CLI

From the CLI itself, in this order: its registry page and repository, its documentation, then **install it and run
its `--help`** (and the help of its subcommands), which wins over anything you remember or read. You can reach
the web: use `curl`. Write each answer in the notes, with where you learned it.

| Find out | It decides |
|---|---|
| The latest **stable** version, and its makers' own way to install it on Linux | `install`: pinned, under `$HG_HARNESS/tools`, the version checked |
| Its headless mode, and its machine-readable output (JSON lines, a stream) | `run`: the command, and what each line becomes |
| How it is given a base URL, a key and a model, and **which provider APIs it speaks** | `apis` in `harness.json`; the variables `run` sets |
| Where under `$HOME` it keeps a session, and how one is continued by its id | `mind` in `harness.json`; resuming in `run` |
| How it is told to act without asking, and how updates and telemetry are turned off | `run`'s flags and variables |
| Where it reports the tokens a turn used | `usage` on the terminal line |
| Where it reads instructions (an `AGENTS.md` or its own file), skills, MCP servers and settings | What the package gives it: see "What the CLI is given" |

Then run it once by hand, headless, against this session's provider (`$HG_PROVIDER_URL`, `$HG_PROVIDER_KEY`,
`$HG_MODEL`; the key is a placeholder that works only toward the provider), and keep what it printed in the notes:
the shape of a line of text, of reasoning, of a command, of a file written, of an error, and of the end.

**API shapes.** List every API the CLI speaks in `apis`, the one it does best first: `anthropic`,
`openai-responses`, `openai-chat`, `gemini`. The platform picks the first the request's provider also speaks and
tells `run` which in `$HG_PROVIDER_API`; `run` sets the CLI up for that one. You can run the CLI here only on the
API this session's provider speaks (`$HG_PROVIDER_API`). Write the others from the CLI's documentation, and say at
the hand-over which were run and which were not.

## 2. Write the package

In `/work/<name>/`, where `<name>` is the package's name (lower-case letters, digits and `-`):

- **`harness.json`**: only the fields the contract names. The version is the CLI's, then `-1`.
- **`install`**: a shell script. It checks the image has what is needed, installs the CLI at the pinned version
  under `$HG_HARNESS/tools` by its makers' method, and checks the version it got. It names no CPU architecture.
- **`run`**: Python, with the kit. It has the shape of `/harness/run`: the CLI's command; its environment; each
  line it prints turned into a call of the kit's `Emitter`; the CLI's session id saved with `State` the moment it
  is known, and used to resume; one terminal line; and a new session when the old one cannot be resumed. A CLI or
  provider error is `emit.failed` with a message a person can act on, never a silent exit.
- **`README.md`**: what it is; what it needs from an image; which APIs it speaks, and which of them were run; what
  an environment must allow beyond the provider; what must be set; what could not be tried.

Print through the kit and nothing else on standard output: every line the kit prints is one the platform takes.
`run` and the CLI write nothing under `/harness` during a turn: it is read-only there.

### What the CLI is given

A package may give its CLI instructions, skills, MCP servers and settings of its own. `/harness` is read-only
during a turn and the mind belongs to the session, so there are three ways, best first:

1. **Kept in the package and pointed at**, when the CLI has an option or a variable for it (a config file, a
   plugin or skills folder, a system-prompt file). Nothing is copied, so nothing drifts.
2. **Copied into the mind by `run`** at the start of a turn, when the CLI reads only under `$HOME`. Copy what is
   missing or different; never overwrite what the session wrote.
3. **Written each turn** from the request and the manifest: the model, the provider, the request's MCP servers
   (`request.mcp_servers`).

What an organization may set is a `setting` in `harness.json` (it reaches `run` as a variable); a value that must
not enter the sandbox is a `secret`, with the hosts it may go to. No key, token or placeholder is in any file.

## 3. Verify it

```
python3 /harness/skills/building-harnessgateway-harness-package/verify.py /work/<name>
```

It lays the package out as the platform does, with the kit; runs its `install`; then runs a hello-world turn in a
new session and holds every line to what the platform takes. It ends with one of:

- **`VERIFIED`**: go to step 4.
- **`NOT VERIFIED`**, and why: fix it, and verify again. Read what the turn printed; do not guess.
- **`INSTALL VERIFIED ONLY`**: the CLI speaks none of the APIs this session's provider speaks, so its hello world
  cannot run here. Go to step 4 and say so plainly: the person's first message to the new harness is its hello
  world.

Give the command ten minutes: your shell tool stops a command after two unless you ask for more. Then, with
`--say`, one more turn in the same session, to see that it remembers the first:

```
python3 /harness/skills/building-harnessgateway-harness-package/verify.py /work/<name> --say "What did I just ask you?"
```

`--say` takes any prompt (have it write and run a file); `--stop-after 5` stops the turn the way a cancelled one
is stopped. When it is verified, remove `/work/.trial`: an installed CLI is hundreds of megabytes saved with every
turn.

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

and after it, in a few lines: what its environment's image must have; what the environment must allow, if
anything beyond the provider; what must be set; and what was not tried. If the person comes back with a failure
from the platform, they will paste its log: fix the package in `/work/<name>/`, verify again, and hand over again.
