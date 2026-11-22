"""Installing a harness package for an environment's image: once for each package and image, whichever environment
or turn asks first, in whichever process."""

import asyncio
import json
from dataclasses import asdict

from harnessgateway import shipped
from harnessgateway.domain import schema
from harnessgateway.domain.contract import CONTRACT_DIR
from harnessgateway.domain.egress import Policy
from harnessgateway.domain.package import read_archive
from harnessgateway.domain.resolve import Resolved
from harnessgateway.domain.sandbox import install_folder, install_plan, install_policy
from harnessgateway.driver.base import DriverError
from harnessgateway.errors import Refused
from harnessgateway.ids import new_token
from harnessgateway.platform import Platform


def placed() -> dict[str, bytes]:
    """What the platform puts in every install, beside the package: the contract the package is written to, in
    words, and what its `run` may print, as a schema it can be checked against."""
    return {f"{CONTRACT_DIR}/PACKAGES.md": (shipped.folder("docs") / "PACKAGES.md").read_bytes(),
            f"{CONTRACT_DIR}/output.schema.json": json.dumps(schema.lines()).encode()}


class InstallFailed(Exception):
    """The package could not be installed for the image. The message is the install's log, which says why."""


class Installer:
    def __init__(self, platform: Platform) -> None:
        self._platform = platform

    async def ensure(self, org_id: str, resolved: Resolved, image_id: str) -> str:
        """The id of the ready install of this harness's package for this image, installing it first if need be.
        A failed install is recorded, and tried again when next asked for."""
        platform, failure = self._platform, ""
        async with platform.resources.installing(org_id, resolved.harness.digest, image_id) as install:
            if install.ready:
                return install.id
            try:                                   # what goes wrong is recorded here, and raised once it is kept
                log = await self._attempt(org_id, install.id, resolved, image_id)
            except (InstallFailed, DriverError, Refused) as error:
                failure = str(error)
                await platform.driver.remove(install_folder(install.id))
            finally:
                await platform.grants.end(install.id)
            await install.end(ready=not failure, log=failure or log)
        if failure:
            raise InstallFailed(failure)
        return install.id

    async def _attempt(self, org_id: str, install_id: str, resolved: Resolved, image_id: str) -> str:
        platform, manifest, environment = self._platform, resolved.harness.manifest, resolved.environment
        package = read_archive(await platform.resources.package(org_id, resolved.harness.digest))
        files = {**{file.path: file.content for file in package.files}, **placed()}
        await platform.driver.fill(install_folder(install_id), files,
                                   executable=[file.path for file in package.files if file.executable])
        token = new_token()
        await platform.grants.put(Policy(org_id, install_id, resolved.line.environment, install_policy(environment)),
                                  token)
        plan = install_plan(install_id=install_id, image_id=image_id, environment=environment, manifest=manifest,
                            proxy_token=token)
        lines: list[str] = []

        async def keep(line: str) -> None:
            lines.append(line)

        running = asyncio.create_task(platform.driver.run(plan, keep, keep))
        done, _ = await asyncio.wait({running}, timeout=manifest.install.timeout_s)
        if not done:
            await platform.driver.stop(plan.name, plan.stop_grace_s)
        result, log = await running, "\n".join(lines)
        await platform.usage.add(org_id, **asdict(result.measured))
        if not done:
            raise InstallFailed(f"{log}\ninstall did not finish in {manifest.install.timeout_s} seconds".strip())
        if not result.started:
            raise InstallFailed(f"{log}\nthe install could not be started: {result.error}".strip())
        if result.exit_code != 0:
            raise InstallFailed(f"{log}\ninstall exited with code {result.exit_code}".strip())
        return log
