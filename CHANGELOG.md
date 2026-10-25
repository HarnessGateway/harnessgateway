# Changelog

Every release is a section here, newest first, and the newest section's heading is the version.
A merge to `main` that adds a section releases it: the release workflow tags that version, builds
the wheel and the image, and publishes the section as its notes.

## [9.0.0]

The platform, pared down to the multi-tenant platform for organizations: the same product managed
and self-hosted, an app of one page, and one rule for a busy session. What was taken out was not
broken; it was more than the product is.

- **One app page.** `/app` is sign-in, then one page: the sessions on the left, the conversation on
  the right, and a setup page for providers and harnesses. Add a provider, add a harness (from the
  catalog, or upload an archive), start a session with the harness of your choice, type, press
  Enter. The console's other pages (the package editor, the catalog browser, teams, members, keys,
  usage, organization settings, the deployer's dashboard) are gone; what they did is the API's and
  `hg`'s.
- **The next message wins.** There is no steer. A `POST /v1/responses` with `previous_response_id`
  naming a session that is running a turn supersedes that turn: it is stopped gracefully, ends
  `cancelled` with `metadata.incomplete_reason: superseded`, and the new turn runs when it has
  ended, on the workspace it left. A turn still queued ends at once. The trace records the
  supersede. `POST /v1/sessions/{id}/interrupt`, `hg steer`, `harness.json`'s `interrupt`,
  `$HG_INTERRUPT` and `$HG_DIR/interrupt` are gone; the package contract is **8**, and a contract-7
  package still loads and runs (its `interrupt` is ignored). The catalog packages are on 8.
- **Removed:** teams (every harness is the organization's; owners change them, members run them;
  a sandbox's uid is the organization's), rodeos, webhooks, metering and usage, plans, trace
  sharing and the dataset, billing, mail (no email verification or password reset), identity
  providers (GitHub, Google, OpenID Connect), the `usage` and `platform/usage` routes, `PATCH
  /v1/harnesses/{name}`. Settings `HG_TRACE_SHARING`, `HG_BILLING`, `HG_STRIPE_KEY`, `HG_SMTP_URL`,
  `HG_MAIL_FROM`, `HG_GITHUB_*`, `HG_GOOGLE_*` and `HG_OIDC_*` are gone; `harnessgateway doctor`
  checks records, key management, blobs, events and the sandbox.
- **Kept, as it was:** organizations with ceilings and a status, members and invitations, keys,
  providers sealed and swapped at the gateway, harnesses as git repositories with a history,
  nodes, sessions with snapshots, traces and captures, the Responses wire with streaming,
  background turns, cancel, closing the request, `Idempotency-Key` and the rate ceiling, the
  catalog, `hg`, the backends (SQLite or Postgres, a directory or S3, a local master key or a KMS),
  the deployer's organization and fleet routes. A 7.1 data directory still boots; a 9 reads what
  earlier releases kept and ignores what it no longer has (`tests/test_upgrade.py`).
- The image is `:9`.

## [8.0.0]

The platform, rebuilt for production around the bet: harness packages are an organization's
assets, the platform provides the sandboxed execution and the common wire, the managed deployment
and a self-hosted one are the same product, and what is metered is compute, egress and storage,
never tokens. Any CLI runs the same way a coding agent does. A 7.1 data directory boots unchanged.

- **The tree is layered and every swappable part is a backend.** `kernel` (execution, files, the
  vault, ids, limits), `contract` (the package, the wire, the turn), `backends` (one Protocol per
  directory, one module per implementation), `domain`, `services`, `api`. `tests/test_layers.py`
  holds the import rule; `tests/conformance/` runs one suite per protocol over every implementation.
  New backends: events (records polling, Postgres `NOTIFY` for followers across replicas), key
  management (a local master key, an AWS KMS envelope), mail (none, SMTP), billing (none, Stripe
  meter events), identity (GitHub, Google, any OpenID Connect issuer). `config.Settings` names each;
  `harnessgateway doctor` tries each one named and prints a line per backend.
- **Metering.** A turn's usage is the compute the sandbox reserved for its wall time (`cpu_seconds`,
  `memory_gb_seconds`), the bytes it sent out through the door and the gateway (`egress_bytes`),
  the snapshot it wrote, and the tokens the provider counted (shown, never billed). Storage is
  sampled daily from blob sizes. `GET /v1/usage` is the organization's view (an owner's whole, a
  member's own and their teams'), with rows per day, harness, team, member and session and the
  meaning of each meter; the deployer's platform-wide view is `/v1/platform/usage` and
  `/v1/orgs/{name}/usage`. With `HG_BILLING=stripe`, every reading reaches the billing system for
  the organization's `billing_id`.
- **Plans.** `free`, `team` and `enterprise` bring their ceilings (`requests_per_minute` is new);
  the deployer sets the plan and any ceiling. An enterprise organization never shares traces.
- **Trace sharing and the dataset.** An owner turns sharing on or off for the organization; it is
  dated, so only sessions from then on are in it, and turning it off ends it at once. With
  `HG_TRACE_SHARING=1`, organizations that sign up on the free plan share until an owner says
  otherwise, and the sign-up page says so. `GET /v1/dataset` exports, one JSON line per session,
  the request, the response, the steers, the usage numbers and the provider captures verbatim, with
  the organization, member and team replaced by pseudonyms that mean nothing off this deployment.
  `docs/data.md` says what is kept, metered and shared.
- **The API for agents.** `Idempotency-Key` makes a retried request return its sender's first turn
  for a day. Past the organization's rate ceiling the answer is `429` with `Retry-After`, before
  anything is queued; a request refused for its own faults does not count. Owners register webhooks
  (`/v1/webhooks`) and get the signing secret once; every final response is posted to each as an
  event signed over the timestamp and the body, retried after 1, 5 and 25 seconds, then counted
  against the webhook. A queued turn waits three seconds for the node that holds its session's
  trees, then goes to any live node.
- **Any CLI.** A package whose `harness.json` has `"provider_env": {}` runs with no token and no
  gateway; the catalog's new `shell` package runs the request's input as a script in the session's
  workspace and answers with what it printed, with no provider registered at all. `docs/any-cli.md`.
- **Nodes.** The machines that run turns are nodes (the `worker` role is their process); the
  deployer's console says so. Housekeeping fails lost turns and samples storage from every `api`.
- **The console.** A Start page with the checklist to a first turn and the API snippet; Usage with
  every meter and its meaning; Organization settings with the plan, trace sharing and webhooks; an
  `api` tab in a conversation that continues the very same session from code; the deployer's
  dashboard by meter with the dataset's size, plans and trace sharing per organization, Nodes.
  Sign-in offers any configured identity provider, single sign-on included.
- **Deploy.** `deploy/compose.yml` defaults to Postgres, MinIO, two API replicas behind Caddy and a
  node that joins; `--profile one-box` is the small shape. The image is `:8`.
- **Proofs.** `tests/test_upgrade.py` boots a 7.1.0 data directory (`tests/fixtures/`);
  `tests/test_invariants.py` checks that every promise on the Security page names a test that
  exists; new promises cover the dataset, webhooks, provider-less packages, the ceiling and the
  upgrade.
- Routes moved: the deployer's `GET /v1/usage` is `GET /v1/platform/usage`. `/auth/providers`
  gains `identities`. Everything else on the wire is as in 7.1.

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
