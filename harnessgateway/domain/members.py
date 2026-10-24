"""A member of an organization: a name and a role. Owners run the organization; members use it."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from ..backends.records import Records
from ..errors import Conflict, Invalid, NotFound
from ..kernel.ids import check_name, is_name
from .orgs import Orgs

ROLES = ("owner", "member")


@dataclass(frozen=True)
class Member:
    org: str
    name: str
    role: str
    created: float

    @property
    def owner(self) -> bool:
        return self.role == "owner"


class Members:
    def __init__(self, records: Records, orgs: Orgs):
        self.records, self.orgs = records, orgs

    def create(self, org: str, name: str, role: str) -> Member:
        self.orgs.get(org)
        check_name(name, "member")
        if role not in ROLES:
            raise Invalid(f"a role is one of {', '.join(ROLES)}")
        if self.records.get("member", org, name):
            raise Conflict(f"member {name} exists")
        m = Member(org, name, role, time.time())
        self.records.put("member", org, name, asdict(m))
        return m

    def get(self, org: str, name: str) -> Member:
        d = self.records.get("member", org, name) if is_name(name) else None
        if d is None:
            raise NotFound(f"no member {name}")
        return Member(**d)

    def list(self, org: str) -> list[Member]:
        return [Member(**d) for d in self.records.list("member", org)]

    def set_role(self, org: str, name: str, role: str) -> Member:
        m = self.get(org, name)
        if role not in ROLES:
            raise Invalid(f"a role is one of {', '.join(ROLES)}")
        if m.owner and role != "owner":
            self._not_the_last_owner(org)
        m = Member(org, name, role, m.created)
        self.records.put("member", org, name, asdict(m))
        return m

    def remove(self, org: str, name: str) -> None:
        """The member record; their keys and team seats are revoked by `Keys.revoke_all` and
        `Teams.drop_member`, which the caller runs with this."""
        m = self.get(org, name)
        if m.owner:
            self._not_the_last_owner(org)
        self.records.delete("member", org, name)

    def _not_the_last_owner(self, org: str) -> None:
        if sum(1 for x in self.list(org) if x.owner) == 1:
            raise Invalid("an organization keeps at least one owner")
