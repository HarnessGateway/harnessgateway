# Changelog

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
