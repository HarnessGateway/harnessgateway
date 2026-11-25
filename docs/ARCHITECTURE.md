# Architecture

## One sentence

A Responses request becomes a **turn**; a turn runs one **harness package** in one **sandbox**; what the harness
prints becomes the response.

## The four contracts

Everything else may change. These are what users, packages and editions are written against.

1. **The wire format** is the OpenAI Responses API, unmodified. A request is validated against the OpenAI SDK's own
   description of one, every event a harness prints against the SDK's model of that event, and every response
   against the SDK's response type. The only field with a meaning of the platform's own is `model`.
2. **The three definitions** (providers, environments, harnesses) share one shape: a name, a definition, and
   secrets sealed beside it. They have one set of verbs.
3. **The harness package** is two programs, `install` and `run`
   ([the example package](../packages/harness-package-builder/example/README.md)). The
   platform knows no agent CLI: everything about one is in its package. A package declares nothing: what a turn
   may reach and hold is its environment's to say.
4. **The sandbox** is what a package finds inside it: `/mind`, `/work`, `/turn`, `/harness`, the variables, the
   proxy. It has a version, which the platform reports.

## A turn, start to finish

```
POST /v1/responses ──▶ validate ──▶ resolve the model line ──▶ a row in `turns`, status queued
                                                                      │  (announced on a channel)
   a runner, in any process, claims it with a row lock ◀──────────────┘
        │
        ├─ install     the harness's package for the environment's image, once ever, held by a database lock
        ├─ restore     the session's two folders from its last snapshot, unless they already hold it
        ├─ inputs      request.json, previous.json, handover.txt, uploaded files, the startup script
        ├─ grant       what this sandbox may reach, with its secrets, kept sealed until the sandbox ends
        ├─ sandbox     one hardened container; each line it prints is an event, a log line, or the end
        ├─ save        the two folders as a snapshot: only chunks not already kept are written (unless store is false)
        └─ conclude    the response, once; the event that ends its stream
```

Whoever is waiting on the request (blocking, or on a stream) is following the turn's events in the database, so
the process that answers need not be the one that runs the turn.

## Why each part is where it is

