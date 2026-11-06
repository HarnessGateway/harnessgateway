# HarnessGateway: specification

HarnessGateway runs coding-agent CLIs (harnesses) as an API. A caller sends a standard OpenAI Responses request; the
platform runs it through a **harness package** in a gVisor sandbox and streams back standard Responses events. The
session survives the sandbox, so the next request continues it.

This document is the whole platform: what it does, how it is built, and how it is deployed, self-hosted, as one
organization. It is the reference every change is checked against. What is built on top of the core is specified
elsewhere; here is only what the core provides for it, the extension seam (§13).

## 1. Principles

1. **The contract is the OpenAI Responses schema, in both directions.** No HarnessGateway request or response schema
   exists. Any OpenAI SDK works unchanged. Output is validated with the OpenAI SDK's own types.
2. **The request serves the task; the package defines the agent.** What to work on, with which files, under which
   per-call guidance, comes in the request. What the agent *is* (its instructions, skills, hooks, subagents,
   permissions, MCP servers, egress, secrets it may use) is the package: operational IP, version-controlled.
3. **The platform knows no CLI.** Every CLI-specific fact (install method, command line, config paths, output format)
   lives in a package. The platform runs scripts, lays down seeds, moves bytes, enforces the sandbox and keeps sessions.
4. **No secret ever enters a sandbox.** Not the provider key, not an organization's secrets, not a caller's token. A
   sandbox holds placeholders; values are injected outside it, at the gateway or the sandbox's egress door.
5. **Narrowing is safe; widening is escalation.** A request may select or narrow what the package offers. It may never
   add capability the organization did not grant (§3.3).
6. **One organization, built to be extended.** Every row and object belongs to an organization and every query is
   scoped to one. Identity is the core's: accounts, two roles, keys, sign-ins and audit. Self-hosted runs one
   organization; the extension seam (§13) is where more is built, without forking the core.
7. **Stateless roles, one stateful store.** API, gateway and node processes keep no state that matters across restarts;
   Postgres and an object store hold everything. Any role scales by running more of it.
8. **KISS, DRY, OOP, SRP, negative space.** Each module has one job. What is not allowed is refused at the boundary,
   loudly, with a message that says what is allowed. Nothing speculative is built.

## 2. Vocabulary

| Term | Meaning |
|---|---|
| Organization (org) | The tenant. Owns accounts, keys, providers, egress, harnesses, sessions, files, usage. Self-hosted has one. |
| Account | A person: an email, a password, a role in the organization. |
| API key | What an application is built around: named after the app, with a role; run-only when it only runs. |
| Principal | Whoever a request runs as: an account's sign-in or an API key, resolved to organization, actor, role. |
| Sign-in | An account's bearer token after a login; hashed, expiring, revocable. Not a sandbox session. |
| Operator | The platform's owner: a platform admin's account, or the operator key for automation. |
| Harness package | A directory (a manifest, two executables, seeds) that teaches the platform one CLI and one agent. |
| Harness | A package installed in an org under a name, with the org's settings and secret values. |
| Provider | An org's model endpoint: an API shape (`anthropic`, `openai`, `google`), a base URL, a sealed key. |
| Session (container) | The continuing conversation and workspace of one harness. Exposed as an OpenAI container. |
| Turn (response) | One request on a session: one run of the package's `run`. Exposed as an OpenAI response. |
| Sandbox (box) | The gVisor container a session runs in. Disposable; the session is not. |
| Door | The box's only network exit: an egress allowlist, secret injection, and the provider relay. |
| Gateway | The service that swaps a turn's token for the org's provider key and relays model calls. |
| Node | A host that runs boxes. Pulls turns from the API. |

## 3. The seam: request, package, union

### 3.1 The rule

Three questions place everything:

1. Does it change the **agent** (capability, policy, standing knowledge) or the **task** (the work, its data, its
   framing, its budget, the shape of the answer)? Agent → package. Task → request.
2. Would taking it from the request **widen** what the agent can do? Then the request may only select or narrow it.
3. Is it a **secret**? Then neither package files nor request bodies carry it; the org stores it sealed, and the
   platform injects it outside the sandbox.

### 3.2 The split

| Concern | Request (the caller) | Package (the org's IP) | Union rule |
|---|---|---|---|
| Work items | `input` text, `input_file`, `input_image` | a starter project (`seeds/workspace`), optional | request files are placed in the workspace as work, by name, never at config paths |
| Instructions | `instructions`; `system`/`developer` messages in `input` | standing system prompt, instruction files, rules, persona (`seeds/home`) | the package's standing prompt first, the request's appended. Replace only if the package chooses to map `instructions` to its CLI's replace mechanism |
| Skills, hooks, subagents, commands, permissions, CLI settings | — | `seeds/home` | package only |
| MCP servers | `tools[type=mcp]`: select a package server by `server_label` and narrow with `allowed_tools`; add a server whose host the org already allows | the catalog: `mcp` in `harness.json` | §3.3 |
| Built-in tools | narrow only (`tool_choice: "none"`, `allowed_tools`) | which tools exist and their permission rules | intersection; what a CLI cannot narrow, its package reports as ignored |
| Model and provider | `model` = `harness[/provider[/model]]` | provider APIs spoken, default model | the request picks within what the package speaks |
| Per-call knobs | `reasoning`, `text.format`, `max_output_tokens`, `max_tool_calls`, `stream`, `background`, `store` | defaults and their mapping to the CLI | the request wins where the CLI honours it; otherwise the package reports it ignored |
| Egress | — | `run.egress`, `install.egress` (allow and `!deny` entries) | the org's entries apply to every harness; a harness widens or narrows them for its sandboxes; a deny wins (§7.2) |
| Secrets | an MCP tool's `authorization`/`headers` values (the caller's own token) | secret names, the hosts each may reach | values sealed in the org, injected at the door, scoped to hosts (§8) |
| Continuity | `previous_response_id` | `persist`: the CLI's session store | the platform snapshots workspace, state and `persist` (§6.4) and hands every turn the session's conversation so far in Responses form (§5.6); a package resumes its CLI natively when it can and from that history otherwise |

