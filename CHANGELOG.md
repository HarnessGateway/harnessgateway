# Changelog

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
