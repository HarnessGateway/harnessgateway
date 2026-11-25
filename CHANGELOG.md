# Changelog

## 6.0.0

The platform, rebuilt to its specification ([SPEC.md](SPEC.md)). The API's routes, the definitions, the sandbox,
the egress proxy, the console and the deployment are as they were. What changes is how a session is handed from
one harness to another, what a package owes a request, what `store: false` means, and what ships. This is a change
of the package contract, to contract 3: a package that reads `history.json` or `$HG_CONTRACT` no longer finds it,
and every package should be brought to 3.

- **A handover gives a harness no history.** `history.json` is gone. `handover.txt` is the session's handover
  history, in words: who worked on it, in order, the responses each gave, and where each harness keeps its memory.
  The new harness reads what it needs from the mind the session's harnesses share. It is empty unless the turn
  before was another harness's.
- **A package keeps its harness's memory in the mind, and says where.** `run` writes `$HG_STATE/memory`: a few
  lines of where its CLI keeps its record of the session, and in what form. That note is what the next harness is
  pointed at.
- **Honour or fail.** A field of a request that a package does not honour, and that is not the platform's or a
  label, ends the turn with `response.failed`, code `unsupported_parameter`, and a message naming the field. It
  was left alone, and noted in the turn's log.
- **`store: false` keeps nothing.** The turn runs in a session of its own and is never saved. With
  `previous_response_id` it starts from that response's state and leaves that session as it was; it used to
  become that session's next turn, and be deleted from it.
- **`hg-fde` is now `harness-package-builder`, and it carries the contract.** The Responses API as three schemas (a
  request, a response, and the stream's events) is in its package (`contract/`), and no sandbox is given them any
  more: `$HG_CONTRACT` is gone. `PACKAGES.md` and `lines.schema.json` are gone too: what they said is in what the
  builder is told and shown. What it is told
  is general: what a harness package is, why it exists, how a developer uses one through the Responses API, how the
  platform works, and the framework for building one. Whether a package is a CLI as it comes or an agent shaped
  for a purpose depends only on what is asked of it.
- **The packages that ship are the platform's, and follow it.** Every start puts `shell` and
  `harness-package-builder` at the running version's, so an update of the platform updates the builder and the
  schemas it builds to. They are not changed in place: `PUT` and `DELETE` on their names are refused, and a copy
  under another name is yours. The `default` environment is still given once, at setup, and yours from then on. An
  installation that has `hg-fde` keeps it as its own harness: delete it, and name the new builder in your
  environments.
- **The packages that ship are brought to contract 3.** `shell` honours `input` alone and keeps a record of the
  commands it ran. The builder fails what its CLI cannot be given before the CLI is started, and notes where its
  CLI's transcript is.
- **The builder is shown a whole package, and the requests a developer sends.** Its `example/` is a harness package
  to read, a cloud consultant on Claude Code: `README.md`, `install`, and `run` are required, and everything else (who the agent is, skills, tool
  servers, settings) is optional, and is where a customized agent begins. Its `requests/` is one session of
  Responses requests, which a package is tested with. It is given the three schemas of the API: a request, a
  response, and the stream's events. Its CLI is pinned to the version its makers mark stable.
- **A conversation the caller keeps can be sent back whole.** The platform takes back an item a response held as
  it was, though the SDK's input form of it lacks a field (a tool output's `status`): it refused its own output
  before. The builder takes back every item it prints, as what it said, thought, and did earlier.
- **The console's chat sends a picture as an image.** A PNG, JPEG, GIF, or WebP given with a message is an
  `input_image`, which the model is shown. It was sent as a file, as everything else still is.

## 5.2.1

`hg-fde` uses the Responses API as far as its agent CLI goes. Every package it builds is written from its own
`run`, so that is the example of what a package can do with the one schema. Nothing of the platform changes. An
installation's `hg-fde` is its own, as given at setup: add the package again to have this one
(`tar -czf hg-fde.tar.gz -C packages/hg-fde .`, then `PUT /v1/harnesses/hg-fde` with it).

- **More of a request reaches the agent.** `reasoning.effort`; `text.format`, a JSON schema, which the response's
  one message then fits; an `input_image`, as an image; an `input_file`, sent inside the request or by address;
  what a `developer` or a `system` message says, as instructions; what the agent said earlier (`assistant`); the
  `authorization` of an MCP server, which was dropped; and `tool_choice: "none"`, with which it answers and does
  nothing.
