# HarnessGateway: the specification

This page is everything needed to build the platform with no other context. It says what the platform is, what it
promises, how each part works, which file holds which part, and in what order to build them. Where this page and
the code disagree, this page is right and the code is a bug.

It is version **6.0.0**: the open-source, self-hosted community edition. An enterprise edition is built on it
through the seams of section 15, and never by changing it.

## 1. What it is

Agent infrastructure. OpenAI's Responses API puts a model behind one field, `model`. This platform puts everything
an agent needs behind that same field: where it runs, which agent harness CLI is its body, and which model does the
thinking.

    model = environment / harness / provider / model          dev/my-agent/deepseek/deepseek-flash

A caller builds an agent against the Responses API, unmodified, with OpenAI's own SDK. Changing any of the four
parts is changing one string: the request, the session, and the files stay the same.

In one sentence: **a Responses request becomes a turn; a turn runs one harness package in one sandbox; what the
package prints becomes the response.**

## 2. The rules every decision is held to

1. **The schema is OpenAI's, exactly.** A request is checked against the OpenAI SDK's own description of one, every
   line a package prints against the SDK's model of that item or event, and every response against the SDK's
   response type. The platform adds no field. `model` is the only field with a meaning of the platform's own.
2. **Nothing is ignored unsaid.** What cannot be done is refused, or fails the response, with words that name the
   field. Nothing is dropped silently.
3. **The platform knows no harness.** No agent CLI is named in its code, its console, or its contract. Every quirk
   of a harness lives in that harness's package. A test keeps this so.
4. **A package is `install` and `run`, and declares nothing.** It reads the request as it was sent and prints
   Responses output. There is no manifest, no helper library, and no line of the platform's own to print.
5. **The environment is the only door.** What a turn may reach and hold (image, limits, network, variables,
   secrets) is said by its environment, never by a package.
6. **The platform holds state and facts; the agent does the reading.** A session's state is two folders, saved
   after every turn. A handover gives a harness no history: it is told who worked before and where their memory
   is, and reads up by itself.
7. **KISS, DRY, SRP.** Every module has one job, said in its first line. Every line is justified. A rule lives in
   one place.

## 3. Words

| Word | Meaning |
|---|---|
| **Provider** | A model endpoint an organization defines: the API shapes it speaks, each at a base URL, and a key |
| **Environment** | Where turns run: an image, limits, a network policy, variables, secrets, a startup script, and the harnesses it carries |
| **Harness** | An agent harness CLI made runnable by a package the organization owns |
| **Package** | A folder with two programs, `install` and `run`, sent as a gzip-compressed tar archive |
| **Model line** | `environment/harness/provider/model`, the value of a request's `model`. `none/none` for a harness that calls no model |
| **Turn** | One Responses request, run. Its id is the response's id (`resp_…`) |
| **Session** | A chain of turns on the same two folders (`sess_…`) |
| **Mind** | `/mind`, which is `$HOME` in a sandbox: where a harness keeps its own memory |
| **Work** | `/work`, the working directory: what the session is working on |
| **Snapshot** | What the mind and the work held when a turn ended (`snap_…`) |
| **Stint** | Consecutive turns of a session run with the same model line |
| **Handover** | A turn whose harness is not the harness of the turn before it |
| **Sandbox** | The one hardened container a turn's `run`, or a package's `install`, runs in |
| **Grant** | What one running sandbox may reach, with its secrets, kept only while it runs |

## 4. The Responses API

`POST /v1/responses`, `GET /v1/responses/{id}`, `DELETE /v1/responses/{id}`, `POST /v1/responses/{id}/cancel`,
`GET /v1/responses/{id}/input_items`. Authentication is `Authorization: Bearer <API key>`.

### 4.1 Accepting a request

In this order; the first failure is the answer, as the OpenAI error envelope
(`{"error": {"message", "type", "param", "code"}}`) with the HTTP status of its kind (400, 401, 403, 404, 409, 413).

1. The body is a JSON object of at most 32 MiB with no NUL character.
2. `model` and `input` are present.
3. `conversation` and `prompt` are refused with words that say what to do instead: they name state OpenAI keeps,
   and there is none of it here. (A session is continued with `previous_response_id`; a prompt is sent as text.)
