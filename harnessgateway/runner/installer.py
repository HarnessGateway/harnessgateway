"""Installing a harness package for an environment image (spec §8.5)."""

import asyncio
from collections import defaultdict

from harnessgateway.domain.environment import Environment
from harnessgateway.domain.harness import Harness
from harnessgateway.domain.plan import install_plan, install_volume, unpack_step
from harnessgateway.domain.sealing import new_token
from harnessgateway.drivers.base import Driver, DriverError
from harnessgateway.stores import Stores


class InstallFailed(Exception):
    """The package could not be installed for the image. The install's log says why."""


class Installer:
    def __init__(self, stores: Stores, driver: Driver) -> None:
        self._stores = stores
        self._driver = driver
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)     # one attempt per install at a time

    async def ensure(self, org_id: str, harness: Harness, environment: Environment, image_id: str) -> str:
        """The id of the ready install of this package for this image, installing it first if need be."""
        harnesses = self._stores.harnesses
        install_id = (await harnesses.install_for(org_id, harness.digest, image_id)).id
        async with self._locks[install_id]:
            if (await harnesses.install(org_id, install_id)).status == "ready":
                return install_id
            step_token, proxy_token = new_token(), new_token()
            await harnesses.begin_install(org_id, install_id, environment=environment.name,
                                          step_token_hash=self._stores.hash(step_token),
                                          proxy_token_hash=self._stores.hash(proxy_token))
            try:
                log = await self._attempt(org_id, install_id, harness, environment, image_id, step_token, proxy_token)
            except (InstallFailed, DriverError) as e:
                await self._driver.remove_volume(install_volume(install_id))
                await harnesses.end_install(org_id, install_id, ready=False, log=str(e))
                raise InstallFailed(str(e)) from None
            await harnesses.end_install(org_id, install_id, ready=True, log=log)
            return install_id

    async def _attempt(self, org_id: str, install_id: str, harness: Harness, environment: Environment,
                       image_id: str, step_token: str, proxy_token: str) -> str:
        driver, manifest = self._driver, harness.manifest
        volume = install_volume(install_id)
        await driver.remove_volume(volume)                      # whatever an earlier attempt left
        await driver.ensure_volume(volume)
        unpacked = await driver.run_step(unpack_step(
            org_id=org_id, install_id=install_id, internal_url=self._stores.settings.internal_url, token=step_token))
        if unpacked.exit_code != 0:
            raise InstallFailed(f"the package could not be unpacked:\n{unpacked.output}")

        plan = install_plan(org_id=org_id, install_id=install_id, image_id=image_id, environment=environment,
                            manifest=manifest, proxy_token=proxy_token)
        lines: list[str] = []

        async def keep(line: str) -> None:
            lines.append(line)

        run = asyncio.create_task(driver.run_sandbox(plan, keep, keep))
        done, _ = await asyncio.wait({run}, timeout=manifest.install.timeout_s)
        if not done:
            await driver.stop(plan.name, manifest.run.stop_grace_s)
            await run
            raise InstallFailed("\n".join([*lines, f"install did not finish in {manifest.install.timeout_s} seconds"]))
        result = run.result()
        log = "\n".join(lines)
        if not result.started:
            raise InstallFailed(f"{log}\nthe install could not be started: {result.error}".strip())
        if result.exit_code != 0:
            raise InstallFailed(f"{log}\ninstall exited with code {result.exit_code}".strip())
        return log
