# The harness package contract, version 1

This is the complete contract between HarnessGateway and a harness package. The platform enforces every rule here.

## Files

| Path | Required | Purpose |
|---|---|---|
| `harness.json` | yes | The manifest (below). Unknown keys are refused. |
| `install.sh` | yes | Installs the CLI into `$HG_TOOLS`. Runs once per tools key (hash of `install.sh` + the `install` section), in a sandbox with egress to `install.egress` only. Must be self-contained. |
| `request.sh` | yes | Runs one turn. Must parse as bash. |
| `seeds/workspace/` | no | Copied into `/workspace` when a session starts. |
| `seeds/home/` | no | Copied into `$HOME` when a session starts. |
| anything else | no | Yours (mapper, README, tests, configs). The platform reads nothing else. |

Limits: 5,000 files, 256 MiB unpacked, 64 MiB uploaded archive; regular files and directories only (no symlinks).
A package version is identified by its **digest** (SHA-256 over paths, executable bits and contents). A session runs the
digest it started on; installing a new version affects new sessions only.

## Manifest (`harness.json`)

```json
{
  "contract": 1,
  "name": "my-harness",
  "version": "1.2.3-1",
  "description": "One paragraph: what this harness is and how it maps the Responses API.",
  "provider": {"apis": ["anthropic", "openai"]},
  "defaults": {"model": "claude-sonnet-4-5"},
  "install": {"egress": ["registry.npmjs.org"], "timeout_s": 900},
  "run": {"egress": [], "timeout_s": 3600, "stop_grace_s": 20},
  "settings": {
    "GITHUB_TOKEN": {"secret": true, "required": false, "description": "Lets the agent push branches."}
  }
}
```

| Field | Rule |
|---|---|
| `contract` | `1` |
| `name` | lowercase letters, digits, `.`, `_`, `-`; starts with a letter or digit; at most 63. Clients call it as `model="<name>[/<provider>[/<model>]]"`. |
| `version` | 1–64 characters, yours. |
| `provider` | Omit for a harness with no model (it gets no provider token). `apis`: the provider wire formats the CLI can drive: `anthropic` (Messages API) and/or `openai` (Chat Completions, Responses, Embeddings). A request is refused if the chosen provider's shape is not listed. |
| `defaults.model` | The model when the request names none and the organization set no default. |
| `install.egress` | Hosts (`host` or `host:port`, port 443 by default) `install.sh` may reach. |
| `install.timeout_s` | 1–3600. |
| `run.egress` | Hosts a turn may reach besides the provider (usually none). |
| `run.timeout_s` | 1–86400; the organization's limit caps it. On timeout the turn ends `incomplete` (`hg_reason: timeout`). |
| `run.stop_grace_s` | 1–300: seconds between SIGTERM and SIGKILL when a turn is cancelled or times out. |
| `settings` | Up to 64 names (`A-Z0-9_`) the organization configures with `PATCH /v1/harnesses/<name>`. `secret` values are encrypted at rest. They reach the turn in `$HG_SETTINGS`. |

## The turn environment

| Variable | Value |
|---|---|
| `HG_CONTRACT` | `1` |
| `HG_REQUEST` | `/run/hg/request.json`: the client's Responses request, verbatim (also on stdin) |
| `HG_SETTINGS` | `/run/hg/settings.json`: `{"NAME": "value"}` for the organization's settings |
| `HG_FILES` | `/run/hg/files`: `index.json` maps each `file_id` the input referenced to `{filename, path, bytes}` |
| `HG_WORKSPACE` | `/workspace` (the cwd), persisted across the session's turns |
| `HOME` | `/home/hg`, persisted |
| `HG_STATE` | `/state`, persisted: the place for the CLI's own session/thread id |
| `HG_PACKAGE_DIR` | `/opt/hg/package` (read-only) |
| `HG_TOOLS` | `/opt/hg/tools` (read-only at run time); `PATH` includes `$HG_TOOLS/bin` and `$HG_TOOLS/node_modules/.bin` |
| `HG_SESSION` | the session (container) id |
| `HG_RESPONSE` | this response's id |
| `HG_TURN` | 1 for a session's first turn, then 2, 3, ... |
| `HG_MODEL` | the resolved model name (may be empty for a harness with no provider) |
| `HG_PROVIDER_API` | `anthropic` or `openai`: the shape of the organization's provider for this session |
| `HG_PROVIDER_URL` | `http://hg-egress:8080`: the provider base URL, without `/v1` |
| `HG_PROVIDER_TOKEN` | the API key to present (a per-turn token the platform swaps for the real key) |
| `HTTPS_PROXY` | set: the only way to reach `run.egress` hosts |

Provider paths the gateway relays: `anthropic`: `/v1/messages`, `/v1/messages/count_tokens`, `/v1/models`;
`openai`: `/v1/chat/completions`, `/v1/completions`, `/v1/responses...`, `/v1/embeddings`, `/v1/models`.

The sandbox image provides bash, coreutils, git, curl, jq, ripgrep, python3 (3.11, venv, pip), node 22 (npm) and the
hgkit library. The root filesystem is read-only; write only to the workspace, `$HOME`, `$HG_STATE` and `/tmp`.

## Output

`request.sh`'s stdout is read line by line:

1. A JSON object whose `type` starts with `response.` is a **Responses stream event**. It must validate against the
   OpenAI SDK model for that type, with `sequence_number` left out (the platform assigns it). Envelope events
   (`response.created`, `response.in_progress`, `response.queued`) are dropped: the platform sends its own.
2. Exactly one **terminal event** must come last: `response.completed`, `response.incomplete` or `response.failed`,
   carrying a partial `response` object with any of `output`, `usage`, `error`, `incomplete_details`, `metadata`.
   If `output` is absent, the final output is the ordered `response.output_item.done` items.
3. Anything else is a log line: kept for the turn's trace, never sent to the client.

Limits: 1 MiB per line, 64 MiB per turn. stderr is kept (its tail always; in full with observability).

The final `Response` the client reads has the platform's `id`, `created_at`, `status`, `model` (the request's), the
request's parameters echoed, and `metadata` = the request's metadata + the package's + `session_id`. Platform-decided
endings add `metadata.hg_reason`: `cancelled`, `client_disconnected`, `timeout`, `no_terminal_event`, `invalid_output`,
`output_too_large`, `install_failed`, `sandbox_failed`, `node_lost`, `workspace_too_large`. An `error.code` outside the
OpenAI set is reported as `server_error` with the package's code in `metadata.hg_error_code`.

## Lifecycle

- **Session start:** a sandbox is created (or restored from the session's last snapshot), seeds are copied, tools are
  mounted.
- **Each turn:** request, settings and files are written to `/run/hg`; files the client uploaded to the container are
  placed in the workspace; `request.sh` runs with stdin = the request.
- **After a turn:** the workspace, `$HOME` and `$HG_STATE` are snapshotted (unless the request had `store: false`). The
  sandbox stays warm until the organization's idle time passes.
- **Cancel/timeout:** SIGTERM to `request.sh`'s process group; after `run.stop_grace_s`, SIGKILL to every process in the sandbox, whatever group it is in. Trap SIGTERM to stop your harness cleanly (and pass it on to children your harness moved into groups of their own); a cancelled turn keeps the output items it had finished, and you need not print a terminal event.