- **What it leaves alone, it names.** A field, a tool or a part of the input its agent CLI has no use for is said
  in the turn's log, and the contract now asks that of every package.
- **Each thing it does is the item that says what it was.** A command is printed when it is called and its output
  when it returns, with its own exit code. A file's change says whether it was created or updated, with the diff
  the CLI reports. A tool server is listed with its tools (`mcp_list_tools`), or with the error when it did not
  connect; a call to one begins and ends as one `mcp_call`, with its `output` or its `error`. A search and an
  opened page are `web_search_call`. A call keeps the CLI's own id. Usage has cache writes beside cache reads.
- **The console** shows what was searched for, the address of a page that was opened, and why a call to a tool
  server failed.

## 5.2.0

A harness package is two programs. This is a change of the package contract, to contract 2.

- **`harness.json` is gone.** A package is `install`, `run`, and whatever files they need. It declares nothing:
  a harness is its package, under the name it is added as, and there is nothing to set on one.
- **The environment is the only door.** What a turn may reach and hold is said in one place: the environment's
  image, limits, network, variables and secrets. A package that needs a token reads it from a variable, and the
  environment gives it a secret of that name and the hosts it goes to. Settings and secrets of a harness, and the
  API to set them, are removed.
- **`run` chooses its provider's API.** It is given the provider's base URL for each API the provider speaks
  (`HG_PROVIDER_ANTHROPIC`, `HG_PROVIDER_OPENAI_RESPONSES`, `HG_PROVIDER_OPENAI_CHAT`, `HG_PROVIDER_GEMINI`) and
  uses the one its agent CLI speaks. `HG_PROVIDER_URL`, `HG_PROVIDER_API` and `HG_HARNESS_VERSION` are removed.
  A provider the CLI cannot use is no longer refused when the request is sent: the turn fails, with the package's
  own words. A line that names an environment, a harness or a provider that is not there is still refused at once.
- **`~/.cache` is never saved with a session**, in place of the folders a package listed. An install has 15
  minutes, and a stopped turn 15 seconds to end, for every package.
- **The console** edits a harness as its files and the environments that carry it. A new harness and an imported
  one are named by the person; the model line offers every provider, or none.
- **HG FDE** writes two files, and says in the package's README what its environment must give it.

## 5.1.3

- **A package is its files, however it was packed.** On a Mac, `tar` adds a hidden `._name` entry beside every
  file, and the platform took each as a file of the package. Those, `.DS_Store`, `__pycache__` and `.git` are now
  left out wherever a package is read: an uploaded archive, a folder of a session's work, and the packages that
  ship. A harness's package is kept, and given back, as its files alone, written one way.

## 5.1.2

`hg-fde` (now `2.1.330-3`) builds a package and hands it over; it no longer verifies it. Its `verify.py` is gone,
and so is the step that ran it. Whoever adds the package tries it on the platform, and comes back with the turn's
log if it fails. Its method is still a skill it loads, apart from `AGENTS.md`, which says who it is.

## 5.1.1

`hg-fde` (now `2.1.330-2`) is made simple again. It does one thing: it researches an agent CLI, writes
`harness.json`, `install` and `run`, checks that the package runs, and hands it over.

- Its skill is six short steps. The `agent/` folder, the step that asked what agent was wanted, and the section on
  keeping a package up to date are gone.
- Its verifier no longer has a suite (`--suite`) or a tool server of its own. It still runs a package's install and
  a hello world, holds every line to the schema, and sends any request (`--say`, `--request`, `--new`,
  `--stop-after`, `--shape`).
- Nothing of the platform changes.

## 5.1.0

The OpenAI Responses API is now the only schema a harness package deals in. A package's `run` reads the caller's
request as it was sent and prints pieces of a Responses response; there is no helper to import and no line of the
platform's own to print. **A package written for an earlier version must be rewritten**: the contract is still
version 1, because nothing has been built on it yet.

- **hgkit is gone.** `run` prints, one JSON object a line: an output item, whole (any of the API's, `message` to
  `mcp_approval_request`); a stream event, for what arrives in pieces; and last the stream's own last event
  (`response.completed`, `response.incomplete` or `response.failed`), holding only what the package knows of the
  response (`usage`, `incomplete_details`, `error`). The lines `hg.done`, `hg.incomplete`, `hg.failed` and `hg.log`
  no longer exist; the log is standard error.