**Home holds the IP, the workspace holds the work.** `seeds/home` is laid down in every new sandbox and is not persisted
(unless named in `persist`, which a package uses only for the CLI's session store). So the org's instructions, skills
and hooks are re-applied by the package version on every sandbox, cannot be permanently edited by the agent, and take
effect on upgrade without migration. The workspace belongs to the caller: persisted, worked in, downloaded from.

### 3.3 MCP servers: the one true union

A package declares its catalog in `harness.json` (§5.2). The platform resolves the turn's MCP servers, CLI-agnostically,
into `/run/hg/mcp.json` (§5.6), which the package renders into its CLI's configuration:

1. A request `mcp` tool **without** `server_url` selects the catalog server of that `server_label` (unknown label: 400
   `mcp_server_not_found`). Its `allowed_tools` narrows the catalog's.
2. If the request selects any catalog server, only the selected catalog servers are active; otherwise all are.
3. A request `mcp` tool **with** `server_url` adds a server. Its host must already be in the turn's effective egress
   (§7.2), else 400 `mcp_server_not_allowed`. Its label must not collide with the catalog's (400 `mcp_label_conflict`).
4. Values of an added server's `authorization` and `headers` are the caller's secrets (§8.3): replaced by placeholders
   before the request is stored or delivered, injected by the door for that server's host for that turn only.
5. `require_approval` is accepted and ignored: a harness runs unattended inside its sandbox.

## 4. The Responses contract

### 4.1 In

`POST /v1/responses` takes a body validated against the OpenAI SDK's `ResponseCreateParams` (pinned SDK version). The
platform reads only what routes and secures a turn:

| Field | Platform use |
|---|---|
| `model` | `harness[/provider[/model]]`: the installed harness, the org's provider, the model name the CLI gets. Absent parts fall back: provider to the session's, the harness's default, else the org's only provider of an API the package speaks; model to the session's last, the harness's default, the manifest's default. |
| `input` | `file_id` references in `input_file`/`input_image` parts are checked and delivered to the sandbox. |
| `previous_response_id` | the response to continue from. Its session's last response: the turn joins that session. An earlier response: a **fork**, a new session restored from the state saved after that response (§6.4), named in the container's `metadata.forked_from`; the original session is untouched. Responses form a tree, as in OpenAI's API. Provider and model may change. Another harness in `model` is a fork onto it: a new session on that harness, restored from the checkpoint's workspace only (the old package's `$HG_STATE` and `persist` paths are the old CLI's), with the conversation reaching the new harness as the session's history (§5.6). 409 while that response is still running or its state still being saved. |
| `tools[type=mcp]` | resolved per §3.3; secret values extracted per §8.3. |
| `stream`, `background`, `store` | delivery and retention. `store: false` runs a session that is not snapshotted and cannot be continued. |
| `metadata` | echoed. |
| `conversation` | refused: 400, "continue with previous_response_id". |

Everything else reaches the package as the caller sent it (with §3.3's secret values replaced). The package maps what
its CLI supports and names what it ignored (§4.3).

### 4.2 Out

The package's `run` prints one JSON object per stdout line. A line that parses as an object whose `type` starts with
`response.` is a Responses stream event; any other line is a log line (kept, never delivered).

- The platform owns the envelope: it emits `response.created`, `response.in_progress`, assigns every
  `sequence_number`, and builds the final `Response` from the package's terminal event.
- Each event is validated against the SDK model for its `type`; each item against the SDK model for its item type. An
  invalid line fails the turn with `hg_reason=invalid_output` and a message naming the field.
- Exactly one terminal event (`response.completed`, `response.incomplete`, `response.failed`) ends the package's
  output; anything printed after it fails the turn.
- Delta events stream live and are discarded once their item is done; every other event is kept.
- Stored text holds no NUL: where a package's output carries U+0000 (a binary read back as text), U+FFFD stands
  in, in every stored record. Nothing else in an event is changed.
- The final `Response` echoes the request's parameters (secrets redacted), the package's `output` (or the items it
  completed), `usage`, `error`/`incomplete_details`, and `metadata`: the request's, the package's, plus `session_id`
  and, on platform-caused endings, `hg_reason`.
- Typical items a package emits: `message`, `reasoning`, `shell_call`/`shell_call_output`,
  `apply_patch_call`/`apply_patch_call_output`, `mcp_call`, `function_call`/`function_call_output`.

### 4.3 What a package could not honour

A package names request fields it did not act on in its terminal event's `metadata.hg_ignored_fields`
(comma-separated). `hgkit` provides it. A silently ignored field is indistinguishable from an honoured one.

### 4.4 Errors

OpenAI's error shape: `{"error": {"type", "code", "message", "param"}}`. A turn that fails after acceptance is a
`Response` with `status: "failed"`; codes outside the SDK's enum become `server_error` with the real code in
`metadata.hg_error_code`. Platform reasons (`hg_reason`): `cancelled`, `timed_out`, `install_failed`,
`sandbox_failed`, `invalid_output`, `node_lost`, `exited_without_result`.

## 5. Harness packages

### 5.1 Layout

```
harness.json          the manifest (§5.2)
install               an executable: installs the CLI into $HG_TOOLS
run                   an executable: one turn, the request in, Responses events out
seeds/home/           laid into $HOME in every new sandbox: the agent's standing configuration
seeds/workspace/      laid into the workspace of a new session: a starter project
anything else         the package's own (modules, README, tests): the platform reads none of it
```

The platform reads the manifest, runs the two executables and copies the seeds; it knows no language. `install` and
`run` are scripts whose first line names their interpreter, or Linux binaries, and are run as they are (the platform
marks them executable at install).

A package is identified by its **digest**: SHA-256 over every file's path, executable bit and bytes. It moves as a
canonical gzipped tar (sorted, owner 0, mtime 0), so the same files give the same bytes. Limits: 5,000 files, 256 MiB
unpacked. Regular files and directories only; nothing outside the root.

### 5.2 Manifest

```json
{
  "contract": 1,
  "name": "claude-code",
  "version": "2.1.310-1",
  "description": "…",
  "provider": {"apis": ["anthropic"]},
  "defaults": {"model": "claude-sonnet-5-5"},
  "install": {"egress": ["registry.npmjs.org"], "timeout_s": 1200},
  "run": {"egress": [], "timeout_s": null, "stop_grace_s": 10},
  "persist": [".claude/projects"],
  "settings": {"LOG_LEVEL": {"description": "…", "default": "info"}},
  "secrets": {"GITHUB_TOKEN": {"description": "…", "hosts": ["api.github.com"], "required": false}},
  "mcp": {"github": {"url": "https://api.githubcopilot.com/mcp/",
                     "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
                     "allowed_tools": ["search_issues", "get_issue"]}}
}
```

| Field | Rule |
|---|---|
| `contract` | `1`. |
| `name` | `[a-z0-9][a-z0-9._-]{0,62}`. |
| `version` | `<CLI version>-<package revision>`; handed to `install` and `run` as `HG_PACKAGE_VERSION`, so the CLI's version is written once. |
| `provider.apis` | subset of `anthropic`, `openai`, `google`; absent means the harness runs without a model. |
| `install.egress`, `run.egress` | egress entries (§7.2). `install.timeout_s` 1–3600, default 900. |
| `run.timeout_s` | the package's own bound on a turn, if any. `run.stop_grace_s` 1–300, default 20. |
| `persist` | at most 64 normalized paths relative to `$HOME`, without `..`: what survives besides the workspace and state. |
| `settings` | non-secret configuration, ≤64, names `[A-Z][A-Z0-9_]{0,63}` not starting `HG_` and not a reserved name (`HOME`, `PATH`, `LANG`, proxy and CA variables); exported to `run` as environment variables. |
| `secrets` | secret names (same naming rule), each with the `hosts` it may reach (egress-entry syntax). Exported to `run` as placeholders (§8). |
| `mcp` | the catalog: label → `{url, transport?, headers?, allowed_tools?}`. `${NAME}` in a header value names a declared secret. A catalog server's host joins the run egress and its secrets' hosts. |

Unknown fields are refused, with the field named.

### 5.3 One package per harness

A harness is one package: the CLI's install and mapping and, when the org wants it, the org's own agent on that CLI
(`seeds/home` with its instruction files, skills and hooks; its `mcp` catalog, `settings` and `secrets`). There is no
composition between packages. An org that wants its own agent on a CLI another package already maps copies that
package in the console (open it, **Edit**, change `name`) and adds its seeds; the two are then separate harnesses,
each pinned by its own digest. Packages others start from stay *vanilla*: the CLI as its vendor ships it plus what it
needs to run headless in the sandbox, nothing an org would want to own.

### 5.4 Install

`install` runs once per (package digest, sandbox image id) on a node, in an install box: gVisor, the sandbox image,
writable `$HG_TOOLS` and `/tmp`, egress = org egress ∪ `install.egress`, no provider, no secrets, within
`install.timeout_s`. On success `$HG_TOOLS` is kept on the node and mounted read-only into every box of that package. A
package installs its CLI by the CLI's official, documented method, pinned to the manifest's version, and verifies it.

### 5.5 Run

`run` runs once per turn, in the session's box, in its own process group, with the working directory
`$HG_WORKSPACE`, the request on stdin, stdout as the turn's events (§4.2), stderr as the turn's log. Its exit ends the
turn's process; its terminal event ends the turn's response. After the terminal event it has `stop_grace_s` to exit.

### 5.6 What a turn sees

| Path / variable | Content |
|---|---|
| `/run/hg/request.json` (`HG_REQUEST`) | the request as stored (secret values replaced by placeholders) |
| `/run/hg/files/` (`HG_FILES`) | `file_id` inputs at `<id>/<filename>`, and `index.json`: `{id: {filename, path, bytes}}` |
| `/run/hg/mcp.json` (`HG_MCP`) | the turn's MCP servers: `[{label, url, transport, headers, allowed_tools}]`, header values holding placeholders |
| `/run/hg/history.json` (`HG_HISTORY`) | the session's earlier turns in order, through every fork back to the root: `[{id, status, input, output}]`, the input items (file bytes left out) and output items of each, in Responses form; `[]` on a first turn. The conversation in the platform's own format, for a package whose CLI cannot resume its own session (§6.3) |
| `/run/hg/history.json` (`HG_HISTORY`) | the session's earlier turns in order, through every fork back to the root: `[{id, status, input, output}]`, the input items (file bytes left out) and output items of each, in Responses form; `[]` on a first turn. The conversation in the platform's own format, for a package whose CLI cannot resume its own session (§6.3) |
| `HG_WORKSPACE` = `/workspace` | the session's workspace (persisted) |
| `HG_STATE` = `/state` | the package's small session state, e.g. the CLI's session id (persisted) |
| `HOME` = `/home/hg` | seeded from `seeds/home` in every new box; only `persist` paths survive |
| `HG_PACKAGE_DIR` = `/opt/hg/package` | the composed package, read-only |
| `HG_TOOLS` = `/opt/hg/tools` | the installed CLI, read-only |
| `HG_PACKAGE_VERSION` | the manifest's `version` (also given to `install`) |
| `HG_SESSION`, `HG_RESPONSE`, `HG_TURN` | session id, response id, turn number |
| `HG_MODEL`, `HG_PROVIDER_API`, `HG_PROVIDER_URL`, `HG_PROVIDER_TOKEN` | the model name; the provider's API shape; `http://hg-door:8080`; the turn's token (§7.4) |
| settings, secrets | each declared setting as its value; each declared secret as its placeholder |

`/run/hg` and the package and tools are read-only to the box's user.

### 5.7 hgkit

The Responses contract in Python: a small library in the sandbox image (`/opt/hg/lib`, on `PYTHONPATH`), optional,
and harness-agnostic: it knows the request a turn receives and the events it must print, and nothing about any CLI.

- `Request`: the request, with readers for prompt text, instructions (request + system/developer messages), reasoning
  effort, text format, MCP servers (`/run/hg/mcp.json`), `place_inputs()`, which writes `file_id` and inline files
  into the workspace by name (a collision gets a numeric suffix) and returns their names, `history()` and
  `transcript()` (the session's history, as items and as plain text a CLI can be given), and the turn's environment
  (`model`, `provider`, `state`, ids, the package version).
- `Emitter`: prints valid events, named by what the agent did: `say` and `think` (streamed), `ran`, `wrote`,
  `edited`, `deleted`, `called` (MCP), `used` (any tool), each as the whole call or a handle whose `done`/`failed`
  reports the result later; `item` and `emit` for any other Responses item or event; `ignored(fields)`; exactly one
  terminal event: `done`, `stopped`, `failed`.
- `validate`: the reader the platform itself uses (§4.2), on the same pinned OpenAI SDK: `Emitter(validate=True)`
  checks each event as it is printed, `check(text)` reads a whole output, `python3 -m hgkit check` does so from the
  command line. The platform's own reader is this module; a package that passes it passes the platform.

### 5.8 The template and the built-in

The platform ships two packages. `shell` (no model: the input runs as a bash command; the reference and test
package) is the template: the console starts every new harness as a copy of it under the name the user gives.
`harness-package-builder` (the claude-code package seeded with the platform's harness-package-builder skill, the
template and the packages guide) is the one built-in harness: `init` installs it into the org (`HG_BUILDER_DIR`), and
it writes packages into its workspace, which the console turns into a draft from that session's page and from no
other. The builder is the one shipped package that is not vanilla; its seeds are the platform's own. Every other
harness is one someone wrote, by hand from the template or with the builder. Each package follows one layout:
`harness.json`, `install`, `run`, `README.md` (Files, The command, Request fields, Output mapping, Known limits,
Tests), `tests/` with fixtures recorded from real runs. The template's README says, file by file, what to change for
a CLI. The builder's skill is self-contained under `seeds/home`: it carries the template and the packages guide as
copies kept current from their sources (`scripts/sync-builder-seeds`; a unit test fails when they drift), so every
deployment's builder ships the current skill, template and guide, and a builder session's workspace holds only the
packages it delivers.

## 6. Sandboxes and sessions

### 6.1 The box

One per session at a time, on one node:

- gVisor (`runsc`); another runtime only when the operator sets `HG_UNSAFE_RUNTIME` for development.
- uid 10001, no capabilities, `no-new-privileges`, read-only root, `/tmp` a size-limited tmpfs.
- CPU, memory, swap (= memory), pids limits from the org's capacity. The memory limit is `memory_mb` for the box's
  processes plus `tmp_mb`: `/tmp` is a tmpfs, which the kernel charges to the box. A box killed for its memory ends
  its turn with a failure that says so.
- Writable: `/workspace`, `/state`, `/home/hg` (bind mounts of the node's per-session directories). Read-only:
  `/run/hg`, `/opt/hg/package`, `/opt/hg/tools`, `/opt/hg/runtime`.
- An internal network whose only peer is the box's door; no DNS (the door resolves names); proxy variables point at the
  door; the door's CA is trusted (§8.2).
- The node never touches a box's writable files as root: seeding, restore, snapshot and file staging run inside the box
  as its user.

### 6.2 Lifecycle of a turn on a node

1. **Claim.** The node claims the next turn it may run (§10.2) and receives a job: ids, the composed package's
   location and digests, the request's location, file locations, staged container operations, the session's snapshot
   location, capacity, effective egress, settings, secret bindings (§8), the provider token.
2. **Package and tools.** Fetched and verified against the digest; tools installed if absent (§5.4).
3. **Box.** The session's warm box if this node ran its last turn and holds it; otherwise a new box: home seeds, then the
   snapshot (or, for a new session, the workspace seeds).
4. **Inputs.** `/run/hg` written; staged container file operations applied to the workspace; the door's turn rules set.
5. **Run.** `run` started; stdout sent to the API in byte batches as it arrives; stderr kept.
6. **End.** On exit, cancel (SIGTERM to the process group, then SIGKILL to every process in the box after
   `stop_grace_s`), timeout (the tighter of the package's and the org's), or the grace after the terminal event: the end
   is reported, the door's secret bindings are cleared, the turn token revoked.
7. **Snapshot.** In the background, under the session's lock: if the included paths changed, archived and uploaded
   (§6.4). The box stays warm until its idle TTL; then it is destroyed.

One turn per session runs at a time; later turns of a session queue in order.

### 6.3 Continuity

`previous_response_id` continues from a response: on the session's last response and the same harness, the same
session; on an earlier response, a fork: a new session, pinned to the same package digest, restored from that
response's checkpoint; on another harness, a fork onto it: a new session pinned to that harness's package, restored
from the checkpoint's workspace only. Provider and model are free to change at any point. A session ends when deleted,
or after the org's retention.

The platform carries the conversation in two harness-agnostic forms, and knows no CLI to do so. The snapshot (§6.4)
carries the CLI's own session store where the package named it in `persist`; it is the richer form and the package's
first choice. The history (`/run/hg/history.json`, §5.6) carries every earlier turn's input and output items in
Responses form, the platform's own record; it is what a package continues from when the CLI's session is not there:
a turn killed mid-way (a stop or an interrupt), a fork onto this harness from another, a CLI that keeps no session.
How a CLI is resumed, and how the history becomes its context, is the package's; the platform only moves the two.
A client rewinds, branches or switches harness by naming an earlier response; nothing else is needed, and the harness
is not told, because from inside the sandbox each is a continuation on a fresh box.

### 6.4 Snapshots

Include-only, the way egress is allow-only: `/workspace`, `/state`, and `$HOME/<persist>` paths. Everything else
(tools, caches, `/tmp`, the rest of `$HOME`) belongs to the box. The node lists the included paths inside the box; the
listing's hash is the state's fingerprint; when unchanged, nothing is uploaded. Otherwise a gzipped tar is uploaded to
the object store with the workspace's file list (for the Containers API). Restore unpacks it after the home seeds, so
the session's own state wins. Every outcome (completed, failed, cancelled, timed out) is snapshotted. A node lost
mid-turn loses that turn's changes; the session restores from its last snapshot.

Every snapshot is kept for the session's life (its retention, or its deletion): each turn records the snapshot that
holds the state after it, its **checkpoint**, which a later request forks from by naming the turn (§4.1). A fork copies
the checkpoint under the new session, so deleting the parent cannot take it away. A turn that changed nothing, or saved
nothing of its own (cancelled before it ran, lost with its node), points at the last checkpoint before it; one whose
save failed has none, and cannot be forked from. Storage is metered (§12) and bounded
by the organization's retention.

## 7. Network

### 7.1 The door

A small process per box (stdlib Python plus `cryptography`), run from the platform image in its own container on the
box's internal network (`hg-door`) and the node's uplink network:

- **:3128, CONNECT proxy.** A host:port not allowed gets 403. Names are resolved by the door; it connects to the
  address it checked; private, loopback, link-local and metadata addresses are refused unless the operator set
  `HG_ALLOW_PRIVATE_EGRESS`. Hosts with no secret binding are tunnelled untouched (no TLS interception). Hosts with a
  binding are intercepted (§8.2).
- **:8080, provider relay.** Plain HTTP from the box, relayed to the gateway over TLS, with the door's node credential
  attached (§7.4).
- Counts every byte in and out, per turn and for the box, for metering (§12). Logs one line per decision (host, port,
  verdict, intercepted or not, bytes); never a header, body or value.
- Rules (allowlist, bindings) come from a file the node writes into the door's private directory per turn; the door
  reloads it when it changes. The box cannot read it.

### 7.2 Egress

An egress entry is `host`, `*.domain` (any depth of subdomains, not the domain itself) or `*`, with an optional
`:port` (443 when absent). An entry prefixed with `!` denies instead of allowing. The same syntax at every level; the
levels differ only in scope:

| Level | Set by | Applies to |
|---|---|---|
| Org | the org's admin (`PUT /v1/egress`) | every sandbox of the org |
| Harness | the package's manifest (`run.egress`, `install.egress`), plus its catalog MCP hosts and its secrets' hosts | that harness's sandboxes |

A connection is allowed when an allow entry of either level matches it and no deny entry of either level does. So the
org's list applies to every harness; a harness widens it for its own sandboxes with allow entries and narrows it with
deny entries; a deny wins at either level. Install egress is composed the same way from the org's list and
`install.egress`. The private-address rule applies on top and cannot be lifted by an org or package. Provider calls
never need egress: they go through the relay.

### 7.3 The gateway

Stateless HTTP service, the only process where provider keys are in the clear. For each call: the presented turn token
and the door's node credential → the grant (turn, org, provider; cached ≤10 s) → only that provider API's paths
(Anthropic `/v1/messages…`, OpenAI `/v1/responses…`, `/v1/chat/completions`, `/v1/models`, Google
`/v1beta/models…`) → the key set in the provider's auth header → streamed to the provider's base URL with no read
timeout → streamed back. Every exchange is recorded for observability (§11). The provider's host is re-checked against
the private-address rule.

### 7.4 The turn token

Minted when a node claims a turn, scoped to one turn and one provider, revoked when the turn ends (and by the sweeper if
its node is lost). The gateway honours it only when presented with the credential of the node running the turn, which
only that node's doors hold: a token copied out of a sandbox is useless anywhere else.

## 8. Settings and secrets

### 8.1 Storage

Settings are plain values on the org's harness. Secrets (harness secret values, provider keys) are sealed: AES-256-GCM
under a per-org data key, the data key wrapped by the key wrapper port (a local master key, or a KMS), with the org,
kind and name as associated data. A sealed value is written by the API, opened by the scheduler (for a turn's bindings)
or the gateway (provider keys), and never returned by any read API.

### 8.2 Injection at the door

For each turn, every secret the package declares and the org has set is bound to a fresh random placeholder
(`hgs_<32 hex>`). The box sees only placeholders (as the declared environment variable, and inside `/run/hg/mcp.json`
headers). The door's turn rules carry `{placeholder, value, hosts}`.

For a CONNECT to a host with a binding, the door terminates TLS with a certificate it issues for that host from the
node's CA (generated on the node at start; its key never leaves the node; its certificate is trusted inside boxes),
offers only HTTP/1.1, verifies the upstream normally, and in each request **header** replaces the placeholders bound
to that host with their values. Bodies, URLs and responses are not touched. A placeholder sent to any other host is
sent as it is: worthless. Bindings live only for the turn.

### 8.3 The caller's own token

An added MCP server's `authorization` and `headers` values (§3.3) are taken out of the request before it is stored:
the stored and delivered request holds placeholders, the values are sealed into the turn's bindings (bound to that
server's host) until the turn ends, then deleted. They are never echoed, logged, or captured.

### 8.4 What a sandbox can and cannot learn

It can learn placeholders, its turn token (useless outside its door), and anything the injected services send back. It
cannot learn a provider key, an org secret, or a caller's token. A package that needs a secret in a request **body**
or URL cannot have it injected; it is refused rather than given the value.

## 9. Tenancy, identity and access

### 9.1 Organizations

The **organization** is the tenant: a team that builds harness packages and the applications around them. Every
tenant table carries `org_id`, and row-level security confines a tenant transaction to the organization it declared
(`hg.org`); platform work (scheduling, sweeping, the operator) runs on a separate scope (`hg.scope = platform`).
Object keys are `orgs/<org_id>/…`. Self-hosted runs one organization; an extension (§13) may create more, on the same
identity code. A company that wants isolation between groups runs them as organizations; an account may belong to
several.

### 9.2 Principals and roles

A **principal** is a person (an account's sign-in) or a program (an API key). Both resolve to one shape: `org`, `actor`
(`account:<id>` or `key:<id>`), `role`, `run_only` (keys) and `platform_admin` (accounts). Two roles:

| Role | May |
|---|---|
| `admin` | everything in the organization: providers, egress, accounts, every key, harnesses, sessions, usage, traces, audit |
| `developer` | install, configure and uninstall harnesses, use the builder, the chat and the playground, create and revoke its own keys, read the organization's sessions, traces and usage; read providers (names, shapes, URLs) and egress |

An **API key** is what an application is built around: it is named after the app (its label is required), carries a
role, and with `run_only` it may create responses, read, continue and cancel the sessions it made, upload files and
read `/models`, and nothing else: this is what an application holds. A session records the key (or person) that
created it, so an app's sessions and usage are read by its key, with no further concept. A key may carry an expiry.

Every check is one call, `Principal.require(resource, action)`, made by the service that owns the resource; the table of
who may do what lives in one domain module, and a refusal names the role that may.

| Resource | Actions | `admin` | `developer` | run-only key |
|---|---|---|---|---|
| organization | read | ✓ | ✓ | ✓ |
| accounts | create, read, update, reset | ✓ | read self | |
| providers | create, read, update, delete | ✓ | read | read |
| egress | read, update | ✓ | read | |
| harnesses | install, read, configure, uninstall | ✓ | ✓ | read |
| keys | create, read, revoke | ✓ | its own | |
| responses, sessions, files | create, read, cancel, delete | ✓ | ✓ | its own |
| usage, traces | read | ✓ | ✓ | its own |
| audit | read | ✓ | | |
| platform (`/internal`) | all | platform admins, the operator key | | |

### 9.3 Accounts and sign-ins

- An **account** is a person: an email (the login), a name, a password hash (scrypt), a status (`invited`, `active`,
  `disabled`), a platform-admin flag, and a **membership** in the organization with a role. An account in several
  organizations holds one membership per organization; self-hosted holds one.
- An admin creates an account (or `init` creates the first admin). It arrives **invited**: a one-time invitation link
  (the console's setup page with a token `hgi_…`, valid seven days) is mailed to the person when mail is configured
  (`HG_SMTP_URL`, `HG_MAIL_FROM`, `HG_CONSOLE_URL`) and returned to the creator once either way; the person activates
  the account by choosing a password with it. An admin resets a password the same way. There is no default password
  anywhere. A password is at least 12 characters; ten failed sign-ins lock the account for fifteen minutes; every
  attempt is audited. Disabling an account revokes its sign-ins and its keys.
- A **sign-in** is an opaque token (`hgl_…`), stored as an HMAC under the pepper with its account, expiry (twelve
  hours), last use and client, revoked by logout, by disabling the account, or by a password change. The console keeps
  it in an HttpOnly cookie and presents it as a bearer; the API resolves it to a principal exactly as it does a key.
- Single sign-on (OIDC against the organization's identity provider, just-in-time accounts, group-to-role mapping) is
  the next identity feature (§20). Nothing above changes for it: an SSO login produces the same sign-in.

### 9.4 The platform scope

- The **operator** is the platform's owner: whoever deployed it. The platform scope sees every organization, the
  nodes, the queue and the configuration, and invites an organization's admin; it does not work inside an
  organization unless the same account is also a member of it.
- People hold it as a flag on their account (`platform_admin`), never as a shared secret. `init` gives it to the first
  admin; a platform admin grants it to others. `HG_OPERATOR_KEY` remains for automation (node tokens, draining,
  metrics scrapers) and for break-glass; its use is audited.
- `/internal` routes for people accept a platform admin's sign-in or the operator key. Nodes use their own
  credential.

### 9.5 Audit

Every change of standing is a row in `audit`: sign-ins, their failures and locks; accounts created, activated, reset,
disabled; keys created and revoked; providers and egress changed; harnesses installed,
configured and uninstalled. A row is when, who (the actor), what (a dotted action such as `key.create`), the target and
a detail object that never holds a secret. Admins read their organization's log (`GET /v1/audit`); the platform scope
reads all (`GET /internal/audit`).

### 9.6 Capacity

Per organization: `vcpus`, `memory_mb` (the processes), `pids`, `tmp_mb` (`/tmp`, a tmpfs charged on top of
`memory_mb`) per box; `max_sandboxes`, `max_running_turns`; `idle_ttl_s`; `turn_timeout_s`; `retention_days`;
`node_pool`; `observability` (§11). Self-hosted takes defaults from configuration; the platform scope sets an
organization's own. An extension that creates organizations (§13) authenticates nobody in a way of its own: accounts,
roles, sign-ins and audit are the core's.

## 10. Scale and deployment

### 10.1 Processes

One platform image, one CLI, `harnessgateway <command>`:

| Command | Role | Scales by |
|---|---|---|
| `serve --role api` | public API, SSE streaming, internal node API | replicas behind any load balancer |
| `serve --role gateway` | provider relay | replicas behind any load balancer |
| `serve --role api,gateway` | both in one process (small deployments) | replicas |
| `node` | runs boxes on its host's Docker with gVisor | more hosts |
| `migrate` | applies schema migrations (advisory-locked, idempotent; `serve` runs it too) | — |
| `init` | creates the org with the built-in harness package builder, the first admin's invitation and an admin key (idempotent; prints nothing new when run again) | — |
| `door` | the per-box door (started by the node) | — |
| `node-token create` | prints a join token for a node pool (operator) | — |
| `keygen` | prints fresh secrets for configuration | — |

Plus the sandbox image. Both images are versioned together.

### 10.2 Coordination

Postgres is the queue and the bus: nodes claim turns with `FOR UPDATE SKIP LOCKED`; stream followers on any API replica
wake on `LISTEN/NOTIFY`; a periodic sweeper (any API replica, under an advisory lock) fails the turns of nodes silent
for 45 s and releases their sessions. Claim order: orgs by fewest running turns, then age; a session's turns in order;
a session sticks to the node holding its warm box or saving its state; within the org's capacity; within the node's
pool. Nodes hold no credential but their own: everything else travels by short-lived signed links.

### 10.3 Storage

- **Postgres** 16+: all state.
- **Object store**: package archives, request bodies, files, snapshots, captures, logs. Two adapters: a filesystem
  directory (single host; the API serves signed links itself) and any S3-compatible service (multi-host).

### 10.4 Self-hosted, one host

`deploy/` holds `compose.yaml` and `install.sh`. On a Linux host with Docker:

```
curl -fsSL <release>/install.sh | sh     # checks Docker, installs gVisor from its official repository if absent,
                                         # writes .env with generated secrets, pulls images, compose up, init
```

The compose project runs `postgres`, `harnessgateway` (`api,gateway`), `node` (with the Docker socket and the
`runsc` runtime) and `console`, on a filesystem object store volume. `init` prints, once, the first admin's invitation
link (the console's setup page) and an organization admin key for automation; the console holds no key of its own.

### 10.5 Self-hosted, many hosts

- Point `HG_DATABASE_URL` at one Postgres (managed recommended) and `HG_BLOB_URL` at an S3-compatible bucket.
- Run `api` and `gateway` replicas anywhere, behind a load balancer.
- On each node host (Docker + gVisor): `docker run … harnessgateway node` with `HG_API_URL`, `HG_GATEWAY_URL` and a
  node join token (`harnessgateway node-token create`, or `HG_NODE_JOIN_TOKEN` shared by all nodes of the default
  pool). A node joins, gets its own credential, and starts claiming. Draining: `POST /internal/nodes/{id}/drain`
  (operator key); the node finishes its turns and takes no new ones.

### 10.6 Configuration

Environment variables only, all `HG_` prefixed, validated at start with every error reported at once:
`HG_DATABASE_URL`, `HG_BLOB_URL`, `HG_SECRET_KEY` (master key for the local key wrapper and the pepper),
`HG_OPERATOR_KEY` (the operator's key for node tokens and draining), `HG_KMS_KEY`
(optional, KMS adapter), `HG_API_URL`, `HG_GATEWAY_URL`, `HG_NODE_JOIN_TOKEN`, `HG_NODE_SLOTS`,
`HG_NODE_DIR`, `HG_SANDBOX_IMAGE`, `HG_PLATFORM_IMAGE`, `HG_UPLINK_NETWORK`, `HG_UPSTREAM_PROXY`, `HG_CA_FILE` (an operator CA trusted by doors and boxes),
`HG_ALLOW_PRIVATE_EGRESS`, `HG_UNSAFE_RUNTIME`, `HG_EXTENSIONS`, capacity defaults (`HG_DEFAULT_*`), `HG_LOG_LEVEL`.

## 11. Observability

Two kinds of record, both per turn, both org-scoped, both written to the object store under the turn and handed to
extensions (§13) as they happen:

| Record | Content | Level |
|---|---|---|
| Harness exchange | the request as delivered to the sandbox (placeholders, not values), every stdout line (events and logs) and stderr, timings, exit code, the final Response | `metadata`: timings, sizes, exit, outcome. `full`: the bodies too |
| Gateway exchange | method, path, provider host, status, timings, request and response sizes; bodies (≤4 MiB each) | `metadata` / `full` as above |
| Door exchange | host, port, verdict, intercepted, bytes | always (it is also metering's source) |

The org's `observability` is `metadata` (default) or `full`. `GET /v1/traces/{response_id}` returns a turn's records.

**Metrics.** `GET /internal/metrics` (operator key or platform admin) is the platform's state in Prometheus text
exposition, for whatever the deployer already scrapes: turns queued and running, the oldest queued age, nodes live and
their slots, warm sandboxes, turns ended in the last 24 hours by status and reason, usage totals by meter, and the
gateway's relayed calls by status. The deployer console reads the same numbers.
Captures never contain a secret value: placeholders are what the sandbox had, injected values exist only inside the
door's and gateway's outbound requests, which are recorded with auth headers removed. Retention follows the org's.
Platform logs are JSON lines on stdout with ids, never bodies or values.

## 12. Metering

Infrastructure usage, measured where it happens, recorded as an append-only ledger. Tokens are not metered: the org
pays its provider directly.

| Meter | Unit | Measured by | When |
|---|---|---|---|
| `vcpu_seconds` | vCPU × s | node: the box's allocated vCPUs × the time it exists (running and warm) | every 30 s and at close |
| `memory_gib_seconds` | GiB × s | node: allocated memory × time | same |
| `egress_bytes` | bytes | door: bytes from box outward (provider traffic included) | same |
| `ingress_bytes` | bytes | door: bytes toward the box | same |
| `storage_gib_hours` | GiB × h | API sweeper: bytes the org holds in the object store (snapshots, files, packages, captures) | hourly |

Install boxes are metered the same way, attributed to the org whose turn triggered the install. Allocated (not used)
CPU and memory are metered because a box reserves them; the figures are deterministic and auditable.

A usage record: `{id, org_id, meter, quantity, session_id?, response_id?, harness?, kind (turn|warm|install|storage),
start, end}`. Ids are generated where measured, so a retried report is inserted once. `GET /v1/usage` sums records by
meter, optionally grouped by `harness`, `session` or `day`, over `[start, end)`.

## 13. The extension seam

`HG_EXTENSIONS` names classes (`module:Class`) loaded at start; each implements any subset of:

```python
class Extension:
    def router(self) -> fastapi.APIRouter | None            # extra routes under the same app
    async def authenticate(self, request) -> Principal | None  # before keys and sign-ins; None passes to the next
    async def admit(self, principal, turn: Admission) -> None  # raise Refused before a turn queues
    async def observe(self, record: TraceRecord) -> None       # every §11 record as written
    async def meter(self, records: list[UsageRecord]) -> None  # every §12 batch after it is stored
    async def turn_ended(self, summary: TurnSummary) -> None   # status, reason, usage, timings
```

Hooks run in the order configured; `observe`, `meter` and `turn_ended` run after the core's own write and never fail a
turn (their errors are logged). The core exposes its services (`Orgs`, `Accounts`, `Keys`, `Harnesses`, `Usage`,
`Traces`, …) to extensions. What is built on the seam lives outside this repository and never forks the core.

## 14. API

All under `/v1`, JSON, a bearer credential: an API key or a sign-in token (§9). OpenAI-shaped endpoints match OpenAI's
request and response shapes.

### 14.1 OpenAI-shaped

| Endpoint | Notes |
|---|---|
| `POST /responses` | §4. `stream` → SSE; `background` → returns `queued`; `Idempotency-Key` honoured (same body → same response; different body → 409). |
| `GET /responses/{id}` | with `stream=true&starting_after=N`, resumes the stream |
| `POST /responses/{id}/cancel` | |
| `DELETE /responses/{id}` | refused while running |
| `GET /responses/{id}/input_items` | |
| `POST /files`, `GET /files`, `GET /files/{id}`, `GET /files/{id}/content`, `DELETE /files/{id}` | ≤512 MiB per file |
| `GET /containers` (`?user=` filters), `GET /containers/{id}`, `DELETE /containers/{id}` | a container is a session, created by its first response. `metadata` names its harness, model, turns, state, saved bytes, last response, `created_by` (the first turn's actor: `account:<id>`, `key:<id>`, or what an extension's principal names), `user` (the request's `user`, the application's own end user) and, for a fork, `forked_from` (the response it continued from) |
| `GET /containers/{id}/files`, `GET …/files/{fid}`, `GET …/files/{fid}/content` | the workspace as of the last snapshot |
| `POST /containers/{id}/files` (multipart `file`, optional `path`), `DELETE …/files/{fid}` | staged; applied before the session's next turn |
| `GET /containers/{id}/responses` | the session's responses in turn order, each as `GET /responses/{id}` returns it |
| `GET /containers/{id}/traces` (admin) | every §11 record of the session's turns, each with its `response_id`, in turn order; log bodies left out |
| `GET /models`, `GET /models/{id}` | every `harness/provider/model` the organization can send |

### 14.2 Identity

| Endpoint | Notes |
|---|---|
| `POST /auth/login` | `{email, password}` → a sign-in token (once), its expiry, the account and its memberships; 401 on failure, 423 while locked |
| `POST /auth/logout`, `GET /auth/me`, `POST /auth/password` (`{current, new}`) | the sign-in's own account |
| `POST /auth/accept` | `{token, password}`: activates an invited account, or sets the password after a reset; no credential needed |
| `GET/POST /accounts`, `GET/PATCH /accounts/{id}`, `POST /accounts/{id}/reset` (admin) | create: `{email, name, role, platform_admin?}` → the account, its invitation link (once) and whether it was mailed; patch: `role`, `status` (`disabled`), `platform_admin` (platform admins only); reset → a new invitation |
| `GET /audit` (admin) | the organization's audit rows, newest first, `?after=` paging |

### 14.3 Management

| Endpoint | Notes |
|---|---|
| `GET/POST/DELETE /keys` | create: `{label, role, run_only, expires_in_days}` (the label names the app); an admin creates any, a developer creates developer keys and sees its own; returns the key once |
| `GET/PUT/DELETE /providers/{name}` | `{api, base_url, key}`; the key is write-only, and may be left out when updating (the stored one is kept) |
| `GET/PUT /egress` | the org's egress entries |
| `GET /harnesses`, `GET /harnesses/{name}` | the installed harnesses: manifest, digest, which settings and secrets are set (never values); `GET /harnesses/{name}/files` and `/files/{path}` read the package |
| `POST /harnesses` | install or upgrade from a multipart `package` archive (the package folder as a tar, gzipped or not) |
| `PATCH /harnesses/{name}` | `settings`, `secrets` (write-only; null clears), `default_provider`, `default_model` |
| `DELETE /harnesses/{name}` | existing sessions keep running on their pinned package |
| `GET /usage` | §12; `group_by` adds `created_by` (the app's key or the person) and `user` |
| `GET /traces/{response_id}` | §11; a session's records together: `GET /containers/{id}/traces` (§14.1) |

Upgrading a harness affects new sessions only.

### 14.4 Internal (node credential; operator key or a platform admin's sign-in where noted)

`POST /internal/nodes/join`, `/heartbeat`, `/claim`, `/turns/{id}/events` (stdout bytes), `/turns/{id}/finish`,
`/turns/{id}/saved`, `/usage`, `/traces`; operator: `node-tokens`, `nodes/{id}/drain`, `health`, `nodes`, `orgs`, `orgs/{id}/accounts` (invite an organization's admin), `usage`, `config`, `versions`, `audit`, `metrics` (§11), `sessions/{id}/evict`.

## 15. Data model

| Table | Holds |
|---|---|
| `orgs` | id, name, status, capacity (JSON), egress (JSON), sealed data key |
| `accounts` | id, email, name, password hash, status, platform_admin, failures, locked_until, created, last sign-in |
| `memberships` | account, org, role |
| `invitations` | digest, account, expiry (one-time: activation and password reset) |
| `sign_ins` | id, account, digest, created, expiry, last used, client, revoked |
| `api_keys` | id, org, account (its creator), digest, role, run_only, label (the app), created, expires, last used, revoked |
| `audit` | id, org, at, actor, action, target, detail (JSON) |
| `providers` | org, name, api, base_url, sealed key |
| `packages` | digest, manifest, object key (shared across orgs; content-addressed) |
| `harnesses` | org, name, package digest, manifest, settings, sealed secrets, default provider, default model |
| `files` | org, id, created_by, filename, bytes, object key |
| `sessions` | org, id, harness name, package digest, manifest, provider, model, store, status, snapshot key, node, running turn, forked_from, created_by, user, timestamps |
| `turns` | org, id, session, seq, status, model, provider, request key, response (JSON), hg_reason, node, timestamps, cancel flag, actor (the account, key or extension principal that created it), user (the request's), snapshot key (its checkpoint) |
| `turn_events` / `live_events` | kept events / deltas (purged after the turn), by (turn, seq) |
| `turn_bindings` | sealed caller secrets of a turn, deleted at its end |
| `turn_tokens` | digest, turn, org, provider, node, expiry, revoked |
| `container_ops` | staged workspace operations |
| `nodes`, `node_tokens` | node id, pool, digest, slots, last seen, status / join tokens |
| `usage` | §12 records |
| `traces` | §11 record index (object keys) |
| `idempotency` | org, key, request digest, response |

## 16. Security summary

- gVisor boxes with no capabilities, read-only root, one unprivileged user, CPU/memory/pids/tmp limits.
- Network allow-only through the door; no DNS in the box; private addresses refused; per-turn rules.
- No secret in a box: provider keys at the gateway, org and caller secrets at the door, placeholders inside.
- Turn tokens are turn-scoped, provider-scoped, node-bound and revoked at the end.
- Sealed secrets with per-org data keys under a pluggable key wrapper; API keys, sign-ins and invitations stored as HMACs.
- People sign in as themselves: accounts with scrypt-hashed passwords, one-time invitations, no default password, lockout
  after repeated failures, expiring and revocable sign-ins; two roles checked in one place; the platform scope held by a
  flag on an account, the operator key kept for automation; every change of standing audited.
- Row-level security on every tenant table, by organization; org-prefixed object keys; signed, short-lived object links.
- Packages: content-addressed, verified by digest on every node, extracted with no links and no path escape.
- Request validation at the boundary; output validation with the SDK's types; bounded sizes everywhere (§17).
- No credential in any response, event, log, capture or error.

## 17. Limits

| Limit | Value |
|---|---|
| Request body (inline files included) | 32 MiB |
| File upload | 512 MiB |
| Package archive / unpacked | 64 MiB / 256 MiB, 5,000 files |
| `file_id` inputs per request | 64 |
| One stdout line from a package | 64 MiB |
| Captured body | 4 MiB per side |
| Settings / secrets per harness | 64 each, values ≤16 KiB |
| Org egress entries | 256 |

## 18. Repository and engineering

```
harnessgateway/
  domain/      pure rules: the Responses contract, manifests, packages, egress entries, capacity, meters, ids
  ports/       interfaces: BlobStore, KeyWrapper, SandboxRuntime
  adapters/    blob_fs, blob_s3, keys_local, keys_kms, sandbox_docker (vendor SDKs only here)
  services/    orgs, keys, providers, harnesses, files, sessions, turns, scheduler, secrets, traces, usage, sweeper
  api/         FastAPI routers: OpenAI-shaped, management, internal
  gateway/     the provider relay
  node/        the node agent, the door, the turn runtime script
  extensions.py, config.py, db.py (+ migrations), __main__.py
sandbox/       the sandbox image and hgkit (the Responses contract in Python; its validator is the platform's own)
packages/      the two shipped packages: shell (the template) and harness-package-builder (the built-in)
console/       the consoles and the chat: an application on the public API, served by one small server
deploy/        compose.yaml, install.sh, node.env.example
docs/          architecture, API, the Responses wire, packages, operations, extensions, security
tests/         unit, packages, integration (a live stack, driven by the OpenAI SDK)
```

- Python 3.12; FastAPI, psycopg 3, httpx, pydantic, the `openai` SDK (types only), `cryptography`. The door uses only
  the standard library and `cryptography`.
- One class or module per responsibility; no module that knows two things (the turn runner does not know HTTP; the API
  does not know Docker).
- Negative space: every boundary (request, manifest, settings, egress entry, path, archive, node report) is validated
  and refused with a message naming what is allowed. Invariants are asserted, not handled.
- Ports and adapters: vendor SDKs only in `adapters/`; standards (Postgres, OCI, HTTP, Responses) used directly.
- CI: `ruff`, `mypy --strict`, unit and package tests on every push; integration tests on a compose stack.

## 19. Build plan

Each step ends when its check passes.

1. **Domain**: contract (in/out validation, envelope, final Response), manifests, packages, egress entries,
   capacity, ids. → unit tests for every refusal.
2. **Persistence and ports**: schema with RLS, migrations, BlobStore (fs, s3), KeyWrapper (local, kms). → tests prove an
   org cannot read another's rows or objects.
3. **Services and API**: orgs, keys, providers, harnesses (package upload), files, responses
   (queue, stream, cancel, idempotency), containers, usage, traces, extensions. → API tests with a fake node.
4. **Gateway**: token + node credential → grant, path allowlist, key swap, streaming, capture. → tests against a stub
   provider, including a token presented without its node credential.
5. **Node, box and door**: claim, install cache, box lifecycle, seeds, inputs, run, stop, snapshot/restore, metering;
   door allowlist, private-address refusal, interception and header injection, counters. → integration: a secret
   reaches a test HTTPS server and is absent from the box (`env`, files, `/proc`), the request, the response, traces.
6. **Sandbox image and hgkit.** → hgkit unit tests; image scan.
7. **The template package**, shell, with its fixture tests; then, for each coding-agent CLI, a package written in the
   console from the template and verified live: a file-creating first turn, recall on a new sandbox, streaming,
   instructions, cancel, a provider error.
8. **Deploy**: images, compose, `install.sh`. → a clean Linux host goes from nothing to a completed turn with one
   command; a second node host joins and takes turns; two API replicas behind a load balancer stream the same turn.
9. **Docs**: architecture, API, Responses wire, packages (the builder guide), operations, extensions, security.

## 20. Not in this version

Kubernetes runtime; per-request egress narrowing (`network_policy`); request-borne skills, hooks or config files
(`environment.skills`, `environment.file_ids`); `POST /v1/containers`; the Conversations API; live steering of a
running turn; secret injection into bodies or URLs; a web console. Each is refused or ignored and reported as such, not
half-built. Next in identity: OIDC single sign-on with just-in-time accounts and group-to-role mapping; audited, time-boxed support access by the platform scope into an organization.