4. The body validates, strictly, against the SDK's `ResponseCreateParamsBase` plus `stream`: an unknown field, or
   a value of the wrong kind, is refused at any depth, and the refusal says where (`input.0.content.1.detail`).
   What is left out is not refused. The SDK's types call fields required that OpenAI's server fills in (an image's
   `detail`), so only `type` is asked of everything that has one; the rest is the package's to default. An item a
   response held is taken back as it was, though its input form lacks a field (a tool output's `status`): a caller
   that keeps the conversation sends back what it was sent.
5. `metadata` has at most 16 pairs, none with a key beginning `hg_` (the platform writes those).
6. `store: false` with `background: true` is refused: a background response is fetched later.
7. The model line parses (four parts; the last may hold slashes and is at most 200 characters; the first three
   are names: 1 to 63 characters of `a-z`, `0-9`, and `-`). Each named definition exists, and the environment
   carries the harness. `none/none` names no provider; `none` on one side only is refused.
8. `previous_response_id`, when given, names a response that exists, has ended, and left a snapshot whose bytes
   fit the named environment's `disk_mb`.
9. Every `file_id` in `input` (`input_file`, `input_image`) names an uploaded file.
10. Credentials in the request (an MCP tool's `authorization` and `headers`) are replaced by placeholders, and
    each real value is bound to that server's host and sealed beside the turn until it ends.
11. The `before_turn` seam may refuse it.

Then a turn is queued and `response.created` is its event 0.

### 4.2 Answering

| The request | The answer |
|---|---|
| Neither `stream` nor `background` | The connection is held until the turn ends; the final response object |
| `stream: true` | Server-sent events (`event: <type>` and `data: <json>`), from event 0 to the last |
| `background: true` | The response object at once, `status: "queued"`; fetch it later |
| `GET …/{id}?stream=true&starting_after=N` | The same stream again, from after event N |

The process that answers need not be the one that runs the turn: both read the turn's events from the database.

### 4.3 The response object

Built by one function and validated against the SDK's `Response` type. `id`, `object`, `created_at`,
`completed_at`, `status`, `output`, `usage`, `error`, and `incomplete_details` are the turn's. `model` is the model
line as it was sent. `tools`, `tool_choice`, `parallel_tool_calls`, and every other field the request and the
response types share are echoed from the request. `metadata` is the caller's own plus:

| Key | Value |
|---|---|
| `hg_session` | The session's id |
| `hg_reason` | Why a turn did not complete, as one word a program can act on (section 7.6) |
| `hg_code` | A failed harness's own code for the failure |

A cancelled turn has `status: "cancelled"`; its stream ends with `response.incomplete`, since the stream has no
event for cancelled.

### 4.4 `store`, `previous_response_id`, and who keeps the conversation

Both of the API's ways work, and a caller chooses per request.

| | The server keeps it | The client keeps it |
|---|---|---|
| The request | `previous_response_id` names the response to continue from | The whole conversation is in `input` |
| The session | Continued, or forked (section 5.2) | A new one for every request |
| Mind and work | Restored from that response's snapshot, then saved | Empty at the start |
| The harness | Resumes its own memory | Is given the messages by its package |

`store: false` means nothing of the turn is kept: not the response, not its events, and no snapshot. The turn runs
in a session of its own, which is removed once the answer has been given. With `previous_response_id` it starts from
that response's state and leaves that session untouched. A caller who went away is caught up with by housekeeping.

`instructions` never carry from one response to the next, as in OpenAI's API. Making sure a harness does not keep
an earlier turn's is its package's job.

### 4.5 Cancel, delete, input items

- **Cancel**: a queued turn is cancelled at once. A running one is marked and its runner told: the sandbox is
  stopped, what it left is saved, and the turn ends `cancelled`. A final one is returned as it is.
- **Delete**: a final response loses its request, output, log, and events. Its row and snapshot stay, since later
  turns descend from it. A response that is not final answers 409.
- **Input items**: the request's `input` as a list of items, each with an id.

## 5. Sessions and state

### 5.1 Two folders, saved every turn

A session's whole state is its mind and its work. After every turn that started (however it ended), both are saved
as a snapshot, unless the turn is `store: false`.

A snapshot is a **manifest**: one entry for every path, sorted. A file's entry holds its mode, size, modification
time, and the SHA-256 of each 4 MiB chunk of it; a directory's its mode; a link's its target. Links are recorded
and never followed. `mind/.cache` is never saved.

Saving is incremental twice over. A file whose size, time, and mode match the parent snapshot's entry is not read
again. A chunk is kept once per organization under its hash, with a count of the snapshots that name it. So a turn
that changed one file costs one file, and a fork costs nothing until it changes something.

A snapshot that would pass the environment's `disk_mb` is not recorded, and the turn fails as `save_failed`.

### 5.2 Continue, fork, rewind

`previous_response_id` decides, when the turn is created, under a lock on the session's row:

| `previous_response_id` | Result |
|---|---|
| Absent | A new session; the folders start empty |
| The session's latest response | The same session continues |
| Any earlier response | A new session, forked from that response: this is also rewind |
| Any response, with `store: false` | A new session forked from it, removed after the answer |

A turn's folders are restored from its parent's snapshot unless they already hold it
(`sessions.folder_snapshot_id`). A response that ended with no snapshot cannot be continued from: the refusal says
to continue from the response before it.

Every part of the model line may change from one turn to the next. The folders belong to the session, not to an
environment, so a change of environment restores them into the new one.

### 5.3 Warm folders

A driver keeps a session's folders between turns. `folder_snapshot_id` says which snapshot they hold; it is cleared
before a sandbox starts and set after a save, so folders that cannot be trusted are restored. Housekeeping removes
the folders of sessions idle longer than `HG_IDLE_S`: their snapshots hold everything.

### 5.4 Deleting

`DELETE /v1/sessions/{id}` removes the session, its turns, their events, its snapshots (each chunk is named once
less), and its folders. It is refused while a turn of it is queued or running. A chunk nothing names is removed an
hour later; asking about a chunk renews that hour, so a save in progress can rely on it.

## 6. Handover

A turn is a **handover** when its harness is not the harness of the turn before it in its chain. The platform gives
the new harness no conversation. It writes one file, `handover.txt`, into the turn's inputs: the session's handover
history, in words.

### 6.1 What a package does for the next harness

Two duties, both things only a package can know:

1. **Keep the harness's memory inside the mind.** Most harnesses keep it under `$HOME`, which is the mind. One
   that keeps it elsewhere is pointed into the mind by its package. Anything outside the mind and the work is gone
   with the sandbox.
2. **Say where it is.** `run` writes `$HG_STATE/memory`: a short text, in the package's own words, of where its
   harness keeps its record of the session, as paths in the mind, and in what form.

### 6.2 What the platform writes

The history is derived from the turn chain, so it costs no storage and follows forks by itself. Stints are
consecutive turns with the same model line, oldest first. For each harness in the chain the platform reads that
harness's note, `mind/.harnessgateway/<harness>/memory`, from the parent snapshot's manifest and chunks. A note
longer than 16 KiB is not copied: the history gives its path instead.

`handover.txt` is empty unless the turn is a handover. Then it is this, with the session's own values (each
paragraph is one line; they are wrapped here to fit the page):

```
Handover. Other agent harnesses worked on this session before this turn. To the person you are all one
assistant: carry on from where the last one stopped.

Nothing of what was said or done is repeated here. Every harness of a session shares its two folders: the mind
(/mind, which is $HOME), where each keeps its own memory of the session, and the work (/work), which holds what
the session is working on. Read what you need from them before you answer.

Who worked on the session, oldest first:

1. shell, as default/shell/none/none: resp_01A…, resp_01B…
   Its memory: /mind/.harnessgateway/shell/commands.jsonl: one JSON line for each command it ran.
2. my-agent, as dev/my-agent/deepseek/deepseek-flash: resp_01C…
   Its memory: it left no note of where; look in /mind.
3. shell, as default/shell/none/none: resp_01D…
   This is the harness you are: its memory is your own.
```

The rule for a package is one line: when `handover.txt` is not empty, give its text to the harness with the
request. A harness that returns to a session after another worked on it is handed over to as well, and sees its own
earlier stints in the history.

The words are in one function (`domain/handover.py`), so they are the same for every harness.

## 7. Harness packages

The contract is what a package author reads, and it is kept in the package that builds packages
(`packages/harness-package-builder/`): its `AGENTS.md` says how the platform runs a package, `contract/` holds the
Responses API as three schemas (a request, a response, and the stream's events), `example/` is a whole package, and
`requests/` is a session of the requests a developer sends. Building a package is the one thing done from it, so no
other sandbox is given it. This section is what the platform must do to keep it. The contract's version is **3**,
reported by `GET /v1/platform`.

### 7.1 A package

A gzip-compressed tar archive of a folder holding executable `install` and `run`, and anything else they need.
Limits: 64 MiB packed, 256 MiB unpacked, 5,000 files; plain files and folders only; no path outside the folder.
`.git`, `__pycache__`, `.DS_Store`, and `._*` are left out. A package's digest is the SHA-256 of its files (path,
executable bit, length, content), so the same files are the same package however they were archived.

### 7.2 Install

Once for each pair of package digest and image id, whichever turn asks first, held by a database advisory lock so
no two processes install the same thing. The sandbox has `/harness` (the package, writable) as its working
directory, `HOME=/tmp/home`, no provider, no variable, no secret, the environment's limits, and 15 minutes. It may
reach the package managers and what the environment allows. A failed install is recorded with its log and tried
again when next asked for. After a good install `/harness` is read-only.

### 7.3 What a turn's sandbox is given

Mounts: `/mind` and `/work` (the session's, writable), `/turn` (this turn's inputs, read-only), `/harness` (the
install, read-only), and `/etc/hg` (the trust bundle `ca.pem`, read-only).

Files in `/turn`:

| File | Content |
|---|---|
| `request.json` | The request as it was sent, with a placeholder in place of each credential |
| `previous.json` | The response `previous_response_id` names, as the API returns it; `null` when there is none or it was deleted |
| `handover.txt` | The handover history (section 6.2); empty when the turn is not a handover |
| `files/<file id>/<filename>` | Each uploaded file the input names |
| `startup.sh` | The environment's startup script, when it has one |

Variables: `HOME` (`/mind`), `HG_TURN`, `HG_MIND`, `HG_WORK`, `HG_HARNESS`, `HG_STATE`
(`/mind/.harnessgateway/<harness>`), `HG_SESSION`, `HG_RESPONSE`, `HG_ENVIRONMENT`, `HG_HARNESS_NAME`,
`HG_PROVIDER`, `HG_MODEL`, `HG_PROVIDER_KEY` (a placeholder), one `HG_PROVIDER_<API>` for each API the provider
speaks (`ANTHROPIC`, `OPENAI_RESPONSES`, `OPENAI_CHAT`, `GEMINI`) holding its base URL, the proxy and certificate
variables (section 9), then the environment's variables, then a placeholder for each of its secrets. A later one of
the same name wins. Names beginning `HG_`, `HOME`, and the proxy and certificate variables are reserved: no
definition may set them.

### 7.4 What `run` prints

One JSON object a line on standard output; standard error is the turn's log (its last 64 KiB are kept). A line is
one of three things, and nothing else:

1. **An output item, whole**: any item of the Responses API. The platform gives it an id if it has none, its place
   in the output, and its stream events (`response.output_item.added` and `.done`; for a message or a reasoning,
   also the part and text events).
2. **A stream event**: any event of the Responses stream except the response-level ones, which are the platform's
   (`response.created`, `response.queued`, `response.in_progress`, `error`). A text delta
   (`response.output_text.delta`, `response.reasoning_summary_text.delta`) that names no item goes on an item the
   platform keeps for it: begun by the first piece that has text, ended when anything else is printed.
3. **The stream's last event**, once: `response.completed`, `response.incomplete` (with
   `incomplete_details.reason`), or `response.failed` (with `error.message`, and optionally `error.code`:
   lower-case letters, digits, and `_`). Each may carry `usage` (`input_tokens`, `output_tokens`, and optional
   details). Whatever is printed after it is ignored.