- **The bookkeeping is the platform's.** It numbers the events, places the items, names an item that came without
  an id, and keeps the message (or the reasoning) that a piece of text naming no item belongs to. A message
  printed whole reaches a caller as a stream carries text, deltas included.
- **A refused line says which field.** Each item is checked against the OpenAI SDK's model of that item, so the
  message names the item and the field (`shell_call: action: Field required`).
- **The Responses API is pinned, and kept as schemas.** The OpenAI SDK is pinned to one version (`openai 3.57.0`),
  which `GET /v1/platform` reports as `sdk`. Four schemas are generated from it and kept in the repository's
  `contract/`, beside `PACKAGES.md`, which moves there from `docs/`: a request, a response and the stream's events,
  as OpenAI's SDK describes them, unchanged; and the platform's rule for one line a `run` may print. A test fails
  when they are not what the pin gives; `harnessgateway schema` writes them.
- **Every sandbox is given the contract.** It is at `$HG_CONTRACT` (`/etc/hg/contract`), read-only and written when
  the platform starts, so it is always the running version's. It is no longer copied into each install, and a
  package may hold a folder of any name. `HG_CONTRACT` was the contract's number and is now this folder.
- **`/turn/previous.json`** is the response a request continues from, as the API returns it. Its `status` tells a
  package that the turn before was stopped, and its `output` holds the call a request's answer is for.
- **A request's credentials are secrets.** The `authorization` and `headers` of an MCP server a request names are
  kept sealed while the turn runs, bound to that server's host, and gone when it ends. The request is stored,
  returned and given to its harness with placeholders; the egress proxy puts the values in.
- **A response says when it ended, and reports back more.** `completed_at` is set, and every field the API's
  request and response both have is reported where the caller sent it.
- **Every tool type reaches the package.** A request's `tools` were refused unless they were `mcp`. They are now
  passed on as sent, like every other field: a package offers a `function` tool to its agent and ends the turn
  with the `function_call`, and the caller answers in the next request.
- **A stop reaches everything `run` started.** `SIGTERM` goes to `run` and to its CLI at once, so a package
  forwards nothing: it takes the signal and waits for its CLI to end.
- **A failure's code is the API's when the API has it.** `error.code` is the package's own code when that is one
  of the Responses API's (`rate_limit_exceeded`, say), and `server_error` otherwise; `metadata.hg_code` has it
  either way.
- The handover line is given once, as `/turn/handover.txt`; the variable `HG_HANDOVER` is gone.
- **`harness-package-builder` is now `hg-fde`**: HG FDE, the forward deployed engineer for the platform. It starts
  from the agent that is wanted, researches the agent CLI, and writes a package in two parts: the adapter, and an
  `agent/` folder of instructions, skills, hooks, tool servers and settings. It is given the contract and all four
  schemas in full on every turn. Its verifier has a suite (`--suite`) that proves by a marker, in real turns:
  instructions on a first and on a continued turn, memory, a stop and carrying on after it, a tool server the
  request names, and the size of the home. It also sends any request (`--request`), as the first turn of a new
  session when asked (`--new`), and shows the shape of any item (`--shape`).
- The platform names no agent CLI, in its code, its console or its contract, and a test keeps that so.
- `shell` is written this way too.

## 5.0.2

This version arranges the repository so that what ships beside the program can one day live in a repository of
its own. One thing changes in an installation: every install of a harness now holds the package contract beside
the kit.

- The kit is `kit/hgkit.py`, at the top of the repository beside `packages/` and `environments/`. None of the
  three is the server's code: they know the platform only by its contract, the server reaches them in one place
  only (`shipped.py`), and their tests are `tests/shipped/`.
- The package contract is the core's own document again, `docs/PACKAGES.md`. The platform puts it beside the kit
  in every install, at `/harness/kit/PACKAGES.md`, which is where `harness-package-builder` (now `2.1.330-3`) reads
  it. A harness that is already installed is not installed again for this.

## 5.0.1

This version changes where things are in the repository, and nothing a running installation does.

- What an installation starts with is at the top of the repository: the packages in `packages/` (`shell` and
  `harness-package-builder`), and the environment in `environments/default.json`. Before, both were inside the
  Python package, under `src/harnessgateway/defaults/`. First-time setup gives every package in the first folder
  as a harness and every definition in the second as an environment, each under its own name.