| Part | What it does | Why it is separate |
|---|---|---|
| `domain/` | Every rule, as pure functions and data | A rule that touches nothing can be tested exhaustively and read alone |
| `store/` | PostgreSQL and blobs | One module per group of tables: a table has one owner |
| `driver/` | Folders in and out, and running a sandbox | The only code that knows the infrastructure. Docker here; a cluster elsewhere |
| `runner/` | The order of a turn's steps | It knows the steps and none of the mechanisms |
| `proxy/` | The sandbox's only way out | A process of its own, with no database and no sealing key: what faces untrusted code holds nothing worth stealing but the secrets of sandboxes running right now |
| `api/` | HTTP in, HTTP out | It validates and delegates; it decides nothing |
| `extensions.py` | The seams | So that an edition adds to the core instead of changing it |
| `packages/`, `environments/` (the repository's, beside `src/`) | The packages that ship, and the environment an organization starts with | Given through the same stores as anything an organization adds: the platform still knows no harness |
| `console/` (the repository's, beside `src/`) | The pages people use the platform through | A client of the API, served beside it: it has no route of its own, so the API is whole without it |

Dependencies point one way: `api` and `runner` use `store`, `driver` and `domain`; `store` and `driver` use
`domain`; `domain` uses nothing.

## What ships beside the program

Three of the repository's folders are not the server's code: `console/`, `packages/` and `environments/`. The last
two are on the other side of the contract. They know the platform only by the contract and import nothing of it;
the server reaches all three only through `shipped.py`, to serve the console, to keep the packages that ship at the
running version's, and to give a new organization its environment. Their tests are `tests/shipped/`, which hold
them to the contract. So they can be lifted out to a repository of their own without the server changing: only
where `shipped.py` looks.

The contract itself (how the platform runs a package, the three schemas, an example package, and a session of
requests) is inside one of those packages, `harness-package-builder`: building a package is the one thing done
from it. The packages that ship are the platform's, and every start puts each at the running version's, so the
builder an installation has always holds the contract and the schemas of the platform it runs on.

## One schema

A package has no library of the platform's to import and no line of the platform's to print. Its `run` reads the
caller's Responses request as it was sent, and prints pieces of a Responses response: an output item whole, a
stream event, and last the stream's own last event. `domain/events.py` reads those lines and does the
bookkeeping a response needs (the events' numbers, the items' places and ids, the item that text arriving by
itself is kept in), checking each against the OpenAI SDK's model of it.

"The Responses API" means one pinned version of the OpenAI SDK, named in `pyproject.toml` and reported by
`GET /v1/platform`. `domain/schema.py` writes that version as four JSON Schemas, kept beside the contract in words,
in the builder's package: a request, a response and the stream's events, which are OpenAI's own and unchanged, and
the platform's rule for one printed line. A test fails when the kept files are not what the pin gives, so a change
of the pin shows as a change of them: `harnessgateway schema` writes them. The server never reads those files: it
checks requests and printed lines against the SDK's types themselves.

The platform names no agent CLI: not in its code, not in the console, not in the contract. A test keeps that so.
What is known about any one CLI is in its package, and how packages are built is what `harness-package-builder`
is told.

## Sessions and storage

A session is a chain of turns on two folders: `/mind` (the agent CLI's own state, which is how it remembers) and
`/work` (what it is asked to work on). After every turn both are saved as a **snapshot**: a manifest of files, each
a list of chunk hashes. A chunk is kept once per organization and counts the snapshots that name it, so a turn that
changed one file costs one file, and a fork costs nothing until it changes something.

A driver keeps a session's folders warm between turns; `sessions.folder_snapshot_id` says which snapshot they hold.
When that is not the one a turn continues from (a fork, an evicted idle session, a save that failed), the driver
restores them first.

The Docker driver keeps every folder as a sub-folder of one data volume and does the file work itself. A sandbox is
given its own sub-folders with `volume-subpath`, and never the volume.

## Handover

A session may change harness from one turn to the next, and the platform hands the new one nothing that was said.
It writes the session's handover history into the turn's inputs (`handover.txt`): every stint, which is consecutive
turns on one model line, with its responses, and for each harness the note its own package left of where its memory
is (`$HG_STATE/memory`, read from the snapshot the turn continues from). The harness reads what it needs from the
mind, which every harness of a session shares.

The history is derived from the turn chain, so it is stored nowhere and follows a fork by itself. Only a package
knows where its harness keeps its record, so only a package says; the words around what it says are in
`domain/handover.py`, the one place, and are the same for every harness.

## Who keeps the conversation

Both of the Responses API's ways work. With `previous_response_id` the server keeps it: the session continues (or
forks, from an earlier response), its folders are restored, and the harness resumes its own memory. Or the caller
keeps it and sends the whole of it in `input`; with `store: false` nothing is held here at all: the turn runs in a
session of its own, no snapshot is taken, and the session is removed once the answer is given.

## Many processes

Nothing a turn needs lives in one process's memory.

- **The queue** is the `turns` table: `FOR UPDATE SKIP LOCKED` claims a turn for one runner.
- **Liveness** is a heartbeat: a turn whose runner has been silent is ended by any other runner, and what is left of
  its sandbox is discarded.
- **The stream** is the `events` table plus `NOTIFY`: any process can serve any turn's stream from any point.
- **Grants** (what a running sandbox may reach) are rows, sealed, so the proxy can ask any process.
- **Installs** are held by an advisory lock, so two processes never install the same package for the same image.
- **Housekeeping** is a round taken by whichever process asks first.

One process on one host is the community deployment. The same code runs as several.

## Organizations

Every row carries `org_id`, every blob key begins with it, and every transaction on an organization's rows sets
`hg.org_id`. The core creates one organization and has no way to make another. An edition with many adds how they
are made, how a caller is matched to one, and row-level policies on that setting; the core's queries do not change.

## Security, in order of what matters

1. **The sandbox**: read-only root, all capabilities dropped, no new privileges, a non-root user, its own limits on
   CPU, memory, processes and disk, a time limit, and gVisor when the host has it.
2. **The network**: a sandbox is on an internal network whose only other member is the proxy. The environment says
   what may be reached; private and local addresses never are, unless the operator names them.
3. **Secrets**: sealed at rest; given to the proxy only for sandboxes running now; never in a sandbox, a log, a
   response or an error. A sandbox that prints its "key" prints a placeholder.
4. **Untrusted files**: what an agent wrote is saved without following links and without running anything. A
   browser is shown it only through a link that is the permission (one session, one hour), and only sandboxed: a
   page an agent built runs in an origin of its own, and can neither read the page that shows it nor call the API.
5. **Validation**: at every boundary, with a refusal that names what was wrong.

## Usage

The platform is infrastructure, so what it measures is infrastructure: processor time and memory of every sandbox
(read from the runtime while it runs), bytes through the proxy, and bytes stored times how long. One row per
organization per day. `GET /v1/usage` sums them; an edition that bills reads the same rows.
