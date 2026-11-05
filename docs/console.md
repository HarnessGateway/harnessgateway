# The console

One web console on the public API, served by one small server (`console/server.py`) that holds **no key**: people sign
in with their platform account (SPEC §9.3), the sign-in token lives in an HttpOnly cookie, and every call the page
makes is relayed to the API with that sign-in as the bearer and the chosen project as `HG-Project`. The platform decides
what each person may do; the pages only hide what would be refused. The self-hosted install starts it on port 8800.

| | Address | Who | What |
|---|---|---|---|
| **Organization console** | `http://host:8800/` | every account | Setup: Harnesses, Providers, Egress · Workbench: Chat, Playground · Access: API keys (admins: Projects, Accounts) · Observability: Sessions, Usage (admins: Audit log) |
| **Platform console** | `http://host:8800/ops` | platform admins | Health, Nodes & pools, Organizations, Platform usage, Versions, Configuration, Audit log |

A **developer** works in the projects it belongs to: the header's project switcher picks one, and Harnesses, Chat,
Playground, API keys, Sessions and Usage are that project's. Providers and Egress are read-only for a developer. An
**admin** sees the organization: the switcher offers *All projects* (harnesses, sessions and keys of every project,
each naming its project) or one project (to write, edit and install harnesses, which belong to one). A **platform
admin** (a flag on the account, set by `init` for the first admin or by another platform admin) also gets the
platform console.

## Sign-in

`harnessgateway init` (run by `install.sh`) creates the first admin from `HG_ADMIN_EMAIL` and prints its **invitation
link** once: `http://host:8800/setup?token=hgi_…`. The link opens the console's setup page, where the person chooses a
password (at least 12 characters) and is signed in; it works once and expires in seven days. Admins invite accounts
under *Access → Accounts* the same way (the link is shown once), put developers in projects under *Projects*, and reset a
password (*Reset password*: every sign-in of the account ends and a new link is shown). There is no default password
anywhere. Ten failed sign-ins lock an account for fifteen minutes. A sign-in lasts twelve hours and ends at sign-out,
at a password change, or when an admin disables the account. Every sign-in, failure and lock is in the audit log.

By hand, without compose:

```bash
python3 console/server.py --api http://localhost:8000 --host 0.0.0.0 --state /var/lib/harnessgateway-console
```

The server needs Python 3.12 and `httpx`. The state directory holds drafts (below), per project, and nothing else.

## Pages

The modules follow a first sign-in: set it up, try it, let applications in, watch what it does.

**Setup.** *Harnesses*: the project's installed packages and your drafts, one card each. One harness is in every
project: `harness-package-builder`, Claude Code seeded with the skill that writes packages, installed when the project
is created. Every other harness is one someone wrote: **New harness** copies the template (the `shell` package,
`HG_TEMPLATE_DIR`) into a draft under the name you give, and the draft editor (file tree, editor, add and delete files;
`install` and `run` are saved executable) is where you make it a CLI's harness; or you ask the builder in the chat for
a harness for a CLI, and **New harness from workspace**, on the builder session's page only, turns the folder it wrote
into a draft. **Install** packs the draft and installs it into the project through the platform, which validates it
like any upload; a harness of that name is upgraded in place, and the draft is gone. Open a harness to read every file
of the package as installed, its manifest, settings, secrets (write-only, "set" marks, clear) and its default provider
and model; **Edit** opens it as a draft. *Providers*: the organization's model endpoints, shared by every project:
API shape, base URL, key (write-only; leave it empty when editing to keep the stored one). *Egress*: the
organization's allow and deny entries, and what each harness adds.

**Workbench.** *Chat* is `console/chat` embedded, in the chosen project; attachments go with a message. Sending while
the agent works interrupts it (`POST /v1/responses/{id}/cancel`) and continues from where it stopped. **Rewind** on one
of your messages takes it back to edit and resend; **Branch** on a reply opens a new chat that continues from it. Both
send the next message with `previous_response_id` naming the earlier reply, and the platform forks the session from the
state saved after it; the earlier branch keeps its session under Sessions. Picking another harness continues the chat
on it, in a new session with the workspace and the conversation so far. *Playground* is for one request: pick the
harness, the provider and the model as in the chat, edit the Responses request, send it (streamed or not), and read
the events and the final Response beside it.

**Access.** *API keys*: create (shown once, with a copy-to-clipboard button) and revoke. A **run-only** key of a project
is what an application holds: it sends requests and reads the sessions it made; a **developer** key does everything a
developer does in the project; an **admin** key (admins only), everything in the organization. A key may expire.
*Projects* (admins): create, rename, delete (refused while it has sessions), and the developers of each. *Accounts*
(admins): invite, make admin or developer, reset the password, disable and enable; a platform admin may grant the
platform scope.

**Observability.** *Sessions* lists every session of the project (or, for an admin looking at all projects, of the
organization) with its harness, turns, state, saved size, last turn, who created it (an account, or a key by its label)
and, when the application sent one, its end user (`user` in the request). Sessions is observability only; it starts no
conversation. Opening one shows the session: a header with its harness, model, state and workspace size; the
**transcript**, one block per response with the user's input, the agent's thinking and steps (commands with their
output and exit code, file changes with their diffs, MCP and tool calls) and its reply, streamed live while a turn runs;
under each block what the platform measured (duration, tokens, provider calls, door connections, exit code, ignored
fields) with the raw response and trace records a click away; and beside it the **workspace**, the chat's file panel
for this session (files as of the last save, refreshed when a turn's save lands, every kind rendered). From there:
cancel a running turn, delete. *Usage*: the five meters by day, project, harness, session or end user. *Audit log*
(admins): every change of standing in the organization, newest first.

**Platform.** *Health*: database and object store checks, nodes and slots, queue depth and age, turns and failures by
reason over 24 hours. *Nodes & pools*: every node with pool, slots, warm boxes and heartbeat; mint a join token with
the `docker run` line for a new host; drain. *Organizations*: capacity (vCPUs, memory, ceilings, idle TTL, retention,
observability, node pool), egress, suspend. *Platform usage*: all organizations by org or day. *Versions*,
*Configuration* (settings as running, secrets omitted), *Audit log* (every organization's and the platform's own).
Prometheus scrapes the same numbers at `GET /internal/metrics` with the operator key.

## Security

The server holds no credential of its own: the sign-in cookie is HttpOnly and the page only ever calls the server's own
routes, which relay as the signed-in person. JSON requests are accepted from the page's origin only. The chat's
workspace files are served sandboxed in an origin of their own (see `console/chat/README.md`), and its caches are kept
per person. The pages' Content-Security-Policy allows scripts from the server only. Secrets (provider keys, harness
secrets, API keys, invitation links) are shown once at creation and never again; the API never returns them.

## Layout

```
console/server.py             sign-in cookies, the project switch, drafts, the relay to the platform, the chat UI under /chat/
console/static/               app.js (shell, router, project switcher), api.js, ui.js, pages_user.js, pages_ops.js, session.js,
                              app.css, index.html (/), ops.html (/ops), login.html + login.js (/login, /setup)
console/chat/                 the chat, usable on its own or embedded
```