- The package contract is now at
  `packages/harness-package-builder/skills/building-harnessgateway-harness-package/PACKAGES.md`.

## 5.0.0

This version is a new platform: the same idea and the same wire format, written again from the beginning as the
smallest core that does the job. It is not an upgrade in place. An installation of 4.x is not migrated, and its
data, definitions and packages do not carry over; 4.4.1 stays as the tag `v4.4.1`.

What it is:

- The OpenAI Responses API, unmodified, with the Files and Containers APIs beside it.
- Providers, environments and harnesses as definitions, with sealed secrets. A request names all four parts of
  its model: `environment/harness/provider/model`.
- Harness packages run in hardened sandboxes; sessions saved as content-addressed snapshots.
- An egress proxy that enforces each environment's network and keeps secrets out of sandboxes.
- Sessions as their requests and responses; usage as CPU, memory, network and storage.
- Links a browser can open a session's files through, sandboxed (`POST /v1/containers/{id}/view`).
- A console at `/console`: chat, providers, environments, harnesses, sessions and usage.
- The seams an edition builds on, and a sample extension that uses each.

What is different from 4.x:

- **Installing.** Clone the repository and run `./install.sh`: it builds the image on the host and starts it.
  `deploy/install.sh`, with its `--with-gvisor`, is gone; sandboxes run under gVisor when the host has `runsc`.
- **What an installation starts with.** The `default` environment and two harnesses: `shell`, and
  `harness-package-builder`, which writes a package for any other agent CLI and ends with a link that adds it.
  The packages of the `harness-packages` repository were written for the 4.x kit and do not run on this version.
- **Packages.** A package is still `harness.json`, `install` and `run`. There is no `seeds/` folder and the kit is
  smaller (`Request`, `State`, `Cli`, `Emitter`): what a CLI is given, its `run` points it at or puts in place.
  `harnessgateway package check` is gone; the builder's `verify.py` runs a package's install and a hello world.
- **Sandboxes.** There is no sandbox image of the platform's own: an environment names any image. What a sandbox
  may reach is the environment's `network` (`closed`, `limited` or `open`).
- **Bringing in a package an agent wrote.** `PUT /v1/harnesses/{name}` with `{"session", "path"}`; the drafts
  API is gone.
- **Keys.** There is one kind of API key. Who may do what is an edition's to say, through a seam.
- **The image.** One image, `ghcr.io/harnessgateway/harnessgateway`, runs both `serve` and `proxy`.

## 4.4.1

This version changes no code.

- A walkthrough of the console's Providers, Environments and Harnesses pages, with screenshots:
  `docs/console-setup.md`. For each part of each page it says what the part shows, the API call behind it and
  what the platform does with that call. The README links to it.

## 4.4.0

This version changes the API: the audit log is no longer in the core.

- The core keeps no audit log. `GET /v1/audit`, the console's Audit log page and the `audit` table are gone.
  An audit log is the enterprise edition's: it keeps the record and serves it.
- The core still tells its extensions of every change to a definition, a key or a secret, and of every deleted
  session, as it happens (`audit(record)`: the organization, who, what, to which thing, which fields, when, and
  never a secret). That is what an edition keeps its audit log from. Before, the record was stored first and
  the extensions told after.

## 4.3.1

- A new installation installs the harnesses its `default` environment carries from its first start. The
  installer now does the first-time setup before it starts the platform. Before, it started the platform first
  and set up afterwards, in a process the running platform did not hear from: nothing was installed until a
  turn named a harness (and then only that one) or the platform was started again.
- The installer's last lines say that the harnesses that ship are being installed, and where to see how far.

## 4.3.0

This version changes the package contract: a manifest that names hosts to reach is refused.

- The environment constrains the agent. A turn reaches its provider and what its environment allows, and
  nothing else. A harness package no longer adds hosts: `run.egress` and `install.egress` are gone from
  `harness.json`. A harness secret is still put in only toward the hosts its manifest binds it to, and those
  hosts are reached only where the environment allows them.
- An install reaches the package managers and the environment's `setup.egress`, as its setup script does.
  A CLI that is downloaded from anywhere else needs that host in `setup.egress` of each environment that
  carries it.
- Console: a harness's page no longer lists hosts it reaches, and an environment's form asks what "Setup and
  installs may also reach".
