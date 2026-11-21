# Extending the core

An edition is a Python package the core loads. It is named in `HG_EXTENSIONS` as `module:Class`, comma-separated,
and each class subclasses `harnessgateway.extensions.Extension`. The core is never forked or patched: if something
cannot be done through a seam, the seam is what is missing.

## The seams

| Seam | The core's own behaviour | What an edition does with it |
|---|---|---|
| `authenticate(request)` | An API key | Single sign-on; matching a caller to an organization |
| `authorize(who, action)` | Whoever is authenticated may do everything | Roles |
| `drivers()` | Docker on one host (`HG_DRIVER=docker`) | A cluster |
| `blob_stores()` | A directory (`HG_BLOBS=/data/blobs`) | Object storage (`HG_BLOBS=s3://…`) |
| `sealer(settings)` | AES-256-GCM under `HG_SECRET_KEY` | A key service |
| `before_turn(who, request)` | Nothing | Quotas and policy: raise a `Refused` to refuse the request |
| `on_event(name, about)` | Nothing | An audit log; metering |
| `routes(api)` | None | Its own API, authenticated and authorized like the core's |
| `migrations()` | None | Its own tables, and policies on the core's |
| `console()` | None | A folder of files served with the console's own. Its `edition.js` names the pages it adds to the sidebar; a file of any other of the console's names takes that file's place (a sign-in page, a stylesheet) |

Every route names what it does as `noun.read` or `noun.write` (`responses.write`, `providers.read`, `keys.write`,
…): that string is what `authorize` is asked about.

`on_event` hears `turn.finished` (with `target`, `session_id`, `model`, `status`, `reason`, `cpu_seconds`,
`memory_byte_seconds`) and every change: `providers.applied`, `environments.deleted`, `harnesses.applied`,
`keys.created`, `keys.revoked`, `sessions.deleted`. Each carries `org_id`, `at`, `by` and `target`, and never a
secret. An extension that fails on an event is logged; it never fails what it was told about.

## An example

`tests/integration/edition.py` is a complete extension that uses every seam, and
`tests/integration/test_extensions.py` loads it the way an installation does. In short:

```python
from harnessgateway.domain.principal import Principal
from harnessgateway.errors import Forbidden
from harnessgateway.extensions import Extension


class Enterprise(Extension):
    async def authenticate(self, request):
        user = await my_sso(request)                       # None: the next way is tried, the last an API key
        return Principal(user.org_id, user.id) if user else None

    async def authorize(self, who, action):
        if action not in await my_roles(who):
            raise Forbidden(f"{who.subject} may not {action}")

    async def on_event(self, name, about):
        await my_audit_log(name, about)

    def migrations(self):
        return [Path(__file__).parent / "schema"]
```

## Pages in the console

The console is plain JavaScript modules, served from `/console`. An edition's `console()` folder is served at the
same paths, and the console loads its `edition.js`:

```js
import { get } from "./api.js";                  // the console's own modules are beside it
import { add, clear, h, top } from "./ui.js";

async function audit(main) {                     // draws the page at #/audit
  add(clear(main), top("Audit", "Every change."), (await get("/v1/audit")).data.map(row => h("div", {}, row.name)));
}
export const pages = [{ group: "Activity", id: "audit", label: "Audit", icon: "M5 4h14v16H5z", page: audit }];
```

A new organization is given the core's defaults by `harnessgateway.defaults.give(platform, org_id)`: an edition
that makes organizations calls it, or gives its own.

## What stays the core's

The four contracts in [ARCHITECTURE.md](ARCHITECTURE.md): the wire format, the three definitions, the harness
package and the sandbox. An extension builds around them and never changes what they mean.

## Writing a driver

A driver implements `harnessgateway.driver.base.Driver`: keep the folders a plan names, move a session's files
between them and a `ChunkStore`, run a `SandboxPlan`, and say what it used. A plan is plain data and says nothing
about how to run it. The Docker driver is about 200 lines and is the reference.

## Many organizations

The core has one. Every row already carries `org_id` and every transaction sets `hg.org_id`, so an edition adds
three things and changes none: how organizations are made (`routes`), how a caller is matched to one
(`authenticate`), and row-level policies on that setting (`migrations`).
