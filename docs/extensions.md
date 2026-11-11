# The extension seam

Self-hosted HarnessGateway is one organization. More is built on top of the same core, without forking it, through
`harnessgateway.extensions.Extension` ([`SPEC.md`](../SPEC.md) §13): organizations beyond the first, credentials of a
deployment's own, admission rules, a launcher for another substrate, and a pipeline for usage and observability
records. What is built there lives in its own repository; this page is what the core offers it.

```python
# my_extension/platform.py
from fastapi import APIRouter, Request
from harnessgateway.extensions import Admission, Extension, TraceRecord, TurnSummary, UsageRecord
from harnessgateway.errors import Refused
from harnessgateway.domain.access import Principal
from harnessgateway.ports.launch import BoxSpec, Launcher


class Mine(Extension):
    migrations = "my_extension.migrations"               # NNNN_*.sql files, applied after the core's

    def bind(self, platform):                            # the core's services: orgs, accounts, keys, harnesses, boxes, usage…
        self.platform = platform

    def router(self) -> APIRouter:                       # extra routes under the same app
        r = APIRouter(prefix="/v1/mine")
        ...
        return r

    def launcher(self, settings) -> Launcher | None:     # boxes as tasks of a container service instead of Docker
        return MyTasks(settings)

    async def authenticate(self, request: Request) -> Principal | None:
        ...                                              # a credential of your own; None → the core's keys and sign-ins

    async def admit(self, principal: Principal, turn: Admission) -> None:
        if await self.over_limit(turn.org_id):
            raise Refused("over this organization's limit")

    async def observe(self, record: TraceRecord) -> None: ...     # every harness / door / provider record
    async def meter(self, records: list[UsageRecord]) -> None: ...   # every usage batch, after the core stores it
    async def turn_ended(self, summary: TurnSummary) -> None: ...    # status, reason, usage, timings
```

Enable it with `HG_EXTENSIONS=my_extension.platform:Mine` on every role (install the package into the image). Hooks run
in the order configured; `observe`, `meter` and `turn_ended` run after the core's own write and never fail a turn
(their exceptions are logged).

## Pages in the console

The console is an application on the public API, and it loads page modules of the deployment's own beside its pages:
ship them with the extension and set `HG_CONSOLE_PAGES` to their directory on the console
([console](console.md#pages-of-your-own)). A page calls the extension's routes through the console's relay, as the
signed-in person.

## A launcher

A `Launcher` starts a box from the sandbox image with the four values of `BoxSpec.env(api_url, door)` (`HG_BOX_ID`,
`HG_BOX_TOKEN`, `HG_API_URL`, `HG_DOOR`) and the spec's cpu, memory, tmp and pid limits; stops it by the reference it
returned; says whether it is alive; reaps what finished boxes left. The box must reach the door's address and
nothing else; the runner inside does the rest. The core's Docker launcher is the reference (`adapters/launch_docker.py`).

## Tenancy

Every table carries `org_id` with row-level security; the core's services take the organization from the `Principal`
(SPEC §10.5: organization, actor, role, `run_only`, `platform_admin`). An extension creates an organization on the
core's identity: the organization with `platform.orgs.create(name)`, the builder with
`platform.harnesses.install_folder(org_id, platform.settings.builder_dir)`, its first admin with
`platform.accounts.create(org_id, actor, email, name, "admin")` (the invitation is mailed when mail is configured, and
returned), and its limits with `platform.orgs.update(org_id, limits=…)` (vCPUs, memory, /tmp, processes, concurrent
turns, standby, turn timeout, retention, observability level). Accounts, roles, sign-ins, keys, SSO and audit stay the
core's; `authenticate` is for a credential of the extension's own, which still resolves to a `Principal` of an
organization.

## Metering

Usage records are append-only, idempotent by id, with the organization, meter, quantity, kind and interval; the
`meter` hook receives each batch, and `GET /v1/usage` sums them for the organization. `agent_seconds` of kind `turn`
and `storage_gib_hours` are what a customer sees; `cpu_seconds`, `memory_gib_seconds` and the byte meters are the
operator's cost record. Tokens are not metered: the organization pays its provider directly through its own key.

## Observability

`observe` receives every record as it is written: the harness record (outcome, exit code, timings, the number of
events, byte counts, CPU and peak memory, and a 16 KiB tail of the log lines and stderr; the whole stdout and stderr
as objects when the organization's observability is `full`), the door exchange (host, port, verdict, bytes per
connection) and each intercepted provider connection (method, path, status, bytes, duration). No record ever holds a
secret value, and none holds a request or response body.
