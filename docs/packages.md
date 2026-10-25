# Write a package

A harness package is what turns a Responses request into a CLI run and the CLI's output into a
Responses response. It is a directory, kept as a git repository on the platform, holding five
things. This page is the contract (version 8; a contract-7 package still runs); [the Responses wire](responses-wire.md) is the
research behind the catalog's packages, and the [harness-package skill](https://github.com/HarnessGateway/harnessgateway/blob/main/.agents/skills/harness-package/SKILL.md)
is the procedure for writing one with a coding agent.

## The files

| File | Required | Runs | Purpose |
|---|---|---|---|
| `harness.json` | yes | | what the platform needs to launch the package, nothing about the CLI's behaviour |
| `install.sh` | yes | once per commit per node, unprivileged, egress to `install.egress` | puts the CLI under `$HG_TOOLS` |
| `request.sh` | yes | every turn, in the sandbox, egress to `run.egress` plus the provider | reads the request, runs the CLI, prints stream events |
| `response.sh` | no | after `request.sh`, however it ended | shapes the final response from what `request.sh` left |
| `seeds/workspace/` | no | copied into the workspace when a session starts, never overwriting | `AGENTS.md`, an MCP config, rules, skills: what the CLI reads from the project |
| `seeds/home/` | no | copied into `$HOME` every turn, overwriting | the CLI's settings, its trust file, its config: what it reads from the user |

Anything else in the directory is the package's own: a mapper in Python, a README, tests. The
platform reads `harness.json`, copies the seeds and runs the three scripts, and nothing more. A
package is at most 10,000 files and 256 MB.

## harness.json

```json
{
  "contract": 8,
  "description": "Codex in exec mode on any Responses provider.",
  "cli": {"name": "codex", "version": "0.163.0"},
  "defaults": {"provider": "openai", "model": "gpt-5.4-mini"},
  "provider_env": {"key": "OPENAI_API_KEY", "url": "OPENAI_BASE_URL"},
  "install": {"egress": ["registry.npmjs.org"], "timeout_s": 900},
  "run": {"egress": [], "timeout_s": 3600, "stop_timeout_s": 20},
  "accepts": ["input", "instructions", "previous_response_id", "reasoning.effort", "tools", "metadata"]
}
```

| Key | Meaning |
|---|---|
| `contract` | 8 (a package that says 7 still loads; its `interrupt`, the steer signal 8 dropped, is ignored) |
| `description` | shown in lists |
| `cli` | the CLI's name and pinned version, shown in lists |
| `defaults` | the provider and model when the request's `model` is a bare harness name; either may be empty (then the request must name one) |
| `provider_env` | the names of the environment variables the CLI reads its key and base URL from; the platform sets them to the session token and the gateway's address, alongside `HG_PROVIDER_KEY_FILE` and `HG_PROVIDER_URL` |
| `install.egress`, `run.egress` | the hosts the door lets `install.sh` and `request.sh` reach; the provider's host is implied for the run. Everything else is refused, private addresses always |
| `install.timeout_s`, `run.timeout_s`, `run.stop_timeout_s` | ceilings, within the organization's limits |
| `accepts` | the request fields the package honours, from `input`, `instructions`, `previous_response_id`, `reasoning.effort`, `reasoning.summary`, `text.format`, `text.verbosity`, `tools`, `max_tool_calls`, `max_output_tokens`, `store`, `truncation`, `service_tier`, `metadata`, `temperature`, `top_p`; documentation for the client, not enforcement |

## The turn's environment

`request.sh` runs in `$HG_WORKSPACE` with:

| Variable | Holds |
|---|---|
| `HG_REQUEST` | the client's request, verbatim JSON (also on stdin) |
| `HG_WORKSPACE` | the workspace, the cwd, kept between turns |
| `HOME` | the member's home for this package, seeded from `seeds/home`, kept between turns |
| `HG_STATE` | a directory kept with the session: put the CLI's resume handle here |
| `HG_FILES` | the request's `input_file` and `input_image` parts, decoded, by filename |
| `HG_DIR` | scratch for this turn |
| `HG_SESSION`, `HG_RESPONSE`, `HG_TURN`, `HG_PREVIOUS` | the session id, this response's id, the turn number (1, 2, …), the `previous_response_id` or empty |
| `HG_MODEL` | the model segment of the request's path, or the package default, or empty |
| `HG_PROVIDER_URL`, `HG_PROVIDER_KEY_FILE`, `HG_PROVIDER_API` | the gateway's address, the file holding the session token, the provider's API shape |
| the `provider_env` names | the same address and token |
| `HG_HARNESS_DIR` | the package's files, read-only |
| `HG_TOOLS` | what `install.sh` installed; `$HG_TOOLS/bin` is on `PATH` |
| `HG_CONTRACT` | 8 |

The sandbox is the platform's base image (Debian, Python 3.12, Node 22, git, curl, jq, ripgrep)
as the organization's own uid with no capabilities and a read-only root; the writable places are
the workspace, the home, `$HG_DIR` and `/tmp`. See [Security](security.md).

## What request.sh prints

