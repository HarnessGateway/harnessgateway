"""`harnessgateway serve` and `harnessgateway init`: the platform as one process (spec §16, §18)."""

import asyncio
import contextlib
import logging
import signal
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path

import uvicorn

from harnessgateway import __version__
from harnessgateway.api.internal import internal_app
from harnessgateway.api.public import public_app
from harnessgateway.catalog import Catalog
from harnessgateway.domain.names import DEFAULT_ORG
from harnessgateway.domain.plan import CA_VOLUME, ca_step
from harnessgateway.drivers.base import Driver
from harnessgateway.drivers.docker import DockerDriver
from harnessgateway.extensions import Extensions
from harnessgateway.platform import Platform
from harnessgateway.runner.housekeeping import Housekeeping
from harnessgateway.runner.runner import Runner
from harnessgateway.settings import Settings, SettingsError
from harnessgateway.store.blobs import BlobStore, DirectoryBlobs
from harnessgateway.store.db import MIGRATIONS, Database
from harnessgateway.stores import Stores

log = logging.getLogger(__name__)


def blob_store(settings: Settings, extensions: Extensions) -> BlobStore:
    """The store HG_DATA names: a directory, or a URL whose scheme an extension serves."""
    scheme, separator, _ = settings.data.partition("://")
    if not separator:
        return DirectoryBlobs(Path(settings.data))
    stores = extensions.blob_stores()
    if scheme not in stores:
        raise SettingsError(f"HG_DATA names a {scheme}:// store, and no extension provides one")
    return stores[scheme](settings.data)


def driver_for(settings: Settings, extensions: Extensions) -> Driver:
    drivers = {
        "docker": lambda s: DockerDriver(platform_image=s.platform_image, runtime=s.runtime),
        **extensions.drivers(),
    }
    if settings.driver not in drivers:
        raise SettingsError(f"HG_DRIVER is one of {', '.join(sorted(drivers))}")
    return drivers[settings.driver](settings)


@contextlib.asynccontextmanager
async def opened(settings: Settings, extensions: Extensions) -> AsyncIterator[Stores]:
    """The stores, on a database whose schema is up to date."""
    db = Database(settings.database_url)
    await db.open()
    try:
        await db.migrate([MIGRATIONS, *extensions.migrations()])
        yield Stores.of(settings, db, blob_store(settings, extensions))
    finally:
        await db.close()


async def init(settings: Settings, *, new_admin_key: bool = False) -> str | None:
    """First-time setup: the organization, the harnesses that ship with the platform, a default environment that
    carries them all, set up as the catalog says they need, and the first admin key. Returns the key when it made
    one. Run again, it changes nothing: what was set up once is the organization's to change.

    With `new_admin_key` it makes another admin key even though keys exist: the way back in when every admin
    key is lost. It is run on the host, by someone who could read the database anyway."""
    async with opened(settings, Extensions.load(settings.extensions)) as stores:
        if await stores.orgs.ensure(DEFAULT_ORG, "Organization"):
            catalog = Catalog(Path(settings.catalog))
            shipped = [(await stores.harnesses.add(DEFAULT_ORG, catalog.archive(m.name))).name
                       for m in catalog.entries()]
            await stores.environments.ensure_default(DEFAULT_ORG, shipped, catalog.default_environment())
        existing = await stores.keys.all(DEFAULT_ORG)
        if existing and not new_admin_key:
            return None
        name = "admin key made on the host" if existing else "first admin key"
        _, key = await stores.keys.create(DEFAULT_ORG, name=name, role="admin")
        return key


async def trust_bundle(stores: Stores, driver: Driver) -> None:
    """Make sure the certificate authority exists and the bundle sandboxes trust is in its volume (spec §11.5)."""
    await stores.platform.authority()
    await driver.ensure_volume(CA_VOLUME)
    written = await driver.run_step(ca_step(internal_url=stores.settings.internal_url))
    if written.exit_code != 0:
        raise RuntimeError(f"the certificate bundle could not be written for sandboxes: {written.output}")


class _Server(uvicorn.Server):
    """A server that leaves signals to the process: two of them run side by side here."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class Broken(Exception):
    """A part of the platform that must keep running has ended. The process ends with it, to be started again."""


async def stopped_or_broken(stop: asyncio.Event, working: Mapping[str, "asyncio.Task[None]"]) -> str | None:
    """Wait until the process is told to stop, or until one of its workers ends, which none should: a runner
    that has ended claims no turn, whatever the rest goes on answering. None for a stop; else what ended, and why."""
    told = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({told, *working.values()}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        told.cancel()
    for name, task in working.items():
        if task.done():
            error = None if task.cancelled() else task.exception()
            return f"{name} ended: {error!r}" if error else f"{name} ended"
    return None


async def serve(settings: Settings) -> None:
    """Run the public API, the internal API, the turn runner and housekeeping until told to stop. If the runner
    or housekeeping ends by itself (the database went away, say), everything is stopped and `Broken` is raised."""
    extensions = Extensions.load(settings.extensions)
    async with opened(settings, extensions) as stores:
        driver = driver_for(settings, extensions)
        platform = Platform.of(stores, driver, extensions)
        runner = Runner(stores, driver, extensions=extensions, preparer=platform.preparer)
        gone = await driver.reap(keep=set())
        ended = await runner.recover()
        isolation = await driver.isolation()
        log.info("HarnessGateway %s: driver %s, isolation %s; removed %d left-over containers, ended %d "
                 "abandoned turns", __version__, driver.name, isolation, len(gone), len(ended))
        if isolation == "none":
            log.warning("ISOLATION IS NONE: sandboxes share this host's kernel. This is for development and "
                        "for trusted code only. Install gVisor (runsc) to isolate them.")

        servers = [
            _Server(uvicorn.Config(public_app(platform), host="0.0.0.0", port=settings.port,  # noqa: S104
                                   log_config=None, access_log=False)),
            _Server(uvicorn.Config(internal_app(stores), host="0.0.0.0", port=settings.internal_port,  # noqa: S104
                                   log_config=None, access_log=False)),
        ]
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for name in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(name, stop.set)
        serving = [asyncio.create_task(server.serve()) for server in servers]
        while not all(server.started for server in servers):
            await asyncio.sleep(0.05)
        await trust_bundle(stores, driver)
        for org in await stores.orgs.all():
            platform.prepare_in_background(org.id)                 # a new version may need a new image
        working = {"the turn runner": asyncio.create_task(runner.run()),
                   "housekeeping": asyncio.create_task(Housekeeping(platform).run())}
        broken = await stopped_or_broken(stop, working)
        log.info("stopping: running turns are stopped, saved and ended")
        for task in working.values():
            task.cancel()
        await asyncio.gather(*working.values(), return_exceptions=True)
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*serving, return_exceptions=True)
    if broken:
        raise Broken(broken)
