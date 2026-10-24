"""A rodeo: one task, several harnesses' turns on it, and the winner the member picked. Built on
responses that already exist; the leaderboard counts runs and wins per harness."""
from __future__ import annotations

import time
from typing import Any

from ..backends.records import Records
from ..contract import wire
from ..errors import Invalid, NotFound
from ..kernel.ids import short_id
from .members import Member
from .sessions import Sessions


class Rodeos:
    def __init__(self, records: Records, sessions: Sessions):
        self.records, self.sessions = records, sessions

    def create(self, who: Member, task: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
        if not 2 <= len(entries) <= 6:
            raise Invalid("a rodeo has two to six entries")
        rows = []
        for e in entries:
            r = self.sessions.turn(who, str(e.get("response", "")))
            rows.append({"harness": r.harness, "model": wire.model_path(r.harness, r.provider, r.model), "response": r.id, "session": r.session})
        if len({row["harness"] for row in rows}) < 2:
            raise Invalid("a rodeo pits different harnesses against each other")
        d = {"id": f"rodeo_{short_id()}", "org": who.org, "member": who.name, "task": (task or "")[:4000],
             "entries": rows, "winner": "", "created": time.time()}
        self.records.put("rodeo", who.org, d["id"], d)
        return d

    def get(self, who: Member, aid: str) -> dict[str, Any]:
        d = self.records.get("rodeo", who.org, aid) if aid.startswith("rodeo_") else None
        if d is None or not self.sessions.reachable(who, d["org"], d["member"]):
            raise NotFound(f"no rodeo {aid}")
        return d

    def list(self, who: Member) -> list[dict[str, Any]]:
        found = [d for d in self.records.list("rodeo", who.org) if self.sessions.reachable(who, d["org"], d["member"])]
        return sorted(found, key=lambda d: -d["created"])

    def pick(self, who: Member, aid: str, winner: str) -> dict[str, Any]:
        d = self.get(who, aid)
        if winner and winner not in {e["harness"] for e in d["entries"]}:
            raise Invalid("the winner is one of the rodeo's harnesses")
        d["winner"] = winner
        self.records.put("rodeo", who.org, aid, d)
        return d

    def delete(self, who: Member, aid: str) -> None:
        self.get(who, aid)
        self.records.delete("rodeo", who.org, aid)

    def leaderboard(self, who: Member) -> list[dict[str, Any]]:
        """Per harness, across the rodeos this member can see: runs and wins, most wins first."""
        board: dict[str, dict[str, int]] = {}
        for d in self.list(who):
            for e in d["entries"]:
                row = board.setdefault(e["harness"], {"runs": 0, "wins": 0})
                row["runs"] += 1
                row["wins"] += e["harness"] == d["winner"]
        return [{"harness": hn, **v} for hn, v in sorted(board.items(), key=lambda kv: (-kv[1]["wins"], -kv[1]["runs"], kv[0]))]