Stdout is relayed to the client live and kept. A line that is a JSON object whose `type` starts
with `response.` is a Responses stream event: the platform adds `sequence_number`, relays it as
it is, and keeps it. Every other line is the turn's log, readable at `GET /v1/responses/{id}/detail`.
Stderr is kept too.

The response is assembled from the events: each `response.output_item.done` event's `item` is an
output item, in order; a `response.completed` event's `response.usage` is the usage. Items are
validated against the SDK's types (`ResponseOutputItem`), usage against `ResponseUsage`; a
malformed one fails the turn with `metadata.error_code = package_output_invalid` and the offending
event in the log. The exit code of `request.sh` is the turn's `exit_code` in `metadata`; a
non-zero exit makes the turn `failed` with `metadata.error_code = exit_status` unless `response.sh`
sets a status of its own, so a CLI whose exit code means something else gets a `response.sh` that
says so.

Plain text alone (no events at all) becomes one `message` item with `metadata.raw_stdout = true`,
so a package that is only a CLI invocation still answers.

The usual shape of `request.sh` is: parse the request with `jq`, write the instructions where the
CLI reads them, run the CLI in its JSON mode, pipe its output through a mapper (`map.py` in the
catalog's packages) that prints the events. The [template](catalog/template.md) is that skeleton
with a stand-in CLI.

## response.sh

Runs after `request.sh`, however it ended, with `HG_STDOUT` and `HG_STDERR` (the kept files),
`HG_EXIT` (its exit code) and `HG_OUTCOME` (`exited` or `cancelled`). What it prints is one JSON
object; the platform takes from it:

| Key | Becomes |
|---|---|
| `output` | the response's items (validated), replacing the events' |
| `usage` | the usage |
| `status` | `completed`, `failed` or `incomplete`; default from the exit code |
| `error` | `{"code", "message"}`; a code outside `server_error`, `rate_limit_exceeded`, `invalid_prompt` is kept as `metadata.error_code` and the code becomes `server_error` |
| `incomplete_details` | `{"reason"}`; a reason outside `max_output_tokens`, `max_messages`, `content_filter` moves to `metadata.incomplete_reason` |
| `metadata` | string values added to the response's metadata |

Its exit code is the status when `status` is absent. Use it when the CLI reports usage or a result
only at the end (a usage file, a JSON envelope), or to turn the CLI's own failure into `failed`.

## Stopping

A stop is `SIGTERM` to `request.sh`'s whole process group, then `stop_timeout_s` of grace, then
`response.sh` with `HG_OUTCOME=cancelled`. It happens on a cancel, when the client that asked for
the turn is gone, and when a new request on the session supersedes the turn; the next turn then
runs on the workspace and state this one left, so a package that keeps its CLI's resume handle in
`$HG_STATE` as it runs loses nothing. There is no mid-run message: the next message is the next
turn.

## Precedence

When a CLI offers several ways to carry a request field, the catalog's packages use, in order:
the request schema first (a field in the request must do something), then files in the
environment (what most CLIs read: `AGENTS.md`, an MCP config, a settings file), then CLI flags.
Instructions go to the file the most harnesses read, as a section of `AGENTS.md` replaced each
turn, unless the CLI has an append-system-prompt flag, which is more faithful to the field. The
reasoning and the table are in [the Responses wire](responses-wire.md#precedence-which-mechanism-a-package-uses-when-there-are-several).

## Develop one

```bash
hg harness pull my-harness ./pkg                        # or start from catalog/template in the repository
hg harness run ./pkg --input "Create hello.txt" --state ./state      # runs request.sh and response.sh here
hg harness run ./pkg --request req.json --state ./state --install    # a full request; run install.sh first
hg harness run ./pkg --request req2.json --state ./state   # req2 carries previous_response_id: a continuation
hg harness push my-harness ./pkg -m "map tool results"
```

`hg harness run` runs the package on your machine, outside any sandbox, with your environment
and your keys: set `HG_PROVIDER_URL`, `HG_PROVIDER_KEY` and `HG_PROVIDER_API` and the
`provider_env` names are filled from them. `--state` keeps the workspace, the home, the tools and the
CLI's state between runs; each run is the next turn, and a request file with `previous_response_id`
(the previous run's id, `resp_0…1` for the first) exercises continuation. The platform's `serve --dev` runs whole
sessions the same way, without Docker. Then push, and `hg ask` it.

## Egress

A package names the hosts it needs. During install, `install.egress` (a registry, a release
host); during a turn, `run.egress` plus, implicitly, the provider through the gateway. The door
resolves names itself, refuses private, loopback, link-local and multicast answers, and connects
to the address it checked. A CLI that phones home to a host not on the list simply cannot; the
door's log is kept with the turn.

## Without a provider

A package whose `harness.json` has `"provider_env": {}` names no provider: the turn is issued no
session token and no gateway, `HG_PROVIDER_URL` and `HG_PROVIDER_KEY_FILE` are empty, and nothing
in the sandbox can reach a model provider. A request to it needs no provider in its model path and
the organization needs none registered. This is how [any CLI](any-cli.md) that is not an agent runs,
and how a package with its own credentials, carried in its seeds under the organization's
responsibility, would.
