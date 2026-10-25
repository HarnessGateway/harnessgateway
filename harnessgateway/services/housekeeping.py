"""What the platform does on its own clock, in the api role. Each job takes a Postgres advisory lock, so with several
API replicas exactly one runs it at a time; a failing pass is logged and retried on the next tick."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from ..blobs import Blobs, org_key
from ..db import Database
from .metering import Metering
from .scheduler import Scheduler

log = logging.getLogger("harnessgateway.housekeeping")
EVENTS_KEPT_H = 24


class Housekeeping:
    def __init__(self, db: Database, blobs: Blobs, scheduler: Scheduler, metering: Metering):
        self.db, self.blobs, self.scheduler, self.metering = db, blobs, scheduler, metering
        self.jobs: list[tuple[str, float, Callable[[], Awaitable[object]]]] = [
            ("lost-nodes", 15, self.scheduler.sweep),
            ("expire-sessions", 300, self.expire_sessions),
            ("purge", 600, self.purge),
            ("storage", 3600, self.metering.accrue_storage),
        ]

    async def _locked(self, name: str, job: Callable[[], Awaitable[object]]) -> None:
        lock = 9_000_000 + sum(name.encode())
        async with self.db.platform() as tx:
            if not await tx.value("SELECT pg_try_advisory_xact_lock(%s)", lock):
                return
            await job()

    async def run(self) -> None:
        last = {name: 0.0 for name, _, _ in self.jobs}
        while True:
            for name, every, job in self.jobs:
                if time.monotonic() - last[name] >= every:
                    last[name] = time.monotonic()
                    try:
                        await self._locked(name, job)
                    except Exception:
                        log.exception("housekeeping job %s failed", name)
            await asyncio.sleep(5)

    async def expire_sessions(self) -> int:
        """Sessions idle past their organization's retention end; their snapshots are deleted."""
        async with self.db.platform() as tx:
            rows = await tx.all(
                "UPDATE sessions s SET status = 'expired', snapshot_key = NULL, manifest_key = NULL FROM orgs o "
                "WHERE o.id = s.org_id AND s.status = 'active' AND s.running_turn_id IS NULL AND s.last_active_at < "
                "now() - coalesce((o.limits->>'session_retention_days')::int, 30) * interval '1 day' "
                "RETURNING s.id, s.org_id")
        for r in rows:
            await self.blobs.delete_prefix(org_key(r["org_id"], "sessions", r["id"], ""))
        return len(rows)

    async def purge(self) -> None:
        """Events of turns ended a day ago (the final response stays), tokens past expiry, sessions not stored."""
        async with self.db.platform() as tx:
            await tx.run("DELETE FROM turn_events e USING turns t WHERE t.id = e.turn_id AND t.ended_at < now() - %s * "
                         "interval '1 hour' AND e.data->>'type' NOT IN ('response.completed', 'response.failed', "
                         "'response.incomplete')", EVENTS_KEPT_H)
            await tx.run("DELETE FROM turn_tokens WHERE expires_at < now() - interval '1 day'")
            ended = await tx.all("UPDATE sessions SET status = 'expired', snapshot_key = NULL, manifest_key = NULL "
                                 "WHERE store = false AND status = 'active' AND running_turn_id IS NULL "
                                 "AND last_active_at < now() - interval '10 minutes' RETURNING id, org_id")
        for r in ended:
            await self.blobs.delete_prefix(org_key(r["org_id"], "sessions", r["id"], ""))
