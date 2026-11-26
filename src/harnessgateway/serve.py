"""`harnessgateway serve` and `harnessgateway init`: the platform as one process, and its first-time setup."""

import asyncio
import contextlib
import logging
import signal
from collections.abc import AsyncIterator, Iterator

import uvicorn
from starlette.applications import Starlette

from harnessgateway import __version__, defaults
from harnessgateway.api.app import internal_app, public_app
from harnessgateway.domain import contract
from harnessgateway.domain.change import HOST, PLATFORM
from harnessgateway.domain.sandbox import GIVEN_FOLDER
from harnessgateway.driver.base import Driver
from harnessgateway.extensions import Extensions
from harnessgateway.ids import DEFAULT_ORG
from harnessgateway.platform import Platform
from harnessgateway.proxy.authority import trust_bundle
from harnessgateway.runner.housekeeping import housekeeping
from harnessgateway.runner.runner import Runner
from harnessgateway.settings import Settings
from harnessgateway.store.db import CORE, SCHEMA, Database

log = logging.getLogger(__name__)


def given(certificate: str) -> dict[str, bytes]:
    """What every sandbox is given, read-only, by its path under the folder: the certificates it trusts."""
    return {contract.CA_BUNDLE[len(contract.GIVEN) + 1:]: trust_bundle(certificate).encode()}


class Broken(Exception):
    """A part of the platform that must keep running has ended. The process ends with it, to be started again."""


async def prepared(settings: Settings, db: Database, *, driver: Driver | None = None) -> Platform:
    """The platform on an open database, ready to work: its extensions loaded, every layer's schema up to date,
    its organization there, and each extension started. `driver` stands in for the one the settings name."""
    extensions = Extensions.load(settings.extensions)
    await db.migrate([(CORE, SCHEMA), *extensions.migrations()])
    platform = Platform.of(settings, db, extensions, driver=driver)
    await platform.orgs.ensure(DEFAULT_ORG, "Organization")
    await extensions.start(platform)
    return platform


@contextlib.asynccontextmanager
async def opened(settings: Settings) -> AsyncIterator[Platform]:
    """The platform, ready to work, for as long as the block lasts."""
    db = Database(settings.database_url)
    await db.open()
    try:
        yield await prepared(settings, db)
    finally:
        await db.close()


async def init(settings: Settings, *, new_key: bool = False) -> str | None:
    """Set the installation up: give its organization the defaults, then make the first API key. Returns the key
    when it made one. The defaults come before the key and only with the first one, so a setup that was
    interrupted is finished by running it again, and one that is finished is never repeated. With `new_key` it
    makes another key even though keys exist: the way back in when every key is lost. It is run on the host, by
    someone who could read the database anyway."""
    async with opened(settings) as platform:
        existing = await platform.keys.all(DEFAULT_ORG)
        if existing and not new_key:
            return None
        if not existing:
            await defaults.give(platform, DEFAULT_ORG, by=HOST)
        name = "made on the host" if existing else "first key"
        return (await platform.keys.create(DEFAULT_ORG, name, by=HOST))[1]


class _Server(uvicorn.Server):
    """A server that leaves signals to the process: two of them run side by side here."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def serve(settings: Settings) -> None:
    """Run the public API, the internal API, the runner and housekeeping until told to stop. If the runner or
    housekeeping ends by itself (the database went away, say), everything is stopped and `Broken` is raised."""
    async with opened(settings) as platform:
        isolation = await platform.driver.isolation()
        log.info("HarnessGateway %s: driver %s, isolation %s", __version__, platform.driver.name, isolation)
        if isolation == "none":
            log.warning("ISOLATION IS NONE: sandboxes share this host's kernel. This is for development and for "
                        "trusted code only. Install gVisor (runsc) to isolate them.")
        certificate, _ = await platform.authority()
        await platform.driver.fill(GIVEN_FOLDER, given(certificate))
        for org_id in await platform.orgs.all():   # the packages that ship follow the platform: each is this version's
            await defaults.packages(platform, org_id, by=PLATFORM)

        def server(app: Starlette, port: int) -> _Server:
            return _Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_config=None,  # noqa: S104
                                          access_log=False))                               # in its container
        servers = [server(public_app(platform), settings.port), server(internal_app(platform), settings.internal_port)]
        stop = asyncio.Event()
        for name in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(name, stop.set)
        serving = [asyncio.create_task(each.serve()) for each in servers]
        working = {"the runner": asyncio.create_task(Runner(platform).run()),
                   "housekeeping": asyncio.create_task(housekeeping(platform))}
        told = asyncio.create_task(stop.wait())
        await asyncio.wait({told, *working.values()}, return_when=asyncio.FIRST_COMPLETED)
        told.cancel()
        broken = [f"{name} ended: {task.exception()!r}" for name, task in working.items()
                  if task.done() and not task.cancelled()]
        log.info("stopping: running turns are stopped, saved and ended")
        for task in working.values():
            task.cancel()
        await asyncio.gather(*working.values(), return_exceptions=True)
        for each in servers:
            each.should_exit = True
        await asyncio.gather(*serving, return_exceptions=True)
    if broken:
        raise Broken("; ".join(broken))
