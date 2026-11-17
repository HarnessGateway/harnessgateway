"""Building an environment's image: its setup script run on its base image (spec §9.4)."""

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from harnessgateway.domain.environment import Environment
from harnessgateway.domain.plan import build_plan
from harnessgateway.domain.sealing import new_placeholder, new_token
from harnessgateway.drivers.base import Driver, DriverError
from harnessgateway.store.builds import Build
from harnessgateway.stores import Stores


class BuildFailed(Exception):
    """The environment's setup script failed. The build's log says why."""

    def __init__(self, build_id: str, log: str) -> None:
        super().__init__(log)
        self.build_id = build_id


@dataclass(frozen=True)
class Built:
    image_id: str                      # the image the environment's turns run on
    build_id: str | None               # the build that made it; None when the base image is used as it is


class Builder:
    def __init__(self, stores: Stores, driver: Driver) -> None:
        self._stores = stores
        self._driver = driver
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)     # one attempt per build at a time

    async def ensure(self, org_id: str, environment: Environment,
                     starting: Callable[[str], Awaitable[None]] | None = None) -> Built:
        """The image the environment's turns run on: its base image, set up by its script when it has one, which
        is run first if it has not been (`starting` is told the build's id then). A DriverError when the base
        image cannot be had; BuildFailed when the script fails."""
        stores = self._stores
        base_id = await self._driver.image_id(stores.image(environment))
        if not environment.setup.script:
            return Built(base_id, None)
        build = await stores.builds.build_for(org_id, base_id, environment.setup.script)
        async with self._locks[build.id]:
            build = await stores.builds.get(org_id, build.id)
            if build.status != "ready":
                if starting:
                    await starting(build.id)
                build = await self._attempt(org_id, build, environment)
        if build.image_id is None:
            raise BuildFailed(build.id, build.log_tail)
        return Built(build.image_id, build.id)

    async def _attempt(self, org_id: str, build: Build, environment: Environment) -> Build:
        stores, driver = self._stores, self._driver
        proxy_token = new_token()
        in_build = {name: new_placeholder() for name in environment.secrets}
        await stores.builds.begin(org_id, build.id, environment=environment.name,
                                  proxy_token_hash=stores.hash(proxy_token),
                                  placeholders={placeholder: name for name, placeholder in in_build.items()})
        plan = build_plan(org_id=org_id, build_id=build.id, image_id=build.base_image_id, environment=environment,
                          proxy_token=proxy_token, secret_placeholders=in_build)
        lines: list[str] = []

        async def keep(line: str) -> None:
            lines.append(line)

        image_id = None
        run = asyncio.create_task(driver.build(plan, keep))
        try:
            done, _ = await asyncio.wait({run}, timeout=environment.setup.timeout_s)
            if not done:
                await driver.stop(plan.name, 0)
                await asyncio.gather(run, return_exceptions=True)
                lines.append(f"setup did not finish in {environment.setup.timeout_s} seconds")
            else:
                result = run.result()
                if not result.started:
                    lines.append(f"setup could not be started: {result.error}")
                elif result.image_id is None:
                    lines.append(f"setup exited with code {result.exit_code}")
                image_id = result.image_id
        except DriverError as e:
            lines.append(f"setup failed: {e}")
        await stores.builds.end(org_id, build.id, image_id=image_id, log="\n".join(lines))
        return await stores.builds.get(org_id, build.id)
