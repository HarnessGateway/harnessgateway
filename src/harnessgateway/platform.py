"""A running platform: its settings, its stores, its driver and its extensions, made once and passed to whatever
needs them, and the few things that need more than one of them."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harnessgateway.domain.principal import Principal
from harnessgateway.domain.sandbox import session_folders
from harnessgateway.domain.sealing import KeySealer
from harnessgateway.driver.base import Driver
from harnessgateway.driver.docker import DockerDriver
from harnessgateway.errors import Refused
from harnessgateway.extensions import Extensions
from harnessgateway.settings import Settings, SettingsError
from harnessgateway.store.authority import AuthorityStore
from harnessgateway.store.blobs import BlobStore, DirectoryBlobs
from harnessgateway.store.db import Database
from harnessgateway.store.files import Files
from harnessgateway.store.grants import Grants
from harnessgateway.store.keys import Keys
from harnessgateway.store.orgs import Orgs
from harnessgateway.store.resources import Resources
from harnessgateway.store.rounds import Rounds
from harnessgateway.store.sessions import Sessions
from harnessgateway.store.snapshots import Snapshots
from harnessgateway.store.turns import Turns
from harnessgateway.store.usage import Usage

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Platform:
    settings: Settings
    extensions: Extensions
    db: Database
    driver: Driver
    orgs: Orgs
    keys: Keys
    resources: Resources
    turns: Turns
    sessions: Sessions
    snapshots: Snapshots
    files: Files
    usage: Usage
    grants: Grants
    authority: AuthorityStore
    rounds: Rounds

    @classmethod
    def of(cls, settings: Settings, db: Database, extensions: Extensions | None = None, *,
           driver: Driver | None = None) -> "Platform":
        """The platform these settings describe, with each part an extension supplies in place of the core's."""
        extensions = extensions or Extensions()
        sealer = extensions.sealer(settings) or KeySealer(settings.secret_key)
        scheme, separator, _ = settings.blobs.partition("://")
        stores = extensions.blob_stores()
        if separator and scheme not in stores:
            raise SettingsError(f"HG_BLOBS names a {scheme}:// store, and no extension provides one")
        blobs: BlobStore = stores[scheme](settings.blobs) if separator else DirectoryBlobs(Path(settings.blobs))
        if driver is None:
            drivers = {"docker": lambda s: DockerDriver(namespace=s.namespace, data=s.data, runtime=s.runtime),
                       **extensions.drivers()}
            if settings.driver not in drivers:
                raise SettingsError(f"HG_DRIVER is one of {', '.join(sorted(drivers))}: {settings.driver!r}")
            driver = drivers[settings.driver](settings)
        return cls(settings, extensions, db, driver, Orgs(db), Keys(db), Resources(db, blobs, sealer), Turns(db),
                   Sessions(db), Snapshots(db, blobs), Files(db, blobs), Usage(db), Grants(db, sealer),
                   AuthorityStore(db, sealer), Rounds(db))

    async def announce(self, name: str, org_id: str, *, by: Principal | None = None, **about: Any) -> None:
        """Tell the extensions what happened. One that fails is logged: it never fails what it was told about."""
        said = {"org_id": org_id, "at": datetime.now(UTC).isoformat(), "by": by.subject if by else None, **about}
        try:
            await self.extensions.on_event(name, said)
        except Exception:
            log.exception("an extension failed on %s", name)

    async def remove_session(self, org_id: str, session_id: str) -> None:
        """Delete a session: its turns, its snapshots and its folders."""
        await self.snapshots.release(org_id, await self.sessions.remove(org_id, session_id))
        for folder in session_folders(session_id).values():
            await self.driver.remove(folder)

    async def forget(self, org_id: str, turn_id: str) -> None:
        """Delete a turn that was not to be stored, and its session with it when nothing else is left in it. One
        that is still running (its caller went away) is left for housekeeping to find later."""
        try:
            await self.turns.delete(org_id, turn_id)
            if (session_id := await self.sessions.emptied_by(org_id, turn_id)) is not None:
                await self.remove_session(org_id, session_id)
        except Refused:
            pass
