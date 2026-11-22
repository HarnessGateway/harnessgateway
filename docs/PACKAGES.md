# Harness packages

A harness is an agent CLI made runnable on the platform by a package. The platform knows no CLI: everything about
one is in its package, and every package is run the same way. An organization starts with two, `shell` and
`harness-package-builder`, and owns every package it has.

This is the whole contract. The platform puts it beside the kit in every install, at `/harness/kit/PACKAGES.md`:
it is what `harness-package-builder` reads before it writes a package.

## A package is a folder

```
harness.json      what the package is
install           runs once for each environment image
run               runs once a turn
…                 anything else your install or run needs
```

It travels as a gzip-compressed tar archive of that folder: `tar -czf agent.tar.gz -C agent .`, then
`PUT /v1/harnesses/agent --data-binary @agent.tar.gz`. `install` and `run` must be executable. A folder named
`kit` is the platform's: a package may not hold one. The `shell` package is the smallest that works.

## harness.json

```json
{
  "contract": 1,
  "name": "codex",
  "version": "0.171.0-2",
  "description": "OpenAI's Codex CLI.",
  "apis": ["openai-responses"],
  "mind": ".codex",
  "cache": [".codex/tmp"],
  "install": {"timeout_s": 900},
  "run": {"stop_grace_s": 15},
  "settings": {"SANDBOX_MODE": {"description": "…", "default": "workspace-write"}},
  "secrets": {"TOOL_TOKEN": {"description": "…", "hosts": ["tool.example.com"], "required": true}}
}
```

| Field | Meaning |
|---|---|
| `contract` | The sandbox contract the package is written against: `1` |
| `name`, `version` | The name is the harness's name (lower-case letters, digits and `-`); a request names it in `model`. The version is the CLI's, with a number of the package's own after it |
| `description` | What a person choosing a harness reads |
| `apis` | The provider APIs its CLI speaks, the preferred first: `anthropic`, `openai-responses`, `openai-chat`, `gemini`. The platform picks the first the request's provider speaks too, and tells `run` which (`$HG_PROVIDER_API`). Empty: it calls no model |
| `mind` | Where under `$HOME` its CLI keeps its session. A later harness on the same session is told where to look |
| `cache` | Folders under `$HOME` not worth saving between turns |
| `install.timeout_s`, `run.stop_grace_s` | How long `install` may take (900), and how long `run` is given to end once it is told to (10) |
| `settings` | Values the organization may set. Each reaches `run` as a variable of that name; `default` and `required` are optional |
| `secrets` | Values the organization sets that must not enter the sandbox. `run` sees a placeholder in a variable of that name; the egress proxy puts the value into requests to `hosts`, and nowhere else |

No other field exists. A setting's or a secret's name is upper-case letters, digits and `_`, and never starts with
`HG_`.

## The sandbox

Both programs run in a container of the environment's image, as a user that is not root, with a read-only root
file system. What can be written: `/mind` (which is `$HOME`) and `/work` during a turn, `/harness` during an
install, and `/tmp`, which is in memory and gone when the sandbox is. Nothing else survives a turn: not a running
process, not the sandbox.

The only way out is the egress proxy. The usual variables (`HTTPS_PROXY`, `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`
and the rest) are set, so most programs use it unasked. A turn reaches its provider and what its environment
allows; an install reaches the package managers (npm, PyPI, GitHub and the like) and what its environment allows.
A refused host answers `403` with a line saying why. A package adds no host: what its CLI needs beyond its provider
goes in its README, for whoever sets up the environment.

No secret enters the sandbox. The provider's key and every declared secret are placeholders; the proxy puts the
real value in on the way out, toward the hosts it is bound to.

## install

Runs in the environment's image, with `/harness` (this package, writable) as its working directory and `HOME` in
`/tmp`. It is given no provider, no setting and no secret. Install the CLI at a pinned version under
`$HG_HARNESS/tools`, by its makers' own method, and check the version it got. Name no CPU architecture. It runs
once for each image; after that `/harness` is read-only.

