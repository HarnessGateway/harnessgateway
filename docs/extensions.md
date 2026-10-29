# The extension seam

Self-hosted HarnessGateway is one organization. A SaaS adds tenants, users, plans, billing and its own observability
pipeline on top of the same core, without forking it, through `harnessgateway.extensions.Extension`
([`SPEC.md`](../SPEC.md) §13).

```python
# saas/platform.py
from fastapi import APIRouter, Request
from harnessgateway.extensions import Admission, Extension, TraceRecord, TurnSummary, UsageRecord
from harnessgateway.errors import Refused
from harnessgateway.services.principal import Principal


class Saas(Extension):
    migrations = "saas.migrations"                       # NNNN_*.sql files, applied after the core's

    def bind(self, platform):                            # the core's services: orgs, keys, harnesses, usage, traces…
        self.platform = platform

    def router(self) -> APIRouter:                       # extra routes under the same app
        r = APIRouter(prefix="/v1/console")
        ...
        return r

    async def authenticate(self, request: Request) -> Principal | None:
        ...                                              # a session cookie, a JWT; None → the next extension, then API keys

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

Every table carries `org_id` with row-level security; the core's services take the organization from the
`Principal`. An extension creates organizations with `platform.orgs.create(name)` and sets each one's capacity
(`platform.orgs.update(org_id, capacity=…)`: vCPUs, memory, sandbox ceilings, idle and retention, node pool,
observability level). API keys stay the core's (`platform.keys`); a SaaS may issue its own credentials through
`authenticate` instead.

## Metering and billing

Usage records are append-only, idempotent by id, with the organization, meter, quantity, kind and interval; the
`meter` hook receives each batch for a billing pipeline, and `GET /v1/usage` sums them for the organization. Tokens
are not metered: the organization pays its provider directly through its own key.

## Observability

`observe` receives every record as it is written: the harness exchange (request as delivered, events, logs, timings,
exit; bodies when the organization's observability is `full`), the gateway exchange (method, path, host, status,
sizes; bodies when `full`) and the door exchange (host, port, verdict, bytes). No record ever holds a secret value.