The platform numbers events (`sequence_number`; the package's lines start at 2) and places items (`output_index`),
replacing whatever a line gave for them. Every line and every event is validated against the SDK's model; the first
wrong one stops the sandbox and fails the turn as `invalid_output`, naming the field. A line is at most 16 MiB. NUL
characters are replaced, since the database holds none.

If the sandbox is stopped, the text of an unfinished message or reasoning is kept as an item with
`status: "incomplete"`.

### 7.5 What a package must do with the request

Stated in the contract; held by `tests/shipped` for the packages that ship, and told to the harness package builder:

- Give the harness all of `input`: a string, or a list that may hold a whole conversation.
- Apply `instructions` to this turn only.
- **Honour or fail.** Every field of the request is either the platform's (`model`, `stream`, `stream_options`,
  `background`, `store`, `previous_response_id`, `metadata`), a label that changes nothing (`user`,
  `safety_identifier`, `prompt_cache_key`), honoured by the package, or the turn ends `response.failed` with code
  `unsupported_parameter` and a message naming the field.
- **Hand a function tool's call back to the caller**: end the turn `response.completed` with a `function_call` (or
  `custom_tool_call`) item last; the next request carries the `function_call_output`, and the harness resumes from
  its own memory. A package that cannot do this fails the turn as any unsupported field.
