# HarnessGateway: the specification

This page is everything needed to build the platform with no other context. It says what the platform is, what it
promises, how each part works, which file holds which part, and in what order to build them. Where this page and
the code disagree, this page is right and the code is a bug.

It is version **1.2.0** of the open-source, self-hosted community edition, whose installations are upgraded in
place since 1.0.0 (section 14.3). It is also the bottom of three layers, each a container image built on the one
below it and each a package the core loads (section 15):

| Layer | Repository | What it is |
|---|---|---|
| The core | `harnessgateway` (public, Apache-2.0) | This page. Complete by itself: one person, one organization, one host, API keys |
| The enterprise edition | `harnessgateway-enterprise` (private) | The core, and one extension: identity, audit, security, deployment at scale. Self-hosted, licensed |
| The hosted service | `harnessgateway-cloud` (private) | The enterprise edition, and one more extension: many organizations, plans, billing. Run by HarnessGateway |

The core knows none of them. Everything they do is through the seams of section 15, and never by changing the core.

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
8. **What must not happen is written down, and checked.** A program is also the things it refuses. Every boundary
   refuses what it does not accept, with words that name it. Every invariant is an assertion, a type, or a
   constraint of the database, so a state that must not exist cannot be stored. Every promise of section 18 has a
   test that tries to break it. Nothing fails silently: what is caught is handled or logged, and never dropped.
9. **A test first.** A change starts with a test that fails without it, at the lowest level that can show it:
   **unit** for a rule (no I/O), **integration** for a store, the runner, the API, or a seam (a real database, a
   stand-in sandbox), **end to end** for what only a real installation shows (the released image, real containers,
   a real browser). A change is done when all three levels pass.
