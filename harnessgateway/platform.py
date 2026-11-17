"""A running platform: its stores, its driver, its extensions, and the few things that need more than one of them."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from harnessgateway.domain.plan import install_volume, mind_volume, work_volume
from harnessgateway.domain.principal import Principal
from harnessgateway.drivers.base import Driver
from harnessgateway.extensions import Extensions
from harnessgateway.runner.prepare import Preparer
from harnessgateway.stores import Stores

log = logging.getLogger(__name__)

BUILD_GRACE_S = 3600


@dataclass
class Platform:
    stores: Stores
    driver: Driver
    extensions: Extensions
    preparer: Preparer
    _background: set[asyncio.Task[Any]] = field(default_factory=set)

    @classmethod
    def of(cls, stores: Stores, driver: Driver, extensions: Extensions | None = None) -> "Platform":
        return cls(stores, driver, extensions or Extensions(), Preparer(stores, driver))

    async def audit(self, principal: Principal, action: str, target: str, detail: dict[str, Any] | None = None) -> None:
        """Record a change, and tell the extensions (spec §22.2)."""
        record = await self.stores.audit.record(principal, action, target, detail)
        await self.extensions.audit({"org_id": principal.org_id, "at": record.at.isoformat(), "key_id": record.key_id,
                                     "action": action, "target": target, "detail": record.detail})

    async def remove_session(self, org_id: str, session_id: str) -> None:
        """Delete a session: its turns, its snapshots and its volumes (spec §6.8)."""
        snapshots = await self.stores.turns.remove_session(org_id, session_id)
        await self.stores.snapshots.release(org_id, snapshots)
        await self.driver.remove_volume(mind_volume(session_id))
        await self.driver.remove_volume(work_volume(session_id))

    async def forget(self, org_id: str, turn_id: str) -> None:
        """Delete a turn that was not to be stored, and its session with it if nothing else is left in it."""
        await self.stores.turns.forget(org_id, turn_id)
        session_id = await self.stores.turns.empty_session_of(org_id, turn_id)
        if session_id is not None:
            await self.remove_session(org_id, session_id)

    def prepare_in_background(self, org_id: str, environments: list[str] | None = None) -> None:
        """Build environments and install their harnesses without making the caller wait (spec §9.4): those
        named, or every one of the organization."""
        async def prepare() -> None:
            names = environments if environments is not None else [
                environment.name for environment in await self.stores.environments.all(org_id)]
            for name in names:
                try:
                    await self.preparer.prepare(org_id, name)
                except Exception:
                    log.exception("preparing environment %s failed", name)
        task = asyncio.create_task(prepare())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def carrying(self, org_id: str, harness_name: str) -> list[str]:
        """The environments that carry a harness."""
        return [environment.name for environment in await self.stores.environments.all(org_id)
                if harness_name in environment.harnesses]

    async def settle(self) -> None:
        """Wait for preparations started in the background."""
        await asyncio.gather(*self._background, return_exceptions=True)

    async def remove_unused_builds(self, *, grace_s: float = BUILD_GRACE_S) -> None:
        """Remove the images built for environments that no environment is prepared as now, and what was installed
        on them, once they have not been touched for a while: a turn may be about to start on one that an
        environment has just left. An image still in use by a container is kept, and tried again later."""
        for org_id, build_id, image_id in await self.stores.builds.unused(grace_s=grace_s):
            if image_id is not None:
                if not await self.driver.remove_image(image_id):
                    continue
                for install_id in await self.stores.harnesses.remove_installs_on(org_id, image_id):
                    await self.driver.remove_volume(install_volume(install_id))
            await self.stores.builds.remove(org_id, build_id)

    async def remove_unused_packages(self) -> None:
        for org_id, digest, object_key, installs in await self.stores.harnesses.unused_packages():
            await self.stores.harnesses.remove_package(org_id, digest, object_key)
            for install_id in installs:
                await self.driver.remove_volume(install_volume(install_id))