- Give the harness `handover.txt` when it is not empty, and write `$HG_STATE/memory`.
- Take `SIGTERM`, outlive its harness, and exit: the platform says how a stopped turn ended.

### 7.6 How a turn ends

What the platform saw from outside the sandbox outranks what the package printed. In this order:

| Seen | `status` | `hg_reason` |
|---|---|---|
| The install failed | failed | `install_failed` |
| The folders or inputs could not be put in place | failed | `restore_failed` |
| The sandbox could not start, was lost, or the platform shut down | failed | `sandbox_failed` |
| The turn was cancelled | cancelled | `cancelled` |
| Past the environment's `timeout_s` | failed | `timed_out` |
| The folders passed `disk_mb` while it ran | failed | `out_of_disk` |
| Killed for memory | failed | `out_of_memory` |
| The startup script failed (exit code 78, and `run` printed nothing) | failed | `startup_failed` |
| A printed line was invalid | failed | `invalid_output` |
| The folders could not be saved | failed | `save_failed` |
| `run` printed `response.failed` | failed | `harness_failed` (and `hg_code`) |
| `run` printed `response.incomplete` | incomplete | its reason |
| `run` printed `response.completed` and exited 0 | completed | |
| Anything else | failed | `harness_error` |

A failed response's `error.code` is the harness's code when it is one the API has, otherwise `server_error`.

