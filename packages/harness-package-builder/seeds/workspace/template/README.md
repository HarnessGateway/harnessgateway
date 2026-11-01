# shell: the template every harness starts from

The smallest complete harness package, and the one the platform's own tests run: it has no model, and the input text
runs as one bash command in the session's workspace. In the console, **New harness** copies this package under the
name you give; you then edit four files to make it any coding-agent CLI. The platform knows no CLI, so everything
CLI-specific is in these files and nowhere else. The contract is [`SPEC.md`](../../SPEC.md) §4–§5; the walkthrough is
[`docs/packages.md`](../../docs/packages.md).

## Files

| File | Role | What you change |
|---|---|---|
| `harness.json` | the manifest | `description`; `provider.apis` (the API shapes the CLI speaks: `anthropic`, `openai`, `google`; leave it out for a harness without a model); `defaults.model`; `install.egress` (the hosts `install.sh` downloads from) and `install.timeout_s`; `run.egress` (hosts the CLI needs at run time, usually none); `persist` (the CLI's session store under `$HOME`, so turns can resume); `settings` and `secrets` the organization fills in; the `mcp` catalog |
| `install.sh` | installs the CLI into `$HG_TOOLS`, once per package | the vendor's documented install, pinned to a version, into `$HG_TOOLS` (`npm install -g --prefix "$HG_TOOLS" pkg@1.2.3`, a `pip` venv, or the official installer pointed there). Never name a CPU; end by running the CLI's `--version` |
| `run.sh` | one turn | nothing: it runs `map.py` |
| `map.py` | the request → the CLI's command; the CLI's output → Responses events | the command (headless, non-interactive, auto-approving, JSON output, base URL `$HG_PROVIDER_URL`, key `$HG_PROVIDER_TOKEN`, model `$HG_MODEL`, resume from the id kept in `$HG_STATE`), and the mapping of each output line to `out.text`, `out.reasoning`, `out.shell`, `out.patch`, `out.mcp`, `out.tool`, then `out.completed(usage=…)` |
| `seeds/workspace/README.md` | laid into a new session's workspace | delete it, or seed a starter project |
| `README.md`, `tests/` | yours; the platform reads neither | the command, the request mapping, the output mapping, known limits |

Keep the package *vanilla*: the CLI as its vendor ships it plus what it needs to run headless in the sandbox. An
organization's standing instructions, skills, hooks, permission rules and MCP catalog belong in its own copy of the
package (`seeds/home`, the manifest's `mcp`, `settings`, `secrets`), not in a harness others start from.

## The command

```
bash -c "<input text>"          (working directory: the workspace)
```

## Request fields

| Request field | Where it goes |
|---|---|
| `input` (string, or the user messages' `input_text`) | the command |
| `previous_response_id` | the same workspace (the platform's session); bash keeps no other state |

Ignored, and named in `metadata.hg_ignored_fields`: `instructions`, `reasoning`, `text`, `tools`,
`max_output_tokens`, `max_tool_calls`.

The request arrives whole at `$HG_REQUEST` (and on stdin). hgkit's `Request` reads it: `req.prompt` is the user text,
`req.instructions` the `instructions` field with any system/developer messages, `req.place_inputs()` writes the
request's files into the workspace by name, `Request.mcp_servers()` lists the turn's MCP servers. What the CLI sees
(`HG_MODEL`, `HG_PROVIDER_URL`, `HG_PROVIDER_TOKEN`, `HG_STATE`, `HG_WORKSPACE`, each setting and secret as a variable)
is SPEC §5.6.

## Output mapping

| bash | Responses |
|---|---|
| the command, its stdout, stderr and exit code | `shell_call` + `shell_call_output` (outcome `exit`) |
| stdout (or `(exit N)` when empty) | an assistant `message` |
| the exit code | `metadata.exit_code` on `response.completed` |
| empty input | `response.failed`, code `invalid_prompt` |

A CLI's own stream maps the same way: its assistant text to `out.text` (streamed), its thinking to `out.reasoning`,
its shell runs to `out.shell`, its file edits to `out.patch`, its MCP calls to `out.mcp`, anything else to `out.tool`,
and its final usage to `out.completed(usage=Emitter.usage(input_tokens=…, output_tokens=…))`. Exactly one terminal event
ends the turn; on an error, `out.failed(code, message)`.

## Known limits

A non-zero exit is a completed turn (the exit code is in the output and metadata), not a failed one.

## Tests

`.venv/bin/pytest -q packages/shell` runs `map.py` against requests and validates its output with the platform's own
reader (`harnessgateway.domain.contract.check_output`). On the platform, install the package and send it a turn: the
session under Observability shows every event the mapping produced, the CLI's stderr tail, exit code and provider
calls.
