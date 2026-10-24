"""The organization: the tenant. It has a plan, ceilings the deployer set, a status, and a say in
whether its traces may be shared. Everything else in the platform belongs to one organization.

Plans set the default ceilings and who may share traces: an enterprise organization's traces are
never shared, whatever anyone sets. Sharing is dated: only sessions created after it was turned on
are in the dataset, and turning it off ends it at once."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..backends.blobs import Blobs
from ..backends.records import Records
from ..errors import Conflict, Invalid, NotFound
from ..kernel.ids import check_name, is_name
from . import PLATFORM
from ..kernel.limits import Limits

RESERVED = {"catalog", "template", "platform", "deployer", "api", "internal", "auth", "app", "docs", "web"}
PLANS = ("free", "team", "enterprise")
PLAN_LIMITS: dict[str, dict[str, Any]] = {           # the ceilings a plan starts with; the deployer may set others
    "free": {"max_runs": 2, "run_timeout_s": 1800, "requests_per_minute": 30, "disk_mb": 1024},
    "team": {"max_runs": 8, "requests_per_minute": 240},
    "enterprise": {"max_runs": 32, "requests_per_minute": 1200, "disk_mb": 8192},
}
STATUSES = ("active", "suspended")
KINDS = ("member", "team", "key", "provider", "harness", "session", "response", "turn", "chunk", "usage", "trace", "rodeo", "invite",
         "webhook", "idem", "meter")


@dataclass(frozen=True)
class Org:
    name: str
    created: float
    limits: dict[str, Any] = field(default_factory=lambda: Limits().as_dict())
    status: str = "active"                           # or suspended: its requests are refused
    title: str = ""
    plan: str = "free"                               # free, team, enterprise
    trace_sharing: bool = False                      # whether its traces go into the shared dataset
    billing_id: str = ""                             # the billing system's customer id, when billed
    trace_sharing_since: float = 0                   # when sharing was turned on; sessions created before stay out

    @property
    def shares(self) -> bool:
        return self.trace_sharing and self.plan != "enterprise"

    @property
    def ceilings(self) -> Limits:
        return Limits.from_dict(self.limits)

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "plan": self.plan, "status": self.status, "created": self.created,
                "trace_sharing": self.shares, "trace_sharing_since": self.trace_sharing_since if self.shares else 0, "limits": self.limits}


class Orgs:
    def __init__(self, records: Records, blobs: Blobs):
        self.records, self.blobs = records, blobs

    def create(self, name: str, limits: dict[str, Any] | None = None, title: str = "", plan: str = "free", trace_sharing: bool = False) -> Org:
        check_name(name, "organization")
        if name in RESERVED:
            raise Invalid(f"{name} is a reserved name")
        if plan not in PLANS:
            raise Invalid(f"a plan is one of {', '.join(PLANS)}")
        if self.records.get("org", PLATFORM, name):
            raise Conflict(f"organization {name} exists")
        now = time.time()
        sharing = bool(trace_sharing) and plan != "enterprise"
        o = Org(name, now, Limits.from_dict(limits if limits is not None else PLAN_LIMITS[plan]).as_dict(), title=title[:80], plan=plan,
                trace_sharing=sharing, trace_sharing_since=now if sharing else 0)
        self.records.put("org", PLATFORM, name, asdict(o))
        return o

    def get(self, name: str) -> Org:
        d = self.records.get("org", PLATFORM, name) if is_name(name) else None
        if d is None:
            raise NotFound(f"no organization {name}")
        return Org(**d)

    def active(self, name: str) -> Org:
        """The organization, if it may act."""
        o = self.get(name)
        if o.status != "active":
            raise Invalid(f"organization {o.name} is suspended")
        return o

    def list(self) -> list[Org]:
        return [Org(**d) for d in self.records.list("org", PLATFORM)]

    def sharing(self) -> list[Org]:
        """The organizations whose traces go into the dataset."""
        return [o for o in self.list() if o.shares]

    def update(self, name: str, *, limits: dict[str, Any] | None = None, status: str | None = None, title: str | None = None,
               plan: str | None = None, trace_sharing: bool | None = None, billing_id: str | None = None) -> Org:
        o = self.get(name)
        if status is not None and status not in STATUSES:
            raise Invalid("an organization's status is active or suspended")
        if plan is not None and plan not in PLANS:
            raise Invalid(f"a plan is one of {', '.join(PLANS)}")
        new_plan = plan or o.plan
        sharing = (o.trace_sharing if trace_sharing is None else bool(trace_sharing)) and new_plan != "enterprise"
        since = o.trace_sharing_since if (sharing and o.shares) else (time.time() if sharing else 0)
        if limits is not None:
            ceilings = Limits.from_dict(limits).as_dict()
        elif plan is not None and plan != o.plan:
            ceilings = Limits.from_dict(PLAN_LIMITS[plan]).as_dict()     # a new plan brings its ceilings unless others are given
        else:
            ceilings = o.limits
        o = Org(o.name, o.created, ceilings, status or o.status, (title or "")[:80] if title is not None else o.title, new_plan, sharing,
                o.billing_id if billing_id is None else billing_id[:80], since)
        self.records.put("org", PLATFORM, name, asdict(o))
        return o

    def delete(self, name: str) -> None:
        """Every record and blob of the organization; its repositories are the harnesses' to remove first."""
        self.get(name)
        for kind in KINDS:
            for d in self.records.list(kind, name):
                self.records.delete(kind, name, d.get("id") or d.get("name") or d.get("key"))
        for login in self.records.where("login", "org", name, limit=10_000):
            self.records.delete("login", PLATFORM, login[1])
        self.blobs.delete(f"tenants/{name}")
        self.records.delete("org", PLATFORM, name)
