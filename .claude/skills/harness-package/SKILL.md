---
name: harness-package
description: Write or change a HarnessGateway harness package (contract 7) for a coding-agent CLI, from the mapping tables to a package that passes the three verification checks. Use when forking a catalog package for an organization, packaging a new CLI, bumping a CLI version, or fixing a package whose turns fail. Produces harness.json, install.sh, request.sh, a mapper, the seeds, and a recorded verification.
---

# Harness package

A package turns a Responses request into one run of a coding-agent CLI and the CLI's output into
Responses stream events. The platform hands the request over whole; the package decides what the
CLI can take and what its output means. This skill is the writing procedure. The research
procedure, which produces the tables a package is written from, is the `harness-mapping` skill;
the contract is `docs/packages.md`; the catalog's packages under `catalog/` are the worked examples
and `catalog/template` is the skeleton.

## Rules

1. **The request schema first.** A field in the request must do something. `input` is the prompt,
   `instructions` goes where the CLI reads instructions, `previous_response_id` resumes the CLI's
   own session, `reasoning.effort` sets its thinking level, `tools` of type `mcp` become its MCP
   config. What the CLI cannot take is ignored, and `accepts` in `harness.json` says what is taken.
2. **Then files, then flags.** When a CLI offers a file and a flag for the same thing, write the
   file the most harnesses read (`AGENTS.md` for instructions, the CLI's MCP config file) unless a
   flag is more faithful to the field (`--append-system-prompt`). The precedence table in
   `docs/responses-wire.md` has the reasoning.
3. **Nothing invented on the wire.** Every event printed is one the SDK parses: `response.output_item.added`,
   `response.output_text.delta`, `response.output_item.done` with an item of a real type
   (`message`, `reasoning`, `shell_call`, `shell_call_output`, `apply_patch_call`, `apply_patch_call_output`,
   `function_call`, `function_call_output`, `mcp_call`, `web_search_call`), `response.completed`
   with `usage`. The platform validates and fails the turn on anything else.
4. **Pin the CLI's version** in `install.sh` and `harness.json`, and name the registry host in
   `install.egress`. Nothing else is reachable during install.
5. **Keep the resume handle in `$HG_STATE`.** Whatever the CLI needs to continue (a session id,
   a thread id, a directory) is written there on turn one and used when `HG_PREVIOUS` is set.
6. **Say what does not work.** A check that does not pass is recorded as false in `catalog.json`
   with the reason in `notes`, never worked around by faking the output.
7. **Nothing secret in the package.** Keys arrive through the `provider_env` names at run time;
   a package that needs a key in a config file writes the file each turn from the variable.

## Procedure

### 1. Start from the nearest package

```
cp -r catalog/template pkg          # or the catalog package for the same CLI family
```

Read its `request.sh` and `map.py` end to end. Read the CLI's row in every table of
`docs/responses-wire.md`; if the CLI is not there, run the `harness-mapping` skill first.

### 2. harness.json

`cli.name` and `cli.version` as installed; `defaults.provider` the provider name the package is
built for and `defaults.model` a model that provider serves; `provider_env` the CLI's own variable
names for the key and the base URL; `interrupt` the signal `request.sh` traps; `install.egress`
the registry; `run.egress` empty unless the CLI must reach something beside the provider;
`accepts` the fields `request.sh` honours.

### 3. install.sh

Install the pinned version under `$HG_TOOLS` so that `$HG_TOOLS/bin/<cli>` exists. npm:
`npm install --global --prefix "$HG_TOOLS" --no-audit --no-fund <pkg>@<version>` with
`--allow-scripts` only for the packages whose install scripts are needed. pip: a venv under
`$HG_TOOLS/venv` and a symlink into `bin`. A binary release: `curl` from the host named in
`install.egress`, verified against its published checksum when there is one. It runs unprivileged
in the base image (Debian, Python 3.12, Node 22, git, curl, jq).

### 4. request.sh

In order:

1. `jq` the request: the prompt from `input` (a string, or the text parts of the messages),
   `instructions` (a string or the content of the instruction messages), `reasoning.effort`,
   the `mcp` tools, `text.format`, and whatever else `accepts` lists.
2. Write the seeds that depend on the request each turn: the instructions section of `AGENTS.md`
   (`instructions.py` from the template does this), the MCP config from `tools`, the CLI's
   config with `$HG_PROVIDER_URL` and the token (the `provider_env` names carry them; a CLI that
   reads a config file instead gets the file written here, every turn).
3. Trap the steer signal: read `$HG_DIR/interrupt` into a variable. The usual answer is to let the
   running invocation end and run the steer's text as a second invocation of the same session.
4. Run the CLI in its headless, auto-approving, JSON-output mode, with the resume handle from
   `$HG_STATE` when `HG_PREVIOUS` is set, and pipe its output through the mapper. Capture the
   session id the CLI announces (on stdout, stderr or a file) into `$HG_STATE`.
5. Exit with the CLI's exit code.

Anything printed that is not an event is kept as the turn's log: print what you would want to
read when a turn fails (`log: turn=… model=… resume=…`).

### 5. The mapper

`map.py` reads the CLI's JSON lines on stdin and prints events. Map each of the CLI's record
types to one of the item types above; stream text as `response.output_text.delta` against a
`message` item opened with `response.output_item.added` and closed with `response.output_item.done`;
close each tool call with its output item sharing the `call_id`; emit `response.completed` with the
usage when the CLI reports it (or in `response.sh` when it only writes a usage file). A record the
mapper does not know becomes a log line, not an invented item.

### 6. response.sh, when needed

Only when the final `output`, `usage` or `status` cannot be known from the stream: a usage file,
a single JSON envelope at the end, a CLI that reports failure in its exit code alone. Print one
JSON object with the keys the contract lists.

### 7. Seeds

`seeds/workspace/` holds what the CLI reads from the project and should find on the first turn
(`AGENTS.md` with the instructions section, rules, skills); `seeds/home/` what it reads from the
user and must not be allowed to change (its settings with permissions auto-approved, its trust
file for the workspace, a config that cannot hold the key). Nothing in `seeds/home` may contain a
secret; the key is written at run time.

### 8. Verify

```
HG_PROVIDER_URL=https://… HG_PROVIDER_KEY=$(cat key) HG_PROVIDER_API=anthropic-messages \
hg harness run ./pkg --install --state ./s --input "Create hello.txt containing hello, then say done."
hg harness run ./pkg --state ./s --request req2.json      # previous_response_id set: does it recall hello.txt?
hg harness run ./pkg --state ./s --request req3.json -v   # stream: true; do deltas arrive before the end?
```

The three checks are `make` (a file written and an answer), `recall` (the second turn knows the
first) and `stream` (events before the CLI exits). Record them in `catalog.json` under `verified`
with the date, the provider and the model, and the reason for any false in `notes`. Then push:

```
hg harness push NAME ./pkg -m "…"
```

and run it once through the platform (a conversation in the console, or `hg ask NAME "…"`) so
the sandbox's egress list and the install are proven too; the detail of a failed turn
(`hg response ID --detail`) has the log, stderr and the door's refusals.

### 9. Document

A catalog package's page is generated from `catalog.json` and `harness.json` by
`python scripts/catalog_docs.py`; keep `intro` and `notes` accurate and run it. A package of an
organization's own needs no more than its `description`.