10. **The core knows no edition.** An edition adds to the core at a seam. A change that makes the core name an
    edition, or behave differently because one is loaded, is the wrong change: the seam is what is missing.

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
8. `previous_response_id`, when given, names a response that exists, in a session the caller may change (their
   own, or anyone's: section 5.5), that has ended, and left a snapshot whose bytes fit the named environment's
   `disk_mb`.
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

### 5.5 Whose a session is

A session is its owner's: whoever sent the request that started it. A fork is its forker's. The owner is the
`principal` of the session's first turn, so it is kept in one place, and a session object names it as `owner`.

Its owner reaches a session as they always did. Anyone else who reaches one is reaching everyone's, and
`authorize` (section 15.2) is asked about that as well as about the route:

| Action | Asked when someone who is not the owner |
|---|---|
| `sessions.read_all` | Reads the session, its turns, one of its responses or its input items, its container or a file of it, makes a view link to it, or takes a package from its work |
| `sessions.write_all` | Continues or forks from one of its responses, cancels or deletes one, or deletes the session |

`GET /v1/sessions` lists every session of the organization for a caller who may read all of them, and the caller's
own for one who may not. What someone continues in another's session stays that session's owner's; what they fork
from it is their own.

The core has one person, its owner, and every key acts as them (section 11.1): so with the core alone every session
is the owner's, whichever key started it. An edition with people decides who else may reach one: there, a session is
private to whoever started it unless their role says otherwise. The two actions are named so that a role's `*.read` or `*.write`
does not match them: reading everything of an organization's is not reading everyone's conversations.

So that a client knows which `owner` is itself, every answer to an authenticated caller names that caller in the
header `hg-subject`. It is a header, as OpenAI's own API names the caller's organization in one: nothing is added
to a response's fields.

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

Every connection a sandbox made, or was refused, is reported to the platform (`POST /internal/passed`, in batches
every 2 seconds): its organization, its sandbox, the host and port, why it was refused if it was, and the bytes
sent and received. The platform adds the bytes to the organization's usage and tells the extensions of each
connection (`egress.connection`, section 15.4). What cannot be reported is kept for the next batch, up to 10,000
connections; past that the oldest are dropped, their bytes still counted, and the proxy's log says so.
`GET /internal/authority` gives the proxy the authority's certificate and key.

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
| `POST /v1/containers/{id}/view` | A link a browser opens by itself for an hour: a token naming one session, signed with a key of the installation's own (made once, kept sealed in the `platform` table). Files are served through it sandboxed (a `Content-Security-Policy` with `sandbox`), with byte ranges |
| `GET /v1/sessions`, `GET` `DELETE /v1/sessions/{id}`, `GET /v1/sessions/{id}/turns` | Every session the caller may read (section 5.5), each with its `owner`, as the requests and responses it is made of, with each turn's log and what its sandbox used (`used`: `seconds` it ran, `cpus` and `memory_mb` it was allowed, `cpu_seconds` and `memory_byte_seconds` it measured; `null` for a turn whose sandbox never started) |
| `GET /v1/usage?start=&end=` | Processor time, memory, network bytes, and storage, for the lifetime or a range of days |
| `POST` `GET /v1/keys`, `DELETE /v1/keys/{id}` | API keys (section 11.1): shown once, kept as a hash |
| `GET /v1/permissions` | What the caller may do (section 11.1) |
| `GET /v1/platform` | `version`, `contract`, `sdk`, `driver`, `isolation`, and `extensions`: the name and version of each one loaded, in order (empty for the core alone) |
| `GET /healthz`, `GET /readyz` | Whether it is up, and whether it can reach its database and its driver |

Lists are in the shape OpenAI's SDK pages through (`object`, `data`, `first_id`, `last_id`, `has_more`), with
`limit` (1 to 100) and `after`.

### 11.1 API keys, and who a key is

A key acts as someone, and never does more than they may.

- **Who.** The core has one person: the installation's owner (`owner`). A key acts as whoever made it, so in the
  core every key is the owner's, and so is one made on the host (`harnessgateway init`). An edition with people
  makes that a person, or an application's account: a key's `subject` is the subject of the caller who made it.
- **Limits.** `POST /v1/keys` takes `name`, and optionally `expires_at` (a time, from which the key is refused as
  an unknown one is) and `scopes`: patterns `noun.verb` in which either part may be `*` (`responses.*`, `*.read`),
  over the actions routes name (section 15.2). A key with scopes does only what one of them matches, of what its
  subject may do: that is checked on every request, before any extension is asked. Asking what the caller may do
  (`me.read`) is never limited. A key that is limited, by scopes or by a time, makes no key: one it made could
  reach further, or last longer, than itself.
- **Whose.** A caller lists and revokes their own keys. Anyone else's are asked about at `authorize` as
  `keys.read_all` and `keys.write_all`, as another's sessions are (section 5.5). The core alone allows it, and
  has nobody else.
- The key itself is shown once, when it is made; only its hash is kept. A key object is `id`, `name`, `subject`,
  `scopes`, `created_at`, `expires_at`, `last_used_at`, and `revoked_at`.

`GET /v1/permissions` (`me.read`) answers what the caller may do: `subject`, `key` (the id of the key they came
with, when they came with one), and `allows`: every action of every route this installation has, an extension's
included, that this caller would not be refused. It is what a client draws itself from: the console lists only the
pages whose action is there.

## 12. The console

Plain JavaScript modules in `console/`, served as they are at `/console` (`HG_CONSOLE=off` serves the API without
it). It is a client of the public API, with the key someone signs in with: it has no route of its own. Pages: chat,
providers, environments, harnesses, sessions, usage, and API keys. The sidebar lists the pages the person signed in
may use (`GET /v1/permissions`, section 11.1): a page is listed when its action is allowed, which for the three
definitions is writing them, since those pages are where they are built. The chat is an application built on the Responses API
alone: streamed replies with thinking and steps, files given with a message, a message that interrupts a running
turn, rewind and branch (the API's fork), and a panel that previews what the agent made through a view link.

A harness begun in the console starts as a working package that keeps the contract (`pages/harnesses/template.js`),
so it runs before anything in it is changed.

Signing in is giving the console a credential, which it keeps in the tab (`sessionStorage`) and sends as
`Authorization: Bearer …` with every call. The core's way is typing an API key. A credential may also arrive in the
address, as `#credential=<token>`: the console takes it, removes it from the address, and signs in with it. That is
how a sign-in that happens elsewhere (an edition's single sign-on) comes back.

The chat's list of conversations is kept in the browser, and is the person's who is signed in: it is kept under the
`hg-subject` the platform's answers name (section 5.5). In a browser several people use, each sees their own chats;
and someone who may read every session still sees, in the chat, only what they began there. The Sessions page lists
what `GET /v1/sessions` returns, each with who started it.

Editions add to the console and replace nothing of it (section 15.5): each loaded extension's folder is served
under `/console/x/<name>/`, and its `edition.js` names the pages and the ways to sign in that it adds. The console
learns which extensions there are from `GET /console/extensions.json`, which needs no key. Pages and sign-ins of
every extension are shown, in the order the extensions were named.

## 13. Storage

PostgreSQL holds every row; a blob store (a directory, or an edition's object store) holds bytes: chunks,
manifests, package archives, and uploaded files. Every row of an organization carries `org_id`, every blob key
begins with it, and every transaction on an organization's rows sets `hg.org_id`.

The schema is numbered files, applied in order, all or nothing, under an advisory lock so one process changes it at
a time. A released file is never edited: a change is a new file. Each applied file is recorded with its **owner**
(`core`, or the name of the extension whose folder it is in), so two layers may each have a `0001_…` file. Before
applying anything the platform checks the record against the files it has: when the record names a file of a loaded
owner that this version does not have, the database was changed by a newer version, and the process refuses to
start, saying so. A record of an owner that is not loaded is left alone: an installation may go back from an
edition to the core, and the edition's tables stay as they were.

| Table | Holds |
|---|---|
| `orgs`, `keys` | The organization, and its API keys by hash, each with who it acts as, its scopes, and when it ends |
| `resources` | The three definitions: `spec`, and `sealed` secrets |
| `packages`, `installs` | Package archives by digest; each install for an image, with its status and log |
| `files` | Uploaded files |
| `sessions` | `head_turn_id`, `forked_from_turn_id`, `folder_snapshot_id`, `folder_used_at` |
| `turns` | The queue and the record: session, parent, number, status, reason, code, message, the model line's four parts, `request`, sealed `credentials`, `output`, `usage` (the tokens its harness reported), `used` (what its sandbox used), `snapshot_id`, `store`, `cancel_requested`, `principal` (who sent it), the runner and its heartbeat, `log`, times, `deleted_at` |
| `events` | Each turn's stream, numbered, so it can be read again from any point |
| `chunks`, `snapshots` | Chunks by hash with their reference counts; snapshots with their bytes and file counts |
| `usage` | One row per organization per day |
| `grants` | What each running sandbox may reach, sealed, deleted when it ends |
| `platform`, `rounds`, `migrations` | What the installation makes once and keeps sealed (the certificate authority, the key view links are signed with); when each timed job last ran; which schema files have been applied, each with its owner |

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

### 14.1 Settings

Settings are read from the environment once, at start, and a wrong one stops the process then, naming it:
`HG_DATABASE_URL`, `HG_SECRET_KEY` (32 bytes, base64; required unless an extension supplies the sealer),
`HG_PROXY_KEY`, `HG_DATA` (`/data`), `HG_BLOBS`, `HG_NAMESPACE` (`hg`), `HG_DRIVER` (`docker`), `HG_RUNTIME`,
`HG_PORT` (8000), `HG_INTERNAL_PORT` (8001), `HG_MAX_TURNS` (8), `HG_IDLE_S` (3600), `HG_EXTENSIONS`, `HG_CONSOLE`;
for the proxy, `HG_PROXY_KEY`, `HG_INTERNAL_URL`, `HG_PROXY_PORT` (8080), `HG_PRIVATE_HOSTS`.

An image says what it is with two variables set when it is built: `HG_IMAGE`, its own name and tag (the core's
release is `ghcr.io/harnessgateway/harnessgateway:<version>`), and `HG_EXTENSIONS`, empty in the core's image. An
edition's image sets both to its own. Nothing outside an image has to name its extensions.

### 14.2 Installing: pull and run

The image is the whole distribution. A host needs Docker with Compose v2, and nothing from the repository:

```sh
image=ghcr.io/harnessgateway/harnessgateway:1.2.0
docker run --rm "$image" harnessgateway compose > compose.yaml    # how it is started, for this version
docker run --rm "$image" harnessgateway secrets > .env            # once: the installation's secrets. Keep it
docker compose up --detach --wait                                  # the database, the platform, the egress proxy
docker compose exec serve harnessgateway init                      # the defaults, and the first API key, shown once
```

- `harnessgateway compose` prints the Compose file this version is run with, naming this image (`HG_IMAGE`). It
  builds nothing. `serve` is given everything in `.env` (`env_file`), so an edition's settings reach it with no
  change to the file; the proxy is given only its own three settings, never the database's address or a key.
  `serve` has a health check on `/readyz`, so `--wait` returns when the platform can work.
- `harnessgateway secrets` prints a new `.env`: `HG_SECRET_KEY`, `HG_PROXY_KEY`, `HG_DB_PASSWORD`, and the
  defaults a person may change (`HG_PORT=8000`, `HG_BIND=127.0.0.1`, `HG_NAMESPACE=hg`).
- `harnessgateway init` gives the organization its defaults and prints its first API key, once. Run again it says
  the installation is set up; `--new-key` makes another key, which is the way back in when every key is lost.

There is no installer script. The repository's `compose.dev.yaml` is for whoever works on the platform: it builds
the image from the checkout (`harnessgateway:dev`) and is used with the same `.env`.

### 14.3 Updating

An installation is updated in place, from any 1.x to any later 1.x, with its data:

```sh
docker run --rm "$new" harnessgateway compose > compose.yaml && docker compose up --detach --wait
```

`serve` applies the schema files it has that the database has not (section 13) before it serves anything, and puts
the packages that ship at its version's (section 10). The version that was running is stopped before the new one
starts, so an old version never runs on a new schema; and one started by mistake on a newer database refuses to
start. Moving to an edition is the same two commands with the edition's image, and its settings added to `.env`.
What an update asks of whoever runs the platform, beyond those two commands, is said in that version's entry in
`CHANGELOG.md`; a version that asks anything is a new major version.

## 15. Editions

An edition is the core plus one or more **extensions**. An extension is a Python package installed into the
core's image, with one class that subclasses `Extension` and overrides the seams it uses; each seam's default is the
core's own behaviour. `HG_EXTENSIONS` names the classes as `module:Class`, comma-separated, **in order**: the
enterprise edition's image sets `hgenterprise:Enterprise`, and the hosted service's sets
`hgenterprise:Enterprise,hgcloud:Cloud`. So editions are a chain, and every seam says what several extensions
together mean (section 15.2). A later extension may import an earlier one's public modules, as each imports the
core's; none changes a layer below it.

### 15.1 What an extension may rely on

- **`harnessgateway.edition`** is the whole of the core that an extension imports: the `Extension` class, the
  platform and its stores, the types the seams take and return, the refusals, the helpers every route uses, and
  `defaults.give`. Nothing else in the core is promised to stay where it is. A test in each layer fails when an
  extension imports any other module of the core.
- **`harnessgateway.testing`** is what an extension's tests are written with: the stand-in driver (real folders, a
  sandbox that prints what a test tells it to), the smallest package, a platform on a test database, and running
  the next waiting turn as a runner does.
- **The console's `lib/` and `components/`** are what an extension's pages import: among them `components/keys.js`,
  the dialog that makes an API key, the one time it is shown, and the rows of a list of keys.
- **The core's own tests pass with any extension loaded.** `HG_TEST_EXTENSIONS` names the extensions the core's
  integration tests load. Every edition runs the core's suite this way in its own checks: an extension that
  changes what the core does fails there.
- An extension has a `name` (a resource name: `enterprise`, `cloud`) and a `version`. Its name owns its schema
  files (section 13), its console folder (`/console/x/<name>/`), and its line in `GET /v1/platform`. Every table
  it makes is named `<name>_…` and every row of an organization's carries `org_id`.
- An extension is made with no arguments, before the database is open, when `serve` or `init` starts; the proxy
  loads none. A wrong setting of its own raises `SettingsError` there, and the process stops, as for the core's.
  One whose class is not an `Extension`, has no name, or has a name another has (or `core`), is refused the same
  way.
- **An extension that stands on another finds it by name**: `platform.extensions.named("enterprise")` is the
  loaded extension of that name, or nothing. The one below is named first in `HG_EXTENSIONS`, so it is asked
  first at every seam, and has been started when the one above is.
- **Nothing an extension needs lives in one process's memory**, as nothing of the core's does: several `serve`
  processes run the same extensions.

### 15.2 The seams

| Seam | The core alone | With several extensions |
|---|---|---|
| `drivers()` | Docker on one host (`HG_DRIVER=docker`) | All offered, by name. A name offered twice is refused at start |
| `blob_stores()` | A directory (`HG_BLOBS=/data/blobs`) | All offered, by URL scheme (`s3://…`). A scheme offered twice is refused at start |
| `sealer(settings)` | AES-256-GCM under `HG_SECRET_KEY` | The first that gives one. With one, `HG_SECRET_KEY` is not needed |
| `migrations()` | None | Each extension's one folder, applied after the core's, in order, recorded under its name |
| `start(platform)` | Nothing | Each, in order, once the schema is up to date and before anything is served, in `serve` and in `init`. It is where an extension is handed the platform. One that raises stops the process |
| `authenticate(request)` | An API key | The first that answers; the API key is asked last |
| `authorize(who, action)` | Whoever is authenticated may do everything their key is not limited away from | Every one is asked, once the key's own scopes allow the action; any may refuse (`Forbidden`) |
| `before_turn(who, request)` | Nothing | Every one is asked; any may refuse (`Refused`) |
| `egress(org_id, policy)` | The environment's policy, as it is | Each is given what the one before returned, and returns it narrowed: more denied, never more allowed |
| `on_change(conn, change)` | Nothing | Each, in order, inside the change's own transaction (section 15.3) |
| `on_event(name, about)` | Nothing | Each is told separately; one that fails is logged and never fails another, or what it was told about (section 15.4) |
| `housekeep(platform, elapsed_s)` | Nothing | Each, separately, at the end of every housekeeping round (once a minute, in one process); one that fails is logged |
| `routes(api)` | None | All added after the core's. `api.route(path, method, action, handler)` makes one that is authenticated and authorized as the core's own are. A path and method that already has a route is refused at start |
| `console()` | None | Each extension's folder, under `/console/x/<name>/` (section 15.5) |

Every route names what it does as `noun.read` or `noun.write` (`responses.write`, `providers.read`, `keys.write`,
…): that string is what `authorize` is asked about. Reaching a session that is not the caller's own asks a second one,
`sessions.read_all` or `sessions.write_all` (section 5.5), and another's key `keys.read_all` or `keys.write_all`
(section 11.1). `who` is a `Principal`: the organization whose data the request may touch, a `subject` (who is
calling: `owner` in the core, or whoever an extension names), and, when they came with an API key, that `key` and
its `scopes`. The rule of a pattern (`permission.allows`, `permission.within`) is the core's, and an edition's
roles are made of the same patterns.

### 15.3 Changes: a change and its record commit together

Every change someone makes through the API, and every one the platform makes for itself, is told to the extensions
**inside the transaction that makes it**, with that transaction's connection. An extension that keeps a record (an
audit log) writes it with that connection, so the change and its record are both kept or neither is. An extension
that raises undoes the change, and the request is refused with its words: there is no change without its record.

A `Change` is a `name`, the `org_id`, `by` (the caller's `subject`; `host` for what `harnessgateway init` does;
`platform` for what the platform does by itself when it starts), a `target`, and `about` (more, never a secret).

| `name` | `target` | `about` |
|---|---|---|
| `providers.applied`, `environments.applied`, `harnesses.applied` | The definition's name | |
| `providers.deleted`, `environments.deleted`, `harnesses.deleted` | The definition's name | |
| `keys.created`, `keys.revoked` | The key's id | When created: its `name`, `subject`, `scopes`, and `expires_at` |
| `responses.created` | The response's id | `session`, `model` (the line as sent) |
| `responses.cancelled`, `responses.deleted` | The response's id | |
| `sessions.deleted` | The session's id | |
| `files.created`, `files.deleted` | The file's id | `filename` and `bytes`, when created |

The stores make the changes, so the stores tell of them: each method that changes something takes who is making
the change. A turn that was not to be stored leaves nothing, and its removal is no change of anyone's: it is not
told.

### 15.4 Events: what happened by itself

What nobody asked for is told after it has happened, and cannot be refused: `on_event(name, about)`, with `org_id`
and `at` in every `about`.

| `name` | `about` |
|---|---|
| `turn.finished` | `target` (the response's id), `session_id`, `model`, `status`, `reason`, `by` (who sent it), `usage` (the tokens its harness reported), and what its sandbox used: `seconds`, `cpus`, `memory_mb`, `cpu_seconds`, `memory_byte_seconds` |
| `egress.connection` | `subject_id` (the turn, or the install), `environment`, `host`, `port`, `refused` (why, or empty), `sent`, `received` |

An event may be missed (a process that dies between a turn's end and the telling). What an extension must not
miss, it also reads from the rows: a turn's `used` is kept on the turn (section 11), so a round of `housekeep` can
find what an event did not bring.

### 15.5 Pages and sign-ins in the console

An extension's `console()` folder is served under `/console/x/<name>/`, with the console's own headers. Its
`edition.js` is a module that exports what it adds:

```js
import { top } from "../../components/layout.js";     // the console's own modules
import { get } from "../../lib/api.js";
import { add, clear, h } from "../../lib/dom.js";

async function audit(main) {                           // draws the page at #/audit
  add(clear(main), top("Audit", "Every change."), (await get("/v1/audit")).data.map(row => h("div", {}, row.name)));
}
export const pages = [{ group: "Activity", id: "audit", label: "Audit", icon: "M5 4h14v16H5z", action: "audit.read", page: audit }];
export const signIns = [{ label: "Sign in with single sign-on", href: "/v1/sso/start" }];
```

A page is listed in the sidebar under its `group` and drawn at `#/<id>`, for whoever may do its `action` (section
11.1; a page that names none is listed for everyone); an `id` the console or an earlier extension has is refused,
in the browser's console and on the page. A sign-in is a link on the sign-in form: it
leads away, and comes back to `/console/#credential=<token>` (section 12). A module may also export `signOut`, a
function the console calls when someone signs out, before it forgets the credential: a sign-in that was made
elsewhere is ended there. No file of an extension is served in place of one of the console's.

### 15.6 Many organizations

The core has one. Every row already carries `org_id` and every transaction on an organization's rows sets
`hg.org_id`, so an extension adds three things and changes none: how organizations are made (`routes`, then
`harnessgateway.edition.give(platform, org_id)` for what one starts with), how a caller is matched to one
(`authenticate`), and row-level policies on that setting (`migrations`). A transaction that carries no
organization is the platform's own (the queue, housekeeping) and sees every row.

### 15.7 An example

`tests/integration/edition.py` is a complete extension that uses every seam, and
`tests/integration/test_extensions.py` loads it, and a second one after it, the way an installation does.

## 16. Where each part is

Dependencies point one way: `api` and `runner` use `store`, `driver`, and `domain`; `store` and `driver` use
`domain`; `domain` uses nothing. `edition.py` and `testing.py` import from all of them, and nothing imports them.

```
src/harnessgateway/
  cli.py  serve.py  settings.py  platform.py  extensions.py  errors.py  ids.py  shipped.py  defaults.py
  edition.py    everything of the core an extension may import      testing.py   what its tests are written with
  compose.yaml  how a released image is run (printed by `harnessgateway compose`)
  domain/       every rule, pure: no I/O
    line.py       the model line                       request.py    what a request must be; its credentials and files
    resolve.py    a line and its definitions           response.py   the response object and the response-level events
    events.py     what `run` prints, into events       outcome.py    how a turn ended
    handover.py   stints, and the handover's words     snapshot.py   manifests and entries
    sandbox.py    plans, variables, and policies       contract.py   the sandbox's fixed facts, and the contract's version
    resources.py  the three definitions                package.py    a package archive, read and written
    egress.py     host entries and the decision        sealing.py    secrets at rest
    schema.py     the Responses API as JSON Schemas    view.py       view links
    principal.py  who is calling                       change.py     a change, as extensions are told of it
    permission.py actions, and patterns of them
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
tests/          unit, shipped, integration, e2e
Dockerfile  compose.dev.yaml
```

`shipped.py` is the only place the server finds `console/`, `packages/`, `environments/`, and the contract.

## 17. The order to build it

Each step is complete, with its tests, before the next. Each begins with the tests that fail without it.

1. `errors`, `ids`, `settings`; then `domain/`, with `tests/unit`. Start with `line`, `request`, `events`, and
   `outcome`: they are the Responses API.
2. The contract, in `packages/harness-package-builder/contract/`: `harnessgateway schema` writes the three schemas
   from the pin; then the builder's `AGENTS.md`, its example package, and its requests.
3. `store/`: the schema file, `db` (with the owners and the refusal of a newer database), then one module per group
   of tables, each telling of the changes it makes.
4. `driver/`: `folders` (with its tests), then `docker`.
5. `runner/turn.py`, with the stand-in driver of `testing.py`; then `installer`, `runner`, `housekeeping`.
6. `extensions`, `platform`, `api/`, `serve`, `cli`, `edition`; `tests/integration/edition.py` and a second
   extension loaded after it.
7. `proxy/`.
8. `packages/shell`, then the rest of `packages/harness-package-builder`; `environments/default.json`;
   `tests/shipped`.
9. `console/`.
10. `Dockerfile`, `compose.yaml`, `compose.dev.yaml`; `tests/e2e` against an installation made from the image as
    section 14.2 says.

## 18. Checks

```sh
uv run ruff check . && uv run mypy          # lint, and strict types
uv run pytest tests/unit tests/shipped      # the rules, and what ships held to the contract: no database, no Docker
HG_TEST_DATABASE_URL=postgresql://… uv run pytest tests/integration     # on a database the tests may empty
tests/e2e/run.sh                            # the image built from this tree, installed as section 14.2 says, in a
                                            # folder and a namespace of its own; then every end-to-end check; then removed
```

`tests/e2e/run.sh` runs `install.sh` (the four commands of section 14.2, and that a second `init` makes no second
key), `turns.py` (real turns in real containers), `console.cjs` (every page of the console in a real browser),
`update.sh` (the installation stopped and started again on the same data keeps its sessions; and one whose
database a later version changed refuses to start), and `chain.sh` (the checkout's server with the sample edition
and a second extension after it, in a real browser: both add their pages and sign-ins, and nothing is replaced).
Continuous integration runs all four levels on every change, builds the image, and scans it.

What must hold, and which test holds it:

| Promise | Test |
|---|---|
| A request the SDK would not send is refused, naming the field | `unit/test_request.py` |
| Every printed line becomes valid events and a valid response | `unit/test_events.py` |
| A handover's words name every stint and each harness's memory | `unit/test_handover.py` |
| A turn restores, runs, saves, and concludes; fork and rewind return to a response's state | `integration/test_turns.py` |
| `store: false` leaves no response, no snapshot, no session, and no record of a change | `integration/test_turns.py` |
| A session is reached by its owner, and by anyone else only when reaching everyone's is allowed; the list shows a caller what they may read; each person's chats are their own | `integration/test_sessions.py`, `e2e/turns.py`, `e2e/console.cjs` |
| A harness is told where the one before it keeps its memory | `integration/test_turns.py`, `e2e/turns.py` |
| The platform names no agent CLI, and no edition | `unit/test_agnostic.py` |
| What ships keeps the contract, and what the harness package builder is told is what the platform has | `shipped/test_packages.py` |
| A key acts as whoever made it, does only what its scopes match, is refused from its time, and makes no key once it is limited; a caller is told exactly what they may do | `unit/test_permission.py`, `integration/test_keys.py`, `e2e/turns.py`, `e2e/console.cjs` |
| Every seam works, alone and with a second extension after it; an extension that fails on an event fails nothing else | `integration/test_extensions.py`, `unit/test_extensions.py`, `e2e/chain.sh` |
| A change and its record commit together: an extension that raises undoes the change; every change of section 15.3 is told, once, with who made it | `integration/test_changes.py` |
| Two owners may each have a schema file of one name; a database newer than the code is refused | `integration/test_schema.py` |
| An extension imports the core only through `harnessgateway.edition` | `unit/test_edition.py` |
| No secret is in a sandbox, a response, an event, a change, or a log | `integration/test_proxy.py`, `integration/test_changes.py`, `e2e/turns.py` |
| An installation is made from the image alone, and is updated in place with its data | `e2e/install.sh`, `e2e/update.sh` |

## 19. What 1.0.0 changes from 6.0.1

What the platform does for a caller is unchanged: the API's routes, the definitions, the package contract (3), the
sandbox, sessions, handover, the console's pages. What changes is how the platform is distributed, and what an
edition is given. An installation of 6.x is not upgraded: 1.0.0 is installed fresh, and is the first version that
is upgraded in place.

| | 6.0.1 | 1.0.0 |
|---|---|---|
| Installing | Clone the repository; `install.sh` builds the image on the host | The released image and four commands (section 14.2). `install.sh` is gone; `compose.yaml` is inside the image, names it, and builds nothing; `compose.dev.yaml` is for a checkout |
| `HG_EXTENSIONS` | Set by the Compose file (to nothing, for the core) | Set by the image. The Compose file does not name it, and gives `serve` everything in `.env` |
| The command line | `serve`, `proxy`, `init`, `schema` | And `compose`, `secrets` |
| Schema files | Recorded by file name alone | Recorded with their owner; a database newer than the code is refused (section 13) |
| Updating | Run the installer again; no promise | In place, with its data, from any 1.x to a later one (section 14.3) |
| Several extensions | Loaded in order, but one extension's `edition.js` replaced another's, an extension that failed on an event stopped the rest from hearing, and two drivers of one name were taken silently | A chain: section 15.2 says what several mean for every seam |
| What an extension imports | Any module of the core | `harnessgateway.edition`, and `harnessgateway.testing` for its tests |
| How an extension gets the platform | As a side effect of `routes` | `start(platform)` |
| Timed work of an extension | None | `housekeep(platform, elapsed_s)`, in the round the core takes |
| Changes | Told after the change was committed, from the API's handlers, best effort (`on_event`); a first key, a cancelled or deleted response, and a file were not told | Told in the change's transaction, by the stores, with who made it (`on_change`, section 15.3) |
| Events | `turn.finished`, without who sent it or the tokens | `turn.finished` with both, and `egress.connection` |
| What a sandbox may reach | The environment's policy | And what `egress` narrows it to |
| What a turn used | Added to the organization's day, and gone | Also kept on the turn (`used`), and shown with a session's turns |
| The proxy's report | Bytes for each organization | Each connection: host, port, refusal, bytes |
| View links | Signed with `HG_SECRET_KEY` itself | Signed with a key made once and kept sealed, so a key service can stand in for `HG_SECRET_KEY` |
| The platform object | Its sealer and blob store were not kept on it | `platform.sealer`, `platform.blobs` |
| `GET /v1/platform` | The core's facts | And the extensions loaded |
| The console and editions | An edition's files were served in place of the console's | Served under `/console/x/<name>/`; pages and sign-ins are added, nothing is replaced; a credential may arrive as `#credential=` |
| Tests against a running installation | `tests/live`, on an installation someone made | `tests/e2e`, on one the tests make from the image and remove |

## 20. Decided, and still open

Decided here, and the founder's to overrule:

- `conversation` and `prompt` stay refused. Supporting `conversation` would mean serving OpenAI's Conversations
  API, with a session as a conversation.
- An unsupported field fails inside the turn, as a failed response, because only a package knows what its harness
  can do and a package declares nothing. OpenAI answers the same mistake with HTTP 400 before any work starts.
- The handover's words are the platform's, in one place, and are prompt content: they are the founder's to change.
- An extension that raises in `on_change` refuses the request. The other choice, to make the change and log that
  its record was lost, is the one an audit log cannot be built on.
- A turn's end is an event and not a change: nothing may stop a turn from ending.
- The first API key is printed by a command someone runs, never into a log.
- 1.0.0 is installed fresh. Nothing is kept in the code for an installation of 6.x.
- The core is for one person. Every key acts as its owner, so chats and sessions are theirs whichever key they
  came with; what the core gives an edition is who a key acts as, the questions about anyone else's sessions and
  keys (`sessions.read_all`, `sessions.write_all`, `keys.read_all`, `keys.write_all`), and each session's owner.
- A key's own limits (its scopes, its time) are the core's, since the key and the actions are: an edition adds who
  people are and what their roles allow, and a request must pass both.
- A limited key makes no key. The other choice, to let it make keys no wider and no longer-lived than itself,
  is more rule than its use is worth.
- The console's chats are kept under who is signed in. Chats a browser kept before 1.1.0, under no one, are not
  shown: there is no telling whose they were.

Open:

- A pointer-only handover costs the new harness steps to read up, every time the harness changes. A caller who
  wants a change to cost nothing keeps the conversation itself and sends it in `input`.
- A harness's own record is in a format the platform does not control, and the reading harness has to cope with
  it.
- The queue is oldest first across every organization. With many organizations one can make the others wait; the
  hosted service limits how many turns each may run at once (its `before_turn`), which bounds it, and a fair queue
  is the core's to add when that is not enough.
- A sandbox plan does not say which organization it is for. A driver that keeps organizations apart on its
  infrastructure (a namespace each) needs it; it is added with the first driver that does.