## 8. The sandbox

A sandbox is described by a **plan**: plain data (name, image, command, arguments, working directory, variables,
mounts, CPUs, memory, process limit, size of `/tmp`, whether it is networked, stop grace). Building a plan is pure;
running one is a driver's whole job. A turn's plan is named for its session, so a second sandbox for one session
cannot start.

The Docker driver runs a plan as one `docker run` with every one of these, none optional: `--init` with
`TINI_KILL_PROCESS_GROUP=1`; `--read-only`; `/tmp` as a tmpfs of the environment's `tmp_mb` (a discarded volume
for an install); `--cap-drop ALL`; `--security-opt no-new-privileges`; `--user 10000:10000`; `--cpus`, `--memory`
(and the same `--memory-swap`), `--pids-limit`; the sandbox network, or `none` when the environment is closed and
the line names no provider; each mount as a sub-path of the one data volume, never the volume; `--entrypoint` set,
so the image's own never runs; and `--runtime runsc` when the daemon has gVisor (`HG_RUNTIME`: `auto`, `runsc`,
`runc`). Without gVisor, isolation is `none`, and the log says so at every start.

With a startup script, the command is a shell that runs `/turn/startup.sh` with its output sent to the log, exits
78 if it fails, and otherwise `exec`s `/harness/run`.

While a sandbox runs: its folders are measured every 2 seconds against `disk_mb`; a timer enforces `timeout_s`;
and a stop (cancel, time, disk, invalid output, shutdown) sends `SIGTERM` to everything, repeated every second
until the sandbox has ended, with 15 seconds of grace before it is killed. Processor time and memory are sampled
from the daemon about once a second and added to the organization's usage.

