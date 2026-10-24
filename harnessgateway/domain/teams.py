"""A team: members of an organization who share harnesses and sessions. A harness may belong to one
team; its sessions run under the team's own sandbox uid and are visible to the team. Owners are in
every team."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from ..backends.records import Records
from ..errors import Conflict, Invalid, NotFound
from ..kernel.ids import check_name, is_name
from .members import Member, Members
from .orgs import Orgs


@dataclass(frozen=True)
class Team:
    org: str
    name: str
    created: float
    members: list[str] = field(default_factory=list)
    title: str = ""


class Teams:
    def __init__(self, records: Records, orgs: Orgs, members: Members):
        self.records, self.orgs, self.members = records, orgs, members

    def put(self, org: str, name: str, members: list[str], title: str = "") -> Team:
        self.orgs.get(org)
        check_name(name, "team")
        known = {m.name for m in self.members.list(org)}
        if unknown := sorted(set(members) - known):
            raise Invalid(f"not members of {org}: {', '.join(unknown)}")
        existing = self.records.get("team", org, name)
        t = Team(org, name, existing["created"] if existing else time.time(), sorted(set(members)), (title or (existing or {}).get("title", ""))[:80])
        self.records.put("team", org, name, asdict(t))
        return t

    def get(self, org: str, name: str) -> Team:
        d = self.records.get("team", org, name) if is_name(name) else None
        if d is None:
            raise NotFound(f"no team {name}")
        return Team(**d)

    def list(self, org: str) -> list[Team]:
        return [Team(**d) for d in self.records.list("team", org)]

    def of(self, who: Member) -> list[str]:
        return [t.name for t in self.list(who.org) if who.name in t.members]

    def delete(self, org: str, name: str) -> None:
        self.get(org, name)
        if used := [d["name"] for d in self.records.list("harness", org) if d.get("team") == name]:
            raise Conflict(f"team {name} owns harnesses: {', '.join(used)}; move or delete them first")
        self.records.delete("team", org, name)

    def includes(self, who: Member, team: str) -> bool:
        """Owners are in every team; a member is in the teams that list them; nobody is excluded from
        the organization-wide scope (the empty team)."""
        if not team or who.owner:
            return True
        try:
            return who.name in self.get(who.org, team).members
        except NotFound:
            return False

    def drop_member(self, org: str, name: str) -> None:
        for t in self.list(org):
            if name in t.members:
                self.put(org, t.name, [x for x in t.members if x != name], t.title)
