"""The organization: the tenant. It has a plan, ceilings the deployer set, a status, and a say in
whether its traces may be shared. Everything else in the platform belongs to one organization."""
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

    @property
    def ceilings(self) -> Limits:
        return Limits.from_dict(self.limits)

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "plan": self.plan, "status": self.status, "created": self.created,
                "trace_sharing": self.trace_sharing, "limits": self.limits}


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
        o = Org(name, time.time(), Limits.from_dict(limits).as_dict(), title=title[:80], plan=plan,
                trace_sharing=bool(trace_sharing) and plan != "enterprise")
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

    def update(self, name: str, *, limits: dict[str, Any] | None = None, status: str | None = None, title: str | None = None,
               plan: str | None = None, trace_sharing: bool | None = None) -> Org:
        o = self.get(name)
        if status is not None and status not in STATUSES:
            raise Invalid("an organization's status is active or suspended")
        if plan is not None and plan not in PLANS:
            raise Invalid(f"a plan is one of {', '.join(PLANS)}")
        plan = plan or o.plan
        sharing = o.trace_sharing if trace_sharing is None else bool(trace_sharing)
        o = Org(o.name, o.created, Limits.from_dict(limits).as_dict() if limits is not None else o.limits, status or o.status,
                (title or "")[:80] if title is not None else o.title, plan, sharing and plan != "enterprise")
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