The driver keeps folders by name (`sessions/<id>/mind`, `sessions/<id>/work`, `turns/<id>`, `installs/<id>`,
`given`) under `folders/` of the data volume, and does the file work itself, as root: `fill` (exactly these files,
owned by the sandbox's user), `restore` (exactly what a manifest names), `save` (section 5.1), `measure`, and
`remove`.

## 9. The egress proxy

A process of its own (`harnessgateway proxy`) with no database address and no sealing key. A sandbox is on an
internal network whose only other member is the proxy, known as `egress:8080`. The proxy variables
(`HTTPS_PROXY`, `HTTP_PROXY`, and lower-case; `NO_PROXY` empty; `NODE_USE_ENV_PROXY=1`) carry the sandbox's id and
a one-time token as proxy credentials. The certificate variables (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`,
`CURL_CA_BUNDLE`, `GIT_SSL_CAINFO`, `NODE_EXTRA_CA_CERTS`, `PIP_CERT`) point at `/etc/hg/ca.pem`: the public roots
plus the installation's own authority.

For each connection (`CONNECT`, or an absolute `http://` request):

1. The credentials name a running sandbox. The proxy asks the platform for its policy over the internal API
   (`POST /internal/policy`, authenticated with `HG_PROXY_KEY`); no grant means refusal.
2. The policy decides: a host on `deny` is refused; `open` allows the rest; `limited` and `closed` allow what is on
   the allow list. The turn's provider is always allowed. A limited environment also allows the hosts of its own
   secrets and, unless it says not, the package managers. An entry is `host`, `*.suffix`, or either with `:port`;
   without a port it means 80 and 443.
3. The host's addresses are resolved. Private, loopback, link-local, multicast, and reserved addresses are never
   connected to, unless the operator names the host in `HG_PRIVATE_HOSTS`.
4. A host no secret is bound to is relayed unread. A host a secret is bound to is answered with a certificate of
   the installation's authority, so the request's head can be read: each placeholder in its headers and query
   becomes the secret (never in the body), a secret with a `header` is set as that header, and the request goes on
   over a connection the proxy verified itself. One request is read per connection.
5. A refusal is a `403` with a line a person reading an agent's log can act on.

Bytes each organization's sandboxes sent and received are reported to the platform (`POST /internal/passed`) and
added to its usage. `GET /internal/authority` gives the proxy the authority's certificate and key.

**No secret enters a sandbox.** A provider's key, an environment's secrets, and a request's MCP credentials are
placeholders (`hgs_…`) there. Secrets are sealed at rest with AES-256-GCM under `HG_SECRET_KEY`, each bound to the
place it belongs, and opened only into a grant for a sandbox that is about to run.

## 10. The three definitions

One table, one shape (a name, a definition, and secrets sealed beside it), one set of verbs:
`GET /v1/{kind}`, `GET`, `PUT`, and `DELETE /v1/{kind}/{name}`. `PUT` makes or replaces. Each is validated in full
when written, strictly, with unknown fields refused. A secret is write-only: not sent keeps its value, `null`
removes it, and the API shows only which secrets have a value.

| Kind | Fields |
|---|---|
| `providers` | `apis`: a map from `anthropic`, `openai-responses`, `openai-chat`, or `gemini` to a base URL, at least one. Secret: `key` |
| `environments` | `image`; `harnesses` (names); `cpus` (0.25 to 64, default 2); `memory_mb` (256 to 262,144, default 4,096); `pids` (64 to 65,536, default 512); `tmp_mb` (default 1,024); `disk_mb` (default 10,240); `timeout_s` (1 to 86,400, default 1,800); `network` (`mode`: `closed`, `limited`, or `open`; `allow`; `deny`; `package_managers`); `variables`; `secrets` (each: `hosts`, optional `header` and `prefix`, and its `value`); `scripts.startup` |
| `harnesses` | `PUT` takes the package archive as the body, or `{"session", "path"}` to take it from a folder of a session's saved work. `GET /v1/harnesses/{name}/package` returns the archive |

Deleting a harness takes it off every environment that carries it.

What ships: `shell` (runs the input as a command), `harness-package-builder` (builds every other package, and
carries the contract it builds them to), and `default` (image `node:22`, open network, carrying both).

The packages are the platform's. Every start puts each at the running version's, for every organization, and leaves
one that is unchanged as it is. `PUT` and `DELETE` on their names are refused: a changed copy is added under another
name, and is then the organization's own. So an update of the platform updates the builder, and with it the schemas
it builds to. The environment is what an organization starts with: given once, at setup, and its own from then on.

## 11. The rest of the API

| Routes | What |
|---|---|
| `POST` `GET /v1/files`, `GET` `DELETE /v1/files/{id}`, `GET /v1/files/{id}/content` | OpenAI's Files API: what `input_file` and `input_image` name |
| `GET /v1/containers/{id}`, `…/files`, `…/files/{file}`, `…/files/{file}/content` | OpenAI's Containers API, read-only: a session's work as its latest snapshot holds it. A container's id is its session's id under the prefix `cntr_`; a file's id is made from its path |
| `POST /v1/containers/{id}/view` | A link a browser opens by itself for an hour: a token, signed with the installation's key, naming one session. Files are served through it sandboxed (a `Content-Security-Policy` with `sandbox`), with byte ranges |
| `GET /v1/sessions`, `GET` `DELETE /v1/sessions/{id}`, `GET /v1/sessions/{id}/turns` | Every session, as the requests and responses it is made of, with each turn's log |
| `GET /v1/usage?start=&end=` | Processor time, memory, network bytes, and storage, for the lifetime or a range of days |
| `POST` `GET /v1/keys`, `DELETE /v1/keys/{id}` | API keys: shown once, kept as a hash |
| `GET /v1/platform` | `version`, `contract`, `sdk`, `driver`, `isolation` |
| `GET /healthz`, `GET /readyz` | Whether it is up, and whether it can reach its database and its driver |

Lists are in the shape OpenAI's SDK pages through (`object`, `data`, `first_id`, `last_id`, `has_more`), with
`limit` (1 to 100) and `after`.

## 12. The console

Plain JavaScript modules in `console/`, served as they are at `/console` (`HG_CONSOLE=off` serves the API without
it). It is a client of the public API, with the key someone signs in with: it has no route of its own. Pages: chat,
providers, environments, harnesses, sessions, and usage. The chat is an application built on the Responses API
alone: streamed replies with thinking and steps, files given with a message, a message that interrupts a running
turn, rewind and branch (the API's fork), and a panel that previews what the agent made through a view link.

A harness begun in the console starts as a working package that keeps the contract (`pages/harnesses/template.js`),
so it runs before anything in it is changed.

An edition adds a folder of files served at the same paths; its `edition.js` names the pages it adds.

## 13. Storage

PostgreSQL holds every row; a blob store (a directory, or an edition's object store) holds bytes: chunks,
manifests, package archives, and uploaded files. Every row of an organization carries `org_id`, every blob key
begins with it, and every transaction on an organization's rows sets `hg.org_id`. The schema is applied from
numbered files, all or nothing; a released file is never edited.

| Table | Holds |
|---|---|
| `orgs`, `keys` | The organization, and its API keys by hash |
| `resources` | The three definitions: `spec`, and `sealed` secrets |
| `packages`, `installs` | Package archives by digest; each install for an image, with its status and log |
| `files` | Uploaded files |
| `sessions` | `head_turn_id`, `forked_from_turn_id`, `folder_snapshot_id`, `folder_used_at` |
| `turns` | The queue and the record: session, parent, number, status, reason, code, message, the model line's four parts, `request`, sealed `credentials`, `output`, `usage`, `snapshot_id`, `store`, `cancel_requested`, the runner and its heartbeat, `log`, times, `deleted_at` |
| `events` | Each turn's stream, numbered, so it can be read again from any point |
| `chunks`, `snapshots` | Chunks by hash with their reference counts; snapshots with their bytes and file counts |
| `usage` | One row per organization per day |
| `grants` | What each running sandbox may reach, sealed, deleted when it ends |
| `platform`, `rounds`, `migrations` | The certificate authority; when each timed job last ran; which schema files have been applied |

## 14. Processes

One image runs twice (`serve`: the public API, the internal API, the runner, and housekeeping; `proxy`), beside
PostgreSQL. Nothing a turn needs lives in one process's memory, so the same code runs as several.

- **The queue** is `turns`: a runner claims the longest-waiting one with `FOR UPDATE SKIP LOCKED`, up to
  `HG_MAX_TURNS` at once, and is woken by `NOTIFY`.
- **Liveness** is a heartbeat every 5 seconds. A turn whose runner has been silent for 30 is ended by any runner
  as `sandbox_failed`, what is left of its sandbox is discarded, and its folders are no longer trusted.
- **The stream** is `events` plus `NOTIFY`.
- **Housekeeping** runs once a minute in whichever process takes the round: storage is added to usage; idle
  sessions' folders are removed; `store: false` turns whose caller went away are forgotten after 10 minutes;
  installs of packages no harness is made from are removed; unnamed chunks past their hour are collected; and
  grants older than 25 hours are dropped.
- **Shutdown** stops running turns, saves them, and ends them.

Settings are read from the environment once, at start, and a wrong one stops the process then:
`HG_DATABASE_URL`, `HG_SECRET_KEY` (32 bytes, base64), `HG_PROXY_KEY`, `HG_DATA` (`/data`), `HG_BLOBS`,
`HG_NAMESPACE` (`hg`), `HG_DRIVER` (`docker`), `HG_RUNTIME`, `HG_PORT` (8000), `HG_INTERNAL_PORT` (8001),
`HG_MAX_TURNS` (8), `HG_IDLE_S` (3600), `HG_EXTENSIONS`, `HG_CONSOLE`; for the proxy, `HG_PROXY_KEY`,
`HG_INTERNAL_URL`, `HG_PROXY_PORT` (8080), `HG_PRIVATE_HOSTS`.

`install.sh` writes `.env` with new secrets, builds the image, starts the database, runs `harnessgateway init`
(which gives the defaults and prints the first API key, once), and starts `serve` and `proxy`.

## 15. Editions

An edition is a Python package named in `HG_EXTENSIONS` as `module:Class`. It subclasses `Extension` and overrides
the seams it uses; each seam's default is the core's own behaviour.

| Seam | The core | An edition |
|---|---|---|
| `authenticate(request)` | An API key | Single sign-on; matching a caller to an organization |
| `authorize(who, action)` | Whoever is authenticated may do everything | Roles |
| `drivers()` | Docker on one host | A cluster |
| `blob_stores()` | A directory | Object storage |
| `sealer(settings)` | AES-256-GCM under `HG_SECRET_KEY` | A key service |
| `before_turn(who, request)` | Nothing | Quotas and policy |
| `on_event(name, about)` | Nothing | An audit log; metering |
| `routes(api)`, `migrations()`, `console()` | None | Its own API, tables, and pages |

Every route names what it does as `noun.read` or `noun.write`; that is what `authorize` is asked. `on_event` hears
`turn.finished` and every change to a definition, a key, or a session, never with a secret.

## 16. Where each part is

Dependencies point one way: `api` and `runner` use `store`, `driver`, and `domain`; `store` and `driver` use
`domain`; `domain` uses nothing.

```
src/harnessgateway/
  cli.py  serve.py  settings.py  platform.py  extensions.py  errors.py  ids.py  shipped.py  defaults.py
  domain/       every rule, pure: no I/O
    line.py       the model line                       request.py    what a request must be; its credentials and files
    resolve.py    a line and its definitions           response.py   the response object and the response-level events
    events.py     what `run` prints, into events       outcome.py    how a turn ended
    handover.py   stints, and the handover's words     snapshot.py   manifests and entries
    sandbox.py    plans, variables, and policies       contract.py   the sandbox's fixed facts, and the contract's version
    resources.py  the three definitions                package.py    a package archive, read and written
    egress.py     host entries and the decision        sealing.py    secrets at rest
    schema.py     the Responses API as JSON Schemas    view.py       view links        principal.py  who is calling
  store/        PostgreSQL and blobs: one module for each group of tables
  driver/       base.py (the seam), docker.py, folders.py, meter.py
  runner/       runner.py (the queue), turn.py (one turn's steps), installer.py, housekeeping.py
  proxy/        server.py, tunnel.py, http.py, authority.py, control.py
  api/          app.py, http.py, responses.py, files.py, containers.py, views.py, resources.py, sessions.py,
                organization.py, console.py
packages/       shell, and harness-package-builder, which holds contract/ (three schemas generated from the pinned
                SDK), example/ (a whole package), and requests/ (a session of a developer's requests)
environments/   default.json
console/        the pages
tests/          unit, shipped, integration, live
```

`shipped.py` is the only place the server finds `console/`, `packages/`, `environments/`, and the contract.

## 17. The order to build it

Each step is complete, with its tests, before the next.

1. `errors`, `ids`, `settings`; then `domain/`, with `tests/unit`. Start with `line`, `request`, `events`, and
   `outcome`: they are the Responses API.
2. The contract, in `packages/harness-package-builder/contract/`: `harnessgateway schema` writes the three schemas
   from the pin; then the builder's `AGENTS.md`, its example package, and its requests.
3. `store/`: the schema file, `db`, then one module per group of tables.
4. `driver/`: `folders` (with its tests), then `docker`.
5. `runner/turn.py`, with a stand-in driver whose folders are real and whose sandbox prints what a test tells it
   to; then `installer`, `runner`, `housekeeping`.
6. `api/`, `platform`, `serve`, `cli`.
7. `proxy/`.
8. `packages/shell`, then the rest of `packages/harness-package-builder`; `environments/default.json`;
   `tests/shipped`.
9. `console/`.
10. `Dockerfile`, `compose.yaml`, `install.sh`; `tests/live` against a running installation.

## 18. Checks

```sh
uv run ruff check . && uv run mypy          # lint, and strict types
uv run pytest tests/unit tests/shipped      # the rules, and what ships held to the contract
tests/run.sh                                # everything, on a real database (the installation must be up)
HG_URL=… HG_KEY=hg_… python3 tests/live/turns.py     # a running installation, in real containers
HG_URL=… HG_KEY=hg_… node tests/live/console.cjs     # every page of the console, in a real browser
```

What must hold, and which test holds it:

| Promise | Test |
|---|---|
| A request the SDK would not send is refused, naming the field | `unit/test_request.py` |
| Every printed line becomes valid events and a valid response | `unit/test_events.py` |
| A handover's words name every stint and each harness's memory | `unit/test_handover.py` |
| A turn restores, runs, saves, and concludes; fork and rewind return to a response's state | `integration/test_turns.py` |
| `store: false` leaves no response, no snapshot, and no session | `integration/test_turns.py` |
| A harness is told where the one before it keeps its memory | `integration/test_turns.py`, `live/turns.py` |
| The platform names no agent CLI | `unit/test_agnostic.py` |
| What ships keeps the contract, and what the harness package builder is told is what the platform has | `shipped/test_packages.py` |

## 19. What 6.0.0 changes from 5.2.1

This version was built from `harnessgateway-core` 5.2.1. Everything this page does not list below is kept as it
was: the API's routes, the definitions, the sandbox, the egress proxy, the Files and Containers APIs, view links,
usage, keys, the console, the seams, and the deployment.

| | 5.2.1 | 6.0.0 |
|---|---|---|
| The contract | 2 | 3 |
| A handover | `history.json` (the missed turns' input and output) and a one-line `handover.txt`; what a package did with them was its choice | No history. `handover.txt` is the handover history, with where each harness keeps its memory; giving it to the harness is a rule |
| A harness's memory | Not mentioned | A package keeps it inside the mind, and says where in `$HG_STATE/memory` |
| A field a harness cannot honour | Left alone, and noted in the turn's log | The turn fails, naming the field |
| `store: false` | The turn ran in its parent's session, was saved, then deleted | It runs in a session of its own, is never saved, and leaves its parent's session untouched |
| The contract and its schemas | Given to every sandbox, at `$HG_CONTRACT` | Carried by the builder's package, and by nothing else |
| The packages that ship | `shell` and `hg-fde`: given once, then the organization's | `shell` and `harness-package-builder`: the platform's, put at the running version's on every start, and not changed in place |

## 20. Decided, and still open

Decided here, and the founder's to overrule:

- `conversation` and `prompt` stay refused. Supporting `conversation` would mean serving OpenAI's Conversations
  API, with a session as a conversation.
- An unsupported field fails inside the turn, as a failed response, because only a package knows what its harness
  can do and a package declares nothing. OpenAI answers the same mistake with HTTP 400 before any work starts.
- The handover's words are the platform's, in one place, and are prompt content: they are the founder's to change.

Open:

- A pointer-only handover costs the new harness steps to read up, every time the harness changes. A caller who
  wants a change to cost nothing keeps the conversation itself and sends it in `input`.
- A harness's own record is in a format the platform does not control, and the reading harness has to cope with
  it.
