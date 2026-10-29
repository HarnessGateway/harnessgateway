# shell

The reference harness, with no model: the input text runs as one bash command in the session's workspace. The
smallest package on the contract (SPEC §5) and the one the platform's own tests use.

- **CLI:** bash, from the sandbox image; `install.sh` installs nothing.
- **Provider:** none (`provider` absent): a turn gets no provider token.
- **Run egress:** none of its own; the organization's egress applies.

## Files

| File | Role |
|---|---|
| `harness.json` | manifest (contract 1) |
| `install.sh` | reports the image's bash version |
| `run.sh` | `exec python3 "$HG_PACKAGE_DIR/map.py"` |
| `map.py` | runs the input as a command, prints the events (hgkit `Emitter`) |
| `seeds/workspace/README.md` | the new session's starter workspace |
| `tests/test_map.py` | runs `map.py` against requests and validates its output with the platform's `check_output` |

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

## Output mapping

| bash | Responses |
|---|---|
| the command, its stdout, stderr and exit code | `shell_call` + `shell_call_output` (outcome `exit`) |
| stdout (or `(exit N)` when empty) | an assistant `message` |
| the exit code | `metadata.exit_code` on `response.completed` |
| empty input | `response.failed`, code `invalid_prompt` |

## Known limits

A non-zero exit is a completed turn (the exit code is in the output and metadata), not a failed one.

## Tests

`.venv/bin/pytest -q packages/shell`
