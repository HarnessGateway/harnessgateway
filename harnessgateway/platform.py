"""A running platform: its stores, its driver, its extensions, and the few things that need more than one of them."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from harnessgateway.domain.plan import install_volume, mind_volume, work_volume
from harnessgateway.domain.principal import Principal
from harnessgateway.drivers.base import Driver
from harnessgateway.extensions import Extensions
from harnessgateway.runner.installer import Installer, InstallFailed
from harnessgateway.stores import Stores

log = logging.getLogger(__name__)


@dataclass
class Platform:
    stores: Stores
    driver: Driver
    extensions: Extensions
    installer: Installer
    _background: set[asyncio.Task[Any]] = field(default_factory=set)

    @classmethod
    def of(cls, stores: Stores, driver: Driver, extensions: Extensions | None = None) -> "Platform":
        return cls(stores, driver, extensions or Extensions(), Installer(stores, driver))

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

    def install_in_background(self, org_id: str, harness_name: str, environment_name: str) -> None:
        """Install a harness for an environment's image without making the caller wait (spec §8.5)."""
        async def install() -> None:
            try:
                harness = await self.stores.harnesses.get(org_id, harness_name)
                environment = await self.stores.environments.get(org_id, environment_name)
                image_id = await self.driver.image_id(environment.image)
                await self.installer.ensure(org_id, harness, environment, image_id)
            except InstallFailed:
                pass                                            # the install's own record says why
            except Exception:
                log.exception("installing %s for %s failed", harness_name, environment_name)
        task = asyncio.create_task(install())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def settle(self) -> None:
        """Wait for installs started in the background."""
        await asyncio.gather(*self._background, return_exceptions=True)

    async def remove_unused_packages(self) -> None:
        for org_id, digest, object_key, installs in await self.stores.harnesses.unused_packages():
            await self.stores.harnesses.remove_package(org_id, digest, object_key)
            for install_id in installs:
                await self.driver.remove_volume(install_volume(install_id))
