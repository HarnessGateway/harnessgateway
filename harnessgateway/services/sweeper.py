"""What the API does on its own clock (SPEC §10.2): each job runs under a Postgres advisory lock, so with several
replicas one runs it at a time; a failing pass is logged and retried on the next tick."""
from __future__ import annotations

import asyncio
import logging
import time
import zlib
from collections.abc import Awaitable, Callable

from ..db import Database
from .objects import Objects, org_key
from .scheduler import Scheduler
from .usage import Usage

TICK_S = 5
LIVE_EVENTS_KEPT_MIN = 10
UNSTORED_KEPT_MIN = 10
log = logging.getLogger("harnessgateway.sweeper")


class Sweeper:
    def __init__(self, db: Database, objects: Objects, scheduler: Scheduler, usage: Usage):
        self.db, self.objects = db, objects
        self.jobs: list[tuple[str, float, Callable[[], Awaitable[object]]]] = [
            ("lost-nodes", 15, scheduler.sweep), ("retention", 300, self.retention), ("purge", 300, self.purge),
            ("storage", 3600, usage.accrue_storage)]

    async def run(self) -> None:
        last = {name: 0.0 for name, _, _ in self.jobs}
        while True:
            for name, every, job in self.jobs:
                if time.monotonic() - last[name] >= every:
                    last[name] = time.monotonic()
                    try:
                        async with self.db.platform() as tx:
                            if await tx.value("SELECT pg_try_advisory_xact_lock(%s)",
                                              7_000_000 + zlib.crc32(name.encode()) % 1_000_000):
                                await job()
                    except Exception:
                        log.exception("sweeper job %s failed", name)
            await asyncio.sleep(TICK_S)

    async def _drop(self, rows: list[dict[str, str]]) -> None:
        for r in rows:
            await self.objects.delete_prefix(org_key(r["org_id"], "sessions", r["id"], ""))

    async def retention(self) -> int:
        """Sessions idle past their organization's retention (when it sets one) end; their state is deleted."""
        async with self.db.platform() as tx:
            rows = await tx.all(
                "UPDATE sessions s SET status = 'deleted', snapshot_key = NULL, files_key = NULL FROM orgs o "
                "WHERE o.id = s.org_id AND s.status = 'active' AND s.running_turn_id IS NULL AND s.saving_turn_id IS NULL "
                "AND coalesce((o.capacity->>'retention_days')::int, 0) > 0 "
                "AND s.last_active_at < now() - (o.capacity->>'retention_days')::int * interval '1 day' "
                "RETURNING s.id, s.org_id")
        await self._drop(rows)
        return len(rows)

    async def purge(self) -> None:
        """Deltas of ended turns, spent tokens, idempotency keys after a day, and sessions that were not stored."""
        async with self.db.platform() as tx:
            await tx.run("DELETE FROM live_events e USING turns t WHERE t.id = e.turn_id AND t.ended_at < now() - %s * "
                         "interval '1 minute'", LIVE_EVENTS_KEPT_MIN)
            await tx.run("DELETE FROM live_events e WHERE NOT EXISTS (SELECT 1 FROM turns t WHERE t.id = e.turn_id)")
            await tx.run("DELETE FROM turn_tokens WHERE revoked_at < now() - interval '1 day' OR expires_at < now() - "
                         "interval '1 day'")
            await tx.run("DELETE FROM idempotency WHERE created_at < now() - interval '1 day'")
            rows = await tx.all("UPDATE sessions SET status = 'deleted', snapshot_key = NULL, files_key = NULL "
                                "WHERE store = false AND status = 'active' AND running_turn_id IS NULL "
                                "AND last_active_at < now() - %s * interval '1 minute' RETURNING id, org_id",
                                UNSTORED_KEPT_MIN)
        await self._drop(rows)
