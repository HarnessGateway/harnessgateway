# Harness packages

A harness is an agent CLI made runnable on the platform by a package. The package is yours: the platform ships
none, knows no CLI, and runs whatever package you add the same way.

## A package is a folder

```
harness.json      what the package is
install           runs once for each environment image
run               runs once a turn
…                 anything else your install or run needs
```

Send it as a gzip-compressed tar archive: `tar -czf agent.tar.gz -C agent .`, then
`PUT /v1/harnesses/agent --data-binary @agent.tar.gz`. `examples/shell/` is the smallest one that works.

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
| `name`, `version` | The name is the harness's name; a request names it in `model` |
| `apis` | The provider APIs its CLI speaks: `anthropic`, `openai-responses`, `openai-chat`, `gemini`. Empty: it calls no model |
| `mind` | Where under `$HOME` its CLI keeps its session. A later harness on the same session is told where to look |
| `cache` | Folders under `$HOME` not worth saving between turns |
| `settings` | Values the organization may set. Each reaches `run` as a variable of that name |
| `secrets` | Values the organization sets that must not enter the sandbox. `run` sees a placeholder; the egress proxy puts the value into requests to `hosts` |

## install

Runs in the environment's image, as the sandbox user, with `/harness` (this package, writable) as its working
directory and the network a package manager needs. Install the CLI at a pinned version into `$HG_HARNESS`. It runs
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
| `$HG_MODEL`, `$HG_PROVIDER_URL`, `$HG_PROVIDER_API`, `$HG_PROVIDER_KEY` | The model to call, where, and a placeholder for its key |
| `$HG_STATE` | A folder under the mind for small values of the package's own |

**What it prints**, one JSON object a line, on standard output:

- **Responses stream events**, without `sequence_number` and never a `response.*` envelope event: those are the
  platform's. Each is checked against the OpenAI SDK's model of that event.
- **`{"type": "hg.log", "text": "…"}`** for the turn's log. So is everything on standard error.
- **One terminal line**, then exit 0:
  `{"type": "hg.done"}`, `{"type": "hg.incomplete", "reason": "max_output_tokens"}` or
  `{"type": "hg.failed", "code": "short_name", "message": "what went wrong"}`, each optionally with
  `"usage": {"input_tokens": n, "output_tokens": n}`.

When the turn is stopped, `run` is sent `SIGTERM` and has `run.stop_grace_s` seconds to end. What it left is saved.

## hgkit

`/harness/kit/hgkit.py` is the mapping, for a `run` written in Python (3.9 or later, standard library only). Using
it is optional.

```python
import os, sys
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
| `Cli` | `lines()`, `exit_code`; it forwards a stop to the CLI and everything the CLI started |
| `Emitter` | `say`, `think`, `ran`, `wrote`, `edited`, `deleted`, `called`, `used`, `log`, `done`, `incomplete`, `failed` |
