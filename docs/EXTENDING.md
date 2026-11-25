# Extending the core

An edition is the core plus one or more **extensions**. An extension is a Python package installed into the
core's image, with one class that subclasses `harnessgateway.edition.Extension` and overrides the seams it uses.
`HG_EXTENSIONS` names the classes as `module:Class`, comma-separated, in order; an edition's image sets it, so
nothing outside the image has to. The core is never forked or patched: if something cannot be done through a seam,
the seam is what is missing. [SPEC.md](../SPEC.md) section 15 is the whole of it; this page is the short form.

## What an extension may rely on

- **`harnessgateway.edition`** is everything of the core an extension imports. Anything else may move.
- **`harnessgateway.testing`** is what its tests are written with: a stand-in sandbox with real folders, the
  smallest package, a platform on a test database, and `run_next`, which runs the next waiting turn as a runner
  does.
- **An extension that stands on another finds it by name**: `platform.extensions.named("enterprise")`. The one
  below is named first in `HG_EXTENSIONS`, so it is asked first at every seam, and is started first.
- **The console's `lib/` and `components/`** are what its pages import.
- It has a `name` (`enterprise`) and a `version`. Its name owns its schema files, its console folder
  (`/console/x/<name>/`), and its line in `GET /v1/platform`. Its tables are named `<name>_…`, and every row of
  an organization's carries `org_id`.
- Its own settings are read when it is made, before the database is open; a wrong one raises `SettingsError`, and
  the process stops, as for the core's.
- Nothing it needs lives in one process's memory: several `serve` processes run the same extensions.

## The seams

Extensions are a chain, so every seam says what several of them together mean.

| Seam | The core alone | With several extensions |
|---|---|---|
| `drivers()` | Docker on one host (`HG_DRIVER=docker`) | All offered, by name. A name offered twice is refused at start |
| `blob_stores()` | A directory (`HG_BLOBS=/data/blobs`) | All offered, by URL scheme (`s3://…`). A scheme offered twice is refused |
| `sealer(settings)` | AES-256-GCM under `HG_SECRET_KEY` | The first that gives one. With one, `HG_SECRET_KEY` is not needed |
| `migrations()` | None | Each extension's one folder, applied after the core's, recorded under its name |
| `start(platform)` | Nothing | Each, in order, once the schema is up to date and before anything is served |
| `authenticate(request)` | An API key | The first that answers; the API key is asked last |
| `authorize(who, action)` | Whoever is authenticated may do everything | Every one is asked; any may refuse (`Forbidden`) |
| `before_turn(who, request)` | Nothing | Every one is asked; any may refuse (`Refused`) |
| `egress(org_id, policy)` | The environment's policy, as it is | Each narrows what the one before returned: more denied, never more allowed |
| `on_change(conn, change)` | Nothing | Each, in order, inside the change's own transaction |
| `on_event(name, about)` | Nothing | Each is told separately; one that fails is logged and fails nothing else |
| `housekeep(platform, elapsed_s)` | Nothing | Each, separately, at the end of every housekeeping round |
| `routes(api)` | None | All added after the core's. A path and method that already has a route is refused |
| `console()` | None | Each extension's folder, under `/console/x/<name>/` |

Every route names what it does as `noun.read` or `noun.write` (`responses.write`, `providers.read`, …): that is
what `authorize` is asked about.

## Changes and events

A **change** is something someone, or the platform itself, did to what an organization has. `on_change` is called
inside the transaction that makes it, with that transaction's connection: what an extension writes with it is kept
with the change, or neither is. Raising undoes the change and refuses the request. That is what an audit log is
built on.

| `name` | `target` | `about` |
|---|---|---|
| `providers.applied`, `environments.applied`, `harnesses.applied`, and `.deleted` of each | The definition's name | |
| `keys.created`, `keys.revoked` | The key's id | Its `name`, when created |
| `responses.created`, `responses.cancelled`, `responses.deleted` | The response's id | `session` and `model`, when created |
| `sessions.deleted` | The session's id | |
| `files.created`, `files.deleted` | The file's id | `filename` and `bytes`, when created |

`by` is the caller's subject, `host` for what `harnessgateway init` does, or `platform` for what the platform does
by itself when it starts. A turn that was not to be stored is no change of anyone's.

An **event** is what happened by itself. It is told after it happened, cannot be refused, and may be missed:
`turn.finished` (who sent it, how it ended, the tokens its harness reported, and what its sandbox used) and
`egress.connection` (each connection a sandbox made or was refused: host, port, why, and the bytes).

## An example

`tests/integration/edition.py` is a complete extension that uses every seam, and `tests/integration/second.py` is
one loaded after it. `tests/integration/test_extensions.py` loads both the way an installation does. In short:

```python
from harnessgateway.edition import Change, Connection, Extension, Forbidden, Platform, Principal


class Enterprise(Extension):
    name, version = "enterprise", "1.0.0"

    async def start(self, platform: Platform) -> None:
        self.platform = platform                           # where an extension is handed the platform

    async def authenticate(self, request):
        user = await my_sso(self.platform, request)        # None: the next way is tried, the last an API key
        return Principal(user.org_id, user.id) if user else None

    async def authorize(self, who: Principal, action: str) -> None:
        if not await my_roles(self.platform, who, action):
            raise Forbidden(f"{who.subject} may not {action}")

    async def on_change(self, conn: Connection, change: Change) -> None:
        await conn.execute("INSERT INTO enterprise_audit (org_id, name, by, target) VALUES (%s, %s, %s, %s)",
                           [change.org_id, change.name, change.by, change.target])

    def migrations(self):
        return Path(__file__).parent / "schema"
```

## Pages and sign-ins in the console

The console is plain JavaScript modules (the repository's `console/`). An extension's `console()` folder is served
under `/console/x/<name>/`, and its `edition.js` exports what it adds:

```js
import { top } from "../../components/layout.js";     // the console's own modules
import { get } from "../../lib/api.js";
import { add, clear, h } from "../../lib/dom.js";

async function audit(main) {                           // draws the page at #/audit
  add(clear(main), top("Audit", "Every change."), (await get("/v1/audit")).data.map(row => h("div", {}, row.name)));
}
export const pages = [{ group: "Activity", id: "audit", label: "Audit", icon: "M5 4h14v16H5z", page: audit }];
export const signIns = [{ label: "Sign in with single sign-on", href: "/v1/sso/start" }];
```

A page is listed in the sidebar under its `group` and drawn at `#/<id>`; an `id` that is taken is refused. A
sign-in is a link on the sign-in form: it leads away, and comes back to `/console/#credential=<token>`, which the
console takes, removes from the address, and sends as `Authorization: Bearer …` from then on. A module may also
export `signOut`, a function the console calls before it forgets the credential. No file of an extension is served
in place of one of the console's.

## Many organizations

The core has one. Every row already carries `org_id` and every transaction on an organization's rows sets
`hg.org_id`, so an extension adds three things and changes none: how organizations are made (`routes`, then
`give(platform, org_id, by=…)` for what one starts with), how a caller is matched to one (`authenticate`), and
row-level policies on that setting (`migrations`).

## Writing a driver

A driver implements `Driver`: keep the folders a plan names, move a session's files between them and a
`ChunkStore`, run a `SandboxPlan`, and say what it used. A plan is plain data and says nothing about how to run
it. The Docker driver is about 200 lines and is the reference.

## Holding an extension to the core

Run the core's own integration tests with the extension loaded: `HG_TEST_EXTENSIONS=module:Class`, or several,
in order, with commas between them. An extension that changes what the core does fails there.