- An open environment may carry a deny list: any public host, except those it names. Before, a deny list
  was refused with any mode but `limited`.
- A turn that is stopped keeps its CLI's session. The kit's `Cli` now ends `run` when the CLI it stopped has
  ended, so nothing after `lines()` runs: no terminal line, and no second CLI. Before, the four agent
  packages took a stopped CLI's exit for a session that could not be resumed and started a new one while the
  turn was being stopped, and the next turn had forgotten the conversation. `Cli.stopped` is gone.
- A stop reaches everything the CLI started. The kit passes the signal to every process below `run`, a daemon
  the CLI started for itself included, and waits for them to end. Before, such a daemon was killed with the
  sandbox part-way through the turn (Hermes then held the session for five minutes).
- `serve` stops when its turn runner or its housekeeping ends, and exits with code 1, so that what supervises
  it starts it again. Before, a runner that lost the database went on unnoticed: the process kept accepting
  requests that nothing would ever run.
- A NUL character no longer fails a turn. An agent that printed one (the content of a binary file, say) had
  its turn end as `sandbox_failed` with nothing saved, because the database holds no text with one. In a
  turn's output and log each becomes U+FFFD; a request body that holds one is refused with `400`.
- Text that was being printed when a turn was stopped or killed is kept: its message or reasoning item is in
  the response's output with `status: "incomplete"`. Before, it was streamed and then left out of the response.
- The runner fills a slot the moment one of its own turns ends, not at its next heartbeat.
- An environment says which hosts the last attempt of each of its installs asked for and was refused
  (`installs[].refused`, and on its page in the console): the hosts to add to `setup.egress` when a CLI is downloaded from somewhere the
  package managers do not cover.
- Console: the draft editor shows a file as open once its content has arrived, and takes no typing before
  that; a session page no longer replaces its buttons at every poll, which could lose a click.
- The README's and the console's examples of a setup script work on the platform's sandbox image, which has
  pip and npm and no system package manager.
- Usage has the processor time and the peak memory of each turn's sandbox (`cpu_seconds`,
  `memory_peak_bytes`), measured by the driver. `GET /v1/usage` reports them, a turn's trace has all its
  measures as `used`, and the console shows CPU time, peak memory, network out and in, and storage.
- A package may name `cache` folders in its mind: they are not saved with the session, and the CLI makes
  them again after a restore. They still count toward the disk limit.
- Every install is given the platform itself as a wheel, beside the kit. The harness package builder installs
  its package checker from it; before, it pinned a platform version of its own, and checked packages against
  an older contract than the one it was told about.
- The kit has `outbound()`: the proxy and certificate variables of the turn, for a package to write into
  the configuration of a stdio MCP server, which a CLI starts without them.
- The kit's `seed_mind()` is `seed()`, and it also copies `seeds/work` into the work folder, for a CLI that
  reads its instructions only from the folder it runs in.
- A provider and a harness may speak the Gemini API: `gemini` is a fourth API kind, beside `anthropic`,
  `openai-responses` and `openai-chat`.
- The platform ships with 27 harness packages: the CLIs of 26 agent harnesses, each unmodified, and the
  harness package builder. 22 of them were written by the builder and proven on a platform (the packages
  repository says how). A new installation adds them all, and its `default` environment carries them all.
- Under gVisor a sandbox reaches the egress proxy. It is given the proxy's address for the name `egress`, in
  its hosts file. Before, it asked Docker's name service for the network, which a sandbox under gVisor does
  not reach: nothing that needed the network worked there (an install that downloads, a call to a model).
  A sandbox on the host's kernel was not affected.
- `install.sh --with-gvisor` installs gVisor from the one archive its makers now publish (runsc, the
  containerd shim and the helpers runsc needs beside it). The separate binaries it downloaded before are gone
  from their release folder, so the option failed on every version.
- The catalog may say how a new installation's `default` environment is set up so that every package installs
  and runs in it: `default-environment.json` beside the packages, holding fields of an environment. The
  packages that ship ask for one host during setup, `x.ai`, which Grok Build's install downloads from.

## 4.2.0

This version changes the API: requests and clients written for 4.1 need the changes below.

- The `model` string is `environment/harness/provider/model`, every part named, always. Nothing is filled in:
  a harness has no default provider or model any more, and there is no default environment to fall back
  to. A harness that calls no model is named with `none/none`, as in `default/shell/none/none`. The short
  forms (`claude-code`, `codex/deepseek`, `gpu:pi`) are refused, and the refusal says the grammar.
