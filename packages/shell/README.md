# shell: the template every harness starts from

The smallest complete harness package, and the one the platform's own tests run: it has no model, and the input text
runs as one bash command in the session's workspace. In the console, **New harness** copies this package under the
name you give; you then edit three files to make it any coding-agent CLI. The platform knows no CLI and no language,
so everything CLI-specific is in these files and nowhere else. The contract is [`SPEC.md`](../../SPEC.md) §4–§5; the
walkthrough is [`docs/packages.md`](../../docs/packages.md).

## Files

| File | Role | What you change |
|---|---|---|
| `harness.json` | the manifest: what the agent is | `name`; `version` as `<CLI version>-<revision>` (the install reads the CLI version from it); `description`; `provider.apis` (the API shapes the CLI speaks: `anthropic`, `openai`, `google`; leave it out for a harness without a model); `defaults.model`; `install.egress` (the hosts `install` downloads from) and `install.timeout_s`; `run.egress` (hosts the CLI needs at run time, usually none); `persist` (the CLI's session store under `$HOME`, so turns can resume); `settings` and `secrets` the organization fills in; the `mcp` catalog |
| `install` | installs the CLI into `$HG_TOOLS`, once per package | the vendor's documented install, pinned to `${HG_PACKAGE_VERSION%-*}`, into `$HG_TOOLS` (`npm install -g --prefix "$HG_TOOLS" pkg@$VERSION`, a `pip` venv, or the official installer pointed there). Never name a CPU; end by running the CLI's `--version` |
| `run` | one turn: the request in, Responses events out | the whole turn, in any language (its first line names the interpreter). This one is Python with hgkit: the request becomes the CLI's command (headless, non-interactive, auto-approving, JSON output, base URL `req.provider.url`, key `req.provider.token`, model `req.model`, resume from the id kept in `req.state`), and each line of the CLI's output becomes `out.say`, `out.think`, `out.ran`, `out.wrote` / `out.edited`, `out.called`, `out.used`; then `out.done(input=…, output=…)` |
| `seeds/workspace/README.md` | laid into a new session's workspace | delete it, or seed a starter project |
| `README.md`, `tests/` | yours; the platform reads neither | the command, the request mapping, the output mapping, known limits |

`install` and `run` are executables: the platform runs them as they are, so what they are written in is yours to
choose. Keep the package *vanilla*: the CLI as its vendor ships it plus what it needs to run headless in the sandbox.
An organization's standing instructions, skills, hooks, permission rules and MCP catalog belong in its own copy of the
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
request's files into the workspace by name, `req.mcp_servers()` lists the turn's MCP servers, and `req.model`,
`req.provider`, `req.state` carry the turn's environment (SPEC §5.6: `HG_MODEL`, `HG_PROVIDER_URL`,
`HG_PROVIDER_TOKEN`, `HG_STATE`, `HG_WORKSPACE`, each setting and secret as a variable).

## Output mapping

| bash | Responses |
|---|---|
| the command, its stdout, stderr and exit code | `shell_call` + `shell_call_output` (outcome `exit`): `out.ran(...)` |
| stdout (or `(exit N)` when empty) | an assistant `message`: `out.say(...)` |
| the exit code | `metadata.exit_code` on `response.completed`: `out.done(metadata=...)` |
| empty input | `response.failed`, code `invalid_prompt`: `out.failed(...)` |

A CLI's own stream maps the same way: its assistant text to `out.say` (streamed), its thinking to `out.think`
(streamed), its shell runs to `out.ran`, its file writes and edits to `out.wrote` and `out.edited`, its MCP calls to
`out.called`, any other tool to `out.used`, and its final usage to `out.done(input=…, output=…, cached=…)`. Exactly
one terminal event ends the turn; on an error, `out.failed(code, message)`; when the agent stopped short,
`out.stopped(reason)`.

## Known limits

A non-zero exit is a completed turn (the exit code is in the output and metadata), not a failed one.

## Tests

`.venv/bin/pytest -q packages/shell` runs `run` against requests and validates its output with `hgkit.check`, the
reader the platform itself uses. Inside a sandbox, `python3 -m hgkit check output.jsonl` does the same for any
recorded output. On the platform, install the package and send it a turn: the session under Observability shows every
event the mapping produced, the CLI's stderr tail, exit code and provider calls.
