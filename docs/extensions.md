# The extension seam

Self-hosted HarnessGateway is one organization. A SaaS adds tenants, users, plans, billing and its own observability
pipeline on top of the same core, without forking it, through `harnessgateway.extensions.Extension`
([`SPEC.md`](../SPEC.md) §13).

```python
# saas/platform.py
from fastapi import APIRouter, Request
from harnessgateway.extensions import Admission, Extension, TraceRecord, TurnSummary, UsageRecord
from harnessgateway.errors import Refused
from harnessgateway.domain.access import Principal


class Saas(Extension):
    migrations = "saas.migrations"                       # NNNN_*.sql files, applied after the core's

    def bind(self, platform):                            # the core's services: orgs, keys, harnesses, usage, traces…
        self.platform = platform

    def router(self) -> APIRouter:                       # extra routes under the same app
        r = APIRouter(prefix="/v1/console")
        ...
        return r

    async def authenticate(self, request: Request) -> Principal | None:
        ...                                              # a JWT, a session of its own; None → the core's keys and sign-ins

    async def admit(self, principal: Principal, turn: Admission) -> None:
        if await self.over_quota(turn.org_id):
            raise Refused("quota exceeded for this plan")

    async def observe(self, record: TraceRecord) -> None: ...     # every harness / gateway / door record
    async def meter(self, records: list[UsageRecord]) -> None: ...   # every usage batch, after the core stores it
    async def turn_ended(self, summary: TurnSummary) -> None: ...    # status, reason, usage, timings
```

Enable it with `HG_EXTENSIONS=saas.platform:Saas` on the API and gateway (install the package into the image). Hooks
run in the order configured; `observe`, `meter` and `turn_ended` run after the core's own write and never fail a turn
(their exceptions are logged).

## Tenancy

Every table carries `org_id` with row-level security, and the project-scoped ones (harnesses, sessions, turns) carry
`project_id`; the core's services take both from the `Principal` (SPEC §9.2: organization, actor, role, project,
`run_only`, `platform_admin`). A SaaS signs customers up on the core's identity: it creates the organization with
`platform.orgs.create(name)`, its default project with `platform.projects.create(org_id, actor, "default")` (the
builder is installed), its first admin with `platform.accounts.create(org_id, actor, email, name, "admin")` (an
invitation to send), and sets the organization's capacity (`platform.orgs.update(org_id, capacity=…)`: vCPUs, memory,
sandbox ceilings, idle and retention, node pool, observability level). Accounts, roles, sign-ins, keys and audit stay
the core's; `authenticate` is for a credential of the SaaS's own (a JWT from its identity provider), which still
resolves to a `Principal` of an organization and a project.

## Metering and billing

Usage records are append-only, idempotent by id, with the organization, meter, quantity, kind and interval; the
`meter` hook receives each batch for a billing pipeline, and `GET /v1/usage` sums them for the organization. Tokens
are not metered: the organization pays its provider directly through its own key.

## Observability

`observe` receives every record as it is written: the harness exchange (request as delivered, events, logs, timings,
exit; bodies when the organization's observability is `full`), the gateway exchange (method, path, host, status,
sizes; bodies when `full`) and the door exchange (host, port, verdict, bytes). No record ever holds a secret value.