- An environment carries a list of harnesses, `harnesses`. A request may name only a harness its
  environment carries, and each harness it carries is installed on its image. Which harnesses an
  environment carries is set on the environment: in the console, a checklist in its form. A harness that
  is removed is taken out of every environment.
- An environment may have a setup script, `setup: {script, egress, timeout_s}`. It runs once, as root, on
  the environment's image, before any harness is installed; what it leaves is the image the environment's
  turns run on. It reaches the package managers, the hosts in `setup.egress` and the hosts of the
  environment's secrets, through the egress proxy, and sees each secret as a placeholder only. It runs
  again when the script or the image changes. A turn whose environment's setup failed fails with
  `setup_failed`.
- An environment is prepared (set up, then its harnesses installed) when it is made or changed, when a
  harness it carries is added or replaced, and when the platform starts. Changing it again tries again what
  failed. Its view says how far it is: `image_id`, `build` (status, and the log on a single environment, for a developer or an admin)
  and `installs` (one per harness it carries). `POST /v1/harnesses/{name}/install` and
  `GET /v1/harnesses/{name}/installs` are removed; a harness's view lists the `environments` that carry it.
- A provider lists the models it offers, `models`, for the console's pickers. A request may name any model.
  `default_model` is removed. A provider may not be named `none`.
- A runner key may list and read providers, environments and harnesses, so an application can find out
  what it may name. `GET /v1/models` is removed.
- The `default` environment is an environment like any other: it may be deleted. A new installation adds
  every harness package that ships with the platform and makes `default` carry them all. An installation
  that is updated keeps its harnesses, and its `default` environment carries every one it has.
- Housekeeping removes an image set up for an environment an hour after no environment runs on it, and what
  was installed on it.
- The catalog's Hermes package installs Hermes itself, with Hermes's own package manager, on any image that
  has git and Python 3.14: it no longer needs an environment built from Hermes's image, and runs in `default`
  like the other harnesses. Its install takes a few minutes and about a gigabyte.
- CI installs every package that ships with the platform on the sandbox image, as the platform does.
- Console: Setup is shown to every key, read-only for a runner. Harnesses is the organization's inventory;
  a harness's page lists the environments that carry it, in place of Defaults and Installs. An environment
  has a page of its own: its setup and its log, each harness's install and its log, and Prepare again. The
  provider form lists its models. The chat and the playground pick an environment, then a harness it
  carries, then a provider that speaks it and one of its models.

## 4.1.4

- A sandbox killed with exit 137 that the platform did not stop (not for a cancel, the time limit or the disk
  limit) fails with `out_of_memory`. Docker can report a container's exit before the kernel's out-of-memory
  event reaches it, so its out-of-memory flag can still be false: such a turn failed with `harness_error`.
- The catalog's Claude Code and Harness Package Builder packages install on npm 12, which the default sandbox
  image has and which runs no package's install script unless told to.

## 4.1.3

- An environment that names no image uses the platform's sandbox image of the version that is running, and
  moves to each new version's image when the platform is updated. The `default` environment names none, so it
  follows the platform. Before, an environment was given the image of the version it was made under, and kept
  it. An environment that names an image keeps it; setting `image` to `null` makes it follow again.
- Console: an environment that names no image shows "the platform's sandbox image", and clearing the Image
  field makes it follow again.

## 4.1.2

- The default sandbox image is built on Chainguard's Wolfi base, pinned by digest: Node.js 26 with npm,
  Python 3.14 with pip and venv, and git, curl, jq, ripgrep and the common shell tools. No package manager or
  BusyBox is left in it, no file raises privileges, and one user, 10000, may log in. It was Debian 13.
- The platform image is built on the same base: Python 3.13 with exactly the dependencies uv.lock pins, the
  Docker client, bash and coreutils. No package manager or BusyBox is left in it, and no file raises
  privileges. It was Debian 13.
- CI scans the platform image and the sandbox image with Trivy on every change and every week. A CRITICAL or
  HIGH finding with a fix available fails the run.

## 4.1.1

- Console: an environment's form is in four groups: General (name, image), Resources (CPUs, memory,
  processes, disk, /tmp, a turn's time), Network access (the mode, and Limited's rules set apart beneath it),
  and Variables and secrets. The environments table names its last column the same.

