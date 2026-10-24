"""Intake: what stands between a validated request and a queued turn. The organization's rate ceiling,
the idempotency key a client may send so a retried request makes one turn, then the session's own
rules in `domain.sessions`."""
from __future__ import annotations

import time

from ..backends.records import Records
from ..contract import wire
from ..domain.members import Member
from ..domain.orgs import Orgs
from ..domain.sessions import Sessions, Turn
from ..errors import Invalid

IDEMPOTENCY_TTL_S = 24 * 3600
RATE_WINDOW_S = 60


class Intake:
    def __init__(self, records: Records, orgs: Orgs, sessions: Sessions):
        self.records, self.orgs, self.sessions = records, orgs, sessions

    def create(self, who: Member, req: wire.Request, idempotency_key: str = "") -> Turn:
        if idempotency_key:
            if len(idempotency_key) > 200 or any(c in idempotency_key for c in " /\\\0"):
                raise Invalid("Idempotency-Key is one token of at most 200 characters")
            seen = self.records.get("idem", who.org, f"{who.name}:{idempotency_key}")
            if seen and seen["expires"] > time.time():
                return self.sessions.turn(who, seen["response"])
        self.rate_check(who)
        turn = self.sessions.create(who, req)
        if idempotency_key:
            self.records.put("idem", who.org, f"{who.name}:{idempotency_key}", {"response": turn.id, "expires": time.time() + IDEMPOTENCY_TTL_S})
        return turn

    def rate_check(self, who: Member) -> None:
        """At most `requests_per_minute` turns created per organization per minute; the ceiling is the
        deployer's, like every other."""
        ceiling = self.orgs.get(who.org).ceilings.requests_per_minute
        window = int(time.time() // RATE_WINDOW_S)
        key = f"rate:{window}"
        d = self.records.get("meter", who.org, key) or {"count": 0}
        if d["count"] >= ceiling:
            raise Invalid(f"the organization's rate ceiling of {ceiling} requests per minute is reached; try again shortly")
        self.records.put("meter", who.org, key, {"count": d["count"] + 1, "at": time.time()})
