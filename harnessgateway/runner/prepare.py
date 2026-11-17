"""Preparing an environment: its image built, and each harness it carries installed on it (spec §9.4, §8.5)."""

import asyncio
import logging
from collections import defaultdict

from harnessgateway.drivers.base import Driver, DriverError
from harnessgateway.errors import Refused
from harnessgateway.runner.builder import Builder, BuildFailed
from harnessgateway.runner.installer import Installer, InstallFailed
from harnessgateway.store.environments import Prepared
from harnessgateway.stores import Stores

log = logging.getLogger(__name__)


class Preparer:
    def __init__(self, stores: Stores, driver: Driver) -> None:
        self._stores = stores
        self.builder = Builder(stores, driver)
        self.installer = Installer(stores, driver)
        self._locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)

    async def prepare(self, org_id: str, name: str) -> None:
        """Build an environment's image and install its harnesses on it, so a turn need not wait for either, and
        record what it is prepared as. One preparation of an environment at a time, each of the environment as
        it stands when it starts: the last one asked for is the one recorded. What fails is recorded on the
        build or the install, and tried again when next asked for."""
        environments = self._stores.environments
        async with self._locks[(org_id, name)]:
            try:
                environment = await environments.get(org_id, name)
            except Refused:
                return                                          # deleted meanwhile

            async def starting(build_id: str) -> None:
                await environments.set_prepared(org_id, name, Prepared(None, build_id))

            try:
                built = await self.builder.ensure(org_id, environment, starting)
            except BuildFailed as e:
                await environments.set_prepared(org_id, name, Prepared(None, e.build_id))
                return
            except DriverError as e:
                log.warning("environment %s could not be prepared: %s", name, e)
                return
            await environments.set_prepared(org_id, name, Prepared(built.image_id, built.build_id))
            for harness_name in environment.harnesses:
                try:
                    harness = await self._stores.harnesses.get(org_id, harness_name)
                    await self.installer.ensure(org_id, harness, environment, built.image_id)
                except (InstallFailed, Refused):                # recorded, or removed meanwhile
                    continue
