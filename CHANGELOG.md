# Changelog

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