## 4.1.0

- Environments: a network is `closed`, `limited` or `open`, and nothing is reachable unless its mode allows
  it. `closed` reaches the turn's provider and nothing else, so every harness runs in it; it takes the place
  of `none`, which had no network at all and refused a harness that calls a model. `limited` reaches the
  provider, its allow list, what the package declares, the hosts of the secrets in use and, when allowed, the
  package managers; its deny list wins over all of them. `open` reaches any public host. `allow` and `deny`
  are rules of `limited`, and are refused with the other two.
- The package-manager set is wider: registries for more languages, GitLab and Bitbucket, container
  registries, and common tool downloads. It holds no cloud platform. Installing a harness reaches it whatever
  its environment's mode.
- Console: an environment's network is Closed, Limited or Open. The allow list, the deny list and Allow
  package managers show for Limited only.

## 4.0.3

- Console: a provider's API shape is one API: `anthropic (Messages)`, `openai (Responses)` or
  `openai (Chat Completions)`. In 4.0.2 one `openai` shape stood for both OpenAI APIs, so a harness could be
  sent to an API its provider does not have. A provider added with that shape speaks both until it is saved
  again; an endpoint that speaks two APIs is added once for each.
- Console: with no harness installed, the chat says so and links to Setup → Harnesses. Before, it showed an
  empty picker and sent a request that was refused.

## 4.0.2

- Console: a provider is added as a name, an API shape, a base URL and a key, as in version 3. `anthropic` is
  the Anthropic Messages API; `openai` is the OpenAI Responses and Chat Completions APIs at one base URL. The
  base URL is filled in for the shape chosen. The form sets no default model: a request names its model
  (`harness/provider/model`), or the harness has a default.
- Console: the chat and the playground always show the environment beside the harness, the provider and the
  model. Each message can change any of the four.

## 4.0.1

- Console: Setup lists Providers, Environments, Harnesses, the order a platform is set up in. A developer or
  an admin lands on Providers after signing in.
- Installer: run again, it updates the installation to the installer's version. It kept the images of the
  first install before, and waited on the default port whatever port the installation used. The port and the
  address are kept too; `--port` or `--bind` given again replaces them.
- Installer: piped from the web, it no longer ends after printing the admin key; it says where the platform
  is.
- Every change that reaches `main` is a version with an entry here. The release refuses a `main` that moved
  without one.

## 4.0.0

A new platform, written from an empty tree. Nothing carries over from earlier versions: not the code, the
schema, the package format, or the configuration. There is no upgrade path; install it fresh.

- Any agent harness CLI runs in a sandbox behind the unmodified OpenAI Responses API. The `model` string,
  `[environment:]harness[/provider[/model]]`, chooses the sandbox, the harness, and the model it uses.
- A session keeps a mind (`$HOME`) and a work folder, saved after every turn as content-addressed
  snapshots. A turn can be continued, forked, or handed to another harness, which finds the same work and
  is told where the previous harness kept its notes.
- Harness packages (`harness.json`, `install`, `run`) hold everything specific to one CLI. The platform
  knows no harness. `harnessgateway package check` replays a package's recorded cases.
- Environments: any Linux image, with CPU, memory, disk and time limits, variables, secrets, and a network
  policy. The image's own entrypoint is never run.
- Secrets never enter a sandbox. The egress proxy, the only way out, replaces per-turn placeholders with
  real values on the way to the hosts a secret is bound to.
- Files and Containers APIs: send files into a turn, and list and download what an agent made.
- Management API: providers, harnesses, environments, sessions with traces, keys, usage, audit.
- The console of version 3, on this version's API: the chat with its file panel and previews, interrupt,
  rewind and branch; each session's transcript, traces and files; the harness editor (drafts) and the
  catalog; the playground; and New harness from workspace, on a session of the harness package builder,
  for the package it wrote.
- Drafts (`/v1/drafts`), a package's files as installed, and links a browser can open to a session's files
  (`POST /v1/containers/{id}/view`), served sandboxed.
- The default sandbox image is Debian 13 with Node.js 24 and Python 3.13.
- One host with Docker and PostgreSQL. gVisor isolation where the runtime is installed.
- Extension points for other editions: drivers, blob stores, authentication, routes, schema, console files,
  and hooks before and after a turn.
