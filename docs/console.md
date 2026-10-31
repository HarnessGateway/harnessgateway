# The consoles

Two web consoles on the public API, served by one small server (`console/server.py`) that holds the keys so
the browser never sees one. The self-hosted install starts it on port 8800.

| | Address | Who | What |
|---|---|---|---|
| **Organization console** | `http://host:8800/` | `admin` and `member` users | Setup: Providers, Harnesses, Egress · Workbench: Chat, Playground · Access: API keys, Members · Observability: Sessions, Usage (members see Workbench only) |
| **Deployer console** | `http://host:8800/ops` | `deployer` users | Operations: Health, Nodes & pools, Organizations, Platform usage, Versions, Configuration, Audit log. Account: Deployer users |

They mirror the platform's two authorities: the organization console uses the organization's API key (an `admin`
key for admins; a `run`-scoped key the server creates once for members), the deployer console uses the operator key
for `/internal` reads and actions. With the SaaS extension the same pages gain more rows and buttons (organizations,
plans, billing); nothing moves between consoles.

## Sign-in

`install.sh` creates two users, `deployer` and `admin`, with passwords shown once; each must change it at first
sign-in. Admins add members (and other admins) under *Members*; deployers add deployer users under *Deployer users*.
A new user's initial password is shown once. Users live in the console's state directory (`console` volume with
compose), hashed with scrypt; a sign-in is a signed cookie valid for 12 hours. Every write — sign-ins, user changes,
every platform call that changes something, every chat turn — is in the audit log (deployer console).

By hand, without compose:

```bash
HG_KEY=hg_… HG_OPERATOR_KEY=hgo_… python3 console/server.py --api http://localhost:8000 --host 0.0.0.0 \
  --state /var/lib/harnessgateway-console          # first start prints the two initial users; or run `… init` first
```

Without `HG_OPERATOR_KEY` the deployer console is off. The server needs Python 3.12 and `httpx`.

## Pages

The modules follow a first sign-in: set it up, try it, let applications in, watch what it does.

**Setup.** *Providers*: API shape, base URL, key (write-only). *Harnesses*: the installed packages and your
drafts. Open a harness to read every file of the package as installed (base and overlay layers), its manifest,
settings, secrets (write-only, "set" marks, clear) and its default provider and model. **Fork** copies a package's
files into a draft you own; **New harness** starts a draft from a template: an *overlay* on a base harness (just
`harness.json` and `seeds/` — your instruction files, skills, hooks, MCP catalog) or a *base package* for a new
CLI. A draft is edited in the console (file tree, editor, add and delete files) and **Install** packs it and installs
it through the platform, which validates it like any upload. *Install from git* installs a package kept in a
repository. *Egress*: the organization's allow and deny entries, and what each harness adds.

**Workbench.** *Chat* is `console/chat` embedded; attachments go with a message. *Playground* sends a raw
Responses request (streamed or not), shows the events as they arrive and the final Response, and renders the same
call as curl, Python or TypeScript.

**Access.** *API keys*: create (shown once), revoke. *Members*: console users of the organization and their role.

**Observability.** *Sessions* lists every session of the organization with its harness, turns, state, saved size and
last turn and the client that created it, whichever application that was: the chat, your own application on the
API, an agent of your own. Sessions is observability only; it starts no conversation. Opening one shows the session: a header with
its harness, model, state and workspace size; the **transcript**, one block per response with the user's input, the
agent's thinking and steps (commands with their output and exit code, file changes with their diffs, MCP and tool
calls) and its reply, streamed live while a turn runs; under each block what the platform measured (duration,
tokens, provider calls, door connections, exit code, ignored fields) with the raw response and trace records a click
away; and beside it the **workspace**, the chat's file panel for this session (files as of the last save, refreshed
when a turn's save lands, every kind rendered). The header names the client that created the session (the API key's label). From there: cancel a running turn, delete.
*Usage*: the five meters by day, harness or session.

**Operations.** *Health*: database and object store checks, nodes and slots, queue depth and age, turns and
failures by reason over 24 hours. *Nodes & pools*: every node with pool, slots, warm boxes and heartbeat; mint a
join token with the `docker run` line for a new host; drain. *Organizations*: capacity (vCPUs, memory, ceilings,
idle TTL, retention, observability, node pool), egress, suspend; "Open as admin". *Platform usage*: all
organizations by org or day. *Versions*, *Configuration* (settings as running, secrets omitted), *Audit log*.

## Security

The server holds the keys and talks to the API; the page only ever calls the server's own routes. JSON requests are
accepted from the page's origin only. The chat's workspace files are served sandboxed in an origin of their own (see
`console/chat/README.md`). The pages' Content-Security-Policy allows scripts from the server only. Secrets (provider
keys, harness secrets, API keys, initial passwords) are shown once at creation and never again; the API never returns
them.

## Layout

```
console/server.py             sessions, users, drafts, the relay to the platform, the chat UI mounted under /chat/
console/static/               app.js (shell, router), api.js, ui.js, pages_user.js, pages_ops.js, app.css,
                              index.html (/), ops.html (/ops), login.html + login.js
console/chat/                 the chat, usable on its own or embedded
```