## run

Runs once a turn, in `/work`.

**What it finds**

| | |
|---|---|
| `/turn/request.json` | The Responses request, as it was sent |
| `/turn/history.json` | Earlier turns of the session this harness has not seen (another harness ran them) |
| `/turn/handover.txt` | One line naming those harnesses and where they kept their notes, or nothing |
| `/turn/files/<id>/<name>` | Each uploaded file the request names |
| `/mind` (`$HOME`), `/work` | The session's two folders: they persist from turn to turn |
| `$HG_HARNESS` | `/harness`: the installed package, with the kit in `kit/` |
| `$HG_MODEL`, `$HG_PROVIDER_URL`, `$HG_PROVIDER_API`, `$HG_PROVIDER_KEY` | The model to call, where, which API to speak, and a placeholder for its key |
| `$HG_STATE` | A folder under the mind for small values of the package's own |
| `$HG_SESSION`, `$HG_RESPONSE` | The ids of the session and of this turn's response |

**What it prints**, one JSON object a line, on standard output:

- **Responses stream events**, without `sequence_number` and never a `response.*` envelope event
  (`response.created`, `response.completed` and the like): those are the platform's. Each is checked against the
  OpenAI SDK's model of that event, and the first one that is wrong fails the turn.
- **`{"type": "hg.log", "text": "…"}`** for the turn's log. So is everything on standard error.
- **One terminal line**, last, then exit 0:
  `{"type": "hg.done"}`, `{"type": "hg.incomplete", "reason": "max_output_tokens"}` or
  `{"type": "hg.failed", "code": "short_name", "message": "what went wrong"}`, each optionally with
  `"usage": {"input_tokens": n, "output_tokens": n}`.

A session continues when `run` gives its CLI the CLI's own session id from the turn before: save it in the state
the moment the CLI reports it, and resume with it. When the turn is stopped, `run` is sent `SIGTERM` and has
`run.stop_grace_s` seconds to end; it prints no terminal line then. What it left is saved, and the next turn carries
on from it.

## hgkit

`/harness/kit/hgkit.py` is the mapping, for a `run` written in Python (3.9 or later, standard library only). Using
it is optional, and with it every line printed is one the platform takes.

```python
#!/usr/bin/env python3
import json, os, sys
sys.path.insert(0, os.path.join(os.environ["HG_HARNESS"], "kit"))
import hgkit

request, emit, state = hgkit.Request(), hgkit.Emitter(), hgkit.State()
cli = hgkit.Cli(["my-agent", "--json", "--resume", state.get("session") or "new"], stdin=request.brief())
for line in cli.lines():                    # the CLI's output, a line at a time
    event = json.loads(line)
    if event["type"] == "session":   state.set("session", event["id"])
    elif event["type"] == "text":    emit.say(event["text"])
    elif event["type"] == "command": emit.ran(event["command"], event["stdout"], event["stderr"], event["exit_code"])
emit.done() if cli.exit_code == 0 else emit.failed("agent_failed", f"my-agent exited with code {cli.exit_code}")
```

| | |
|---|---|
| `Request` | `body`, `prompt`, `instructions`, `effort`, `mcp_servers`, `files`, `history`, `handover`, `model`, `provider_url`, `provider_api`, `provider_key`, `brief()` |
| `State` | `get(name)`, `set(name, value)`, `clear(name)`: kept with the session |
| `Cli(command, env=, cwd=, stdin=)` | `lines()`, `exit_code`; it forwards a stop to the CLI and everything the CLI started, and ends `run` there |
| `Emitter` | `say(text)`, `think(text)`, `ran(command, stdout, stderr, exit_code)`, `wrote(path, diff)`, `edited(path, diff)`, `deleted(path)`, `called(server, tool, arguments, output, error)`, `used(name, given, output)`, `log(text)`, `done(usage)`, `incomplete(reason, usage)`, `failed(code, message, usage)` |

The kit's own file says the rest: read it.
