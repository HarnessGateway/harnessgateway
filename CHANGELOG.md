# Changelog

Every release is a section here, newest first, and the newest section's heading is the version.
A merge to `main` that adds a section releases it: the release workflow tags that version, builds
the wheel and the image, and publishes the section as its notes.

## [7.1.0]

- **Closing the request stops the turn.** A turn belongs to the request that made it: when a client
  closes a foreground stream, or stops waiting on a blocking `POST /v1/responses`, the platform
  stops the turn gracefully (the package's stop signal and grace, `response.sh`), with no endpoint
  called. The response ends `cancelled` with `metadata.incomplete_reason: client_disconnected` and
  the trace records the disconnect. A `background: true` turn outlives its request: a follower
  closing its stream changes nothing; only `cancel` ends it early.
- **Teams are isolated in their sandboxes and their sessions.** A turn on a team's harness runs as
  a uid of the team's own, not the organization's, so two teams of one organization are as apart
  on a worker as two organizations are. A team's sessions are the team's: every member of the team
  sees them, opens them in Conversations and continues them; other members of the organization do
  not. Reach is organization, then team, then member.
- Input files keep their relative path inside `$HG_FILES` (`src/a.txt`), never an absolute one and
  never one climbing out.
- The tests that 7.0 dropped are back: the gateway's key swap against a TLS upstream, the first
  boot, the containerized platform, sessions and accounts as units; plus the disconnect, the team
  scope and the sandbox uid.

## [7.0.1]

- `harnessgateway serve --dev` sends a turn's provider calls through the gateway: the node without a
  door names the gateway as the provider's address and puts the session token in a file of the
  turn, as the sandboxed runtime's door does, so a catalog package runs unchanged on a laptop.
- The base-image workflow scans the image it just pushed through the runner's own daemon.

## [7.0.0]

HarnessGateway, rebuilt around one idea: the platform provides the wire and the sandbox, and a
harness package decides everything else.

- **The request is the package's.** A client's OpenAI Responses request is validated against the
  SDK's own schema and handed to the package verbatim, as `request.json`. The package reads what
  its CLI can use (`input`, `instructions`, `previous_response_id`, `reasoning.effort`, `tools`, …)
  and builds the invocation. The platform reads only `model`, `previous_response_id`, `stream`,
  `background`, `store` and `metadata`.
- **The response is the package's.** A package prints Responses stream events as it runs (text
  deltas, `reasoning`, `shell_call`, `apply_patch_call`, `mcp_call` items) and the platform relays
  them with sequence numbers; `response.sh` can shape the final `output`, `usage` and `status`.
  Everything is validated against the SDK's types before a client sees it.
- **Packages are git repositories.** Every harness in an organization is a bare git repository
  with a history; the console edits files and commits, forks a package into another, exports and
  imports tar archives. Workers run the commit a session started on.
- **Organizations and teams.** An organization has owners and members; teams group members and
  own harnesses only their members can use. Providers are the organization's.
- **The catalog.** Eleven shipped packages, forked into an organization as a starting point: a
  template plus Claude Code, Codex, Hermes Agent, Pi, Cline, Kilo Code, Oh My Pi, OpenClaw,
  DeepSeek Harness and Command Code, each built from its documented CLI.
- **Conversations and the workspace.** The console runs a conversation with a harness the way a
  coding agent's web view does: streamed items, the workspace's files after each turn, a steer
  while it runs, a cancel. Rodeos run one task on several harnesses side by side and keep the
  winner you pick.
- **Sandboxing, as before.** A fresh hardened container per turn, an egress door that allows only
  the hosts the package names, provider keys that never enter a sandbox (the gateway swaps a
  session token for the key per request), uids per organization, snapshots per session.
- **Tracing.** Every request and response through the wire, every steer and cancel, and every
  provider call the gateway relayed, kept per session.
- **Distribution.** One image for every role (`ghcr.io/harnessgateway/harnessgateway`), an
  installer script, compose files for one box and a fleet, a cloud-init for workers, the
  documentation site served at `/docs`, and `serve --dev` to run the platform without Docker
  while developing a package. The model path accepts `harness/provider` for the package's
  default model.
- **The `hg` client.** Sign in with an API key; manage providers and packages (`hg harness pull`,
  `push`, `fork`, `run` to develop a package locally); ask, stream, steer, cancel; rodeos.
