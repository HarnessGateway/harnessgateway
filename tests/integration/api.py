"""A whole platform over real HTTP for tests: the public app, a runner on the local driver, and three keys."""

import asyncio
import contextlib
import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from openai import AsyncOpenAI

from harnessgateway.api.internal import internal_app
from harnessgateway.api.public import public_app
from harnessgateway.extensions import Extensions
from harnessgateway.platform import Platform
from harnessgateway.runner.runner import Runner
from harnessgateway.stores import Stores
from tests.helpers import SHELL, serving
from tests.integration.local_driver import LocalDriver


class Running:
    def __init__(self, platform: Platform, org: str, url: str, keys: dict[str, str]) -> None:
        self.platform, self.stores, self.org, self.url, self.keys = platform, platform.stores, org, url, keys
        self.admin = self.http("admin")
        self.developer = self.http("developer")
        self.runner = self.http("runner")
        self.openai = AsyncOpenAI(api_key=keys["runner"], base_url=f"{url}/v1", max_retries=0)

    def http(self, role: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.url, headers={"Authorization": f"Bearer {self.keys[role]}"},
                                 timeout=60)

    async def close(self) -> None:
        for client in (self.admin, self.developer, self.runner):
            await client.aclose()
        await self.openai.close()

    async def installed(self, name: str) -> dict[str, Any]:
        """Wait for a harness's background install to finish, and return the harness as the API shows it."""
        await self.platform.settle()
        return (await self.developer.get(f"/v1/harnesses/{name}")).json()


@contextlib.asynccontextmanager
async def running(stores: Stores, org: str, tmp_path: Path, extensions: Extensions | None = None,
                  driver: Any = None) -> AsyncIterator[Running]:
    """A running platform. It runs turns on the local driver unless another driver is given: another edition
    tests its own driver with the same platform around it."""
    async with stores.db.system() as conn:               # the queue is shared: start with nothing waiting
        await conn.execute("UPDATE turns SET status = 'cancelled' WHERE status IN ('queued', 'in_progress')")
    stores = dataclasses.replace(stores, settings=dataclasses.replace(stores.settings, catalog=str(SHELL.parent)))
    keys = {role: (await stores.keys.create(org, name=f"the {role}", role=role))[1]
            for role in ("admin", "developer", "runner")}
    driver = driver or LocalDriver(tmp_path / "volumes", internal_app(stores))
    platform = Platform.of(stores, driver, extensions)
    runner = asyncio.create_task(Runner(stores, driver, installer=platform.installer, heartbeat_s=0.2).run())
    try:
        async with serving(public_app(platform)) as url:
            api = Running(platform, org, url, keys)
            try:
                yield api
            finally:
                await platform.settle()
                await api.close()
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
