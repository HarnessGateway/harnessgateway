"""Housekeeping, on a timer (spec §18.5): what nobody asked for, and nothing would break without, for a while."""

import asyncio
import logging

from harnessgateway.domain.plan import mind_volume, work_volume
from harnessgateway.drivers.base import Driver
from harnessgateway.stores import Stores

log = logging.getLogger(__name__)

CHUNK_GRACE_S = 3600
UNSTORED_AFTER_S = 600


class Housekeeping:
    def __init__(self, stores: Stores, driver: Driver, *, every_s: float = 60.0) -> None:
        self._stores = stores
        self._driver = driver
        self._every_s = every_s

    async def once(self) -> None:
        stores = self._stores
        for _, session_id in await stores.turns.idle_volumes(idle_for_s=stores.settings.volume_ttl_s):
            await self._driver.remove_volume(mind_volume(session_id))        # the snapshot holds everything
            await self._driver.remove_volume(work_volume(session_id))
        for org_id, turn_id in await stores.turns.unstored(final_for_s=UNSTORED_AFTER_S):
            await stores.turns.forget(org_id, turn_id)
        for org in await stores.orgs.all():
            await stores.snapshots.collect(org.id, grace_s=CHUNK_GRACE_S)

    async def run(self) -> None:
        while True:
            try:
                await self.once()
            except Exception:
                log.exception("housekeeping failed; it will try again")
            await asyncio.sleep(self._every_s)
