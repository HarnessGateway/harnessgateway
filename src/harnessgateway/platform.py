"""A running platform: its settings, its stores, its driver and its extensions, made once and passed to whatever
needs them, and the few things that need more than one of them."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harnessgateway.domain.sandbox import session_folders
from harnessgateway.domain.sealing import KeySealer, Sealer
from harnessgateway.driver.base import Driver
from harnessgateway.driver.docker import DockerDriver
from harnessgateway.errors import Refused
from harnessgateway.extensions import Extensions
from harnessgateway.proxy.authority import new_authority
from harnessgateway.settings import NO_SEALING_KEY, Settings, SettingsError
from harnessgateway.store.blobs import BlobStore, DirectoryBlobs
from harnessgateway.store.db import Database
from harnessgateway.store.files import Files
from harnessgateway.store.grants import Grants
from harnessgateway.store.keys import Keys
from harnessgateway.store.once import Once
from harnessgateway.store.orgs import Orgs
from harnessgateway.store.resources import Resources
from harnessgateway.store.rounds import Rounds
from harnessgateway.store.sessions import Sessions
from harnessgateway.store.snapshots import Snapshots
from harnessgateway.store.turns import Turns
from harnessgateway.store.usage import Usage


@dataclass(frozen=True)
class Platform:
    settings: Settings
    extensions: Extensions
    db: Database
    driver: Driver
    sealer: Sealer
    blobs: BlobStore
    orgs: Orgs
    keys: Keys
    resources: Resources
    turns: Turns
    sessions: Sessions
    snapshots: Snapshots
    files: Files
    usage: Usage
    grants: Grants
    once: Once
    rounds: Rounds

    @classmethod
    def of(cls, settings: Settings, db: Database, extensions: Extensions | None = None, *,
           driver: Driver | None = None) -> "Platform":
        """The platform these settings describe, with each part an extension supplies in place of the core's."""
        extensions = extensions or Extensions()
        sealer = extensions.sealer(settings)
        if sealer is None:
            if settings.secret_key is None:
                raise SettingsError(NO_SEALING_KEY)
            sealer = KeySealer(settings.secret_key)
        scheme, separator, _ = settings.blobs.partition("://")
        stores = extensions.blob_stores()
        if separator and scheme not in stores:
            raise SettingsError(f"HG_BLOBS names a {scheme}:// store, and no extension provides one")
        blobs: BlobStore = stores[scheme](settings.blobs) if separator else DirectoryBlobs(Path(settings.blobs))
        drivers = extensions.drivers(
            {"docker": lambda s: DockerDriver(namespace=s.namespace, data=s.data, runtime=s.runtime)})
        if driver is None:
            if settings.driver not in drivers:
                raise SettingsError(f"HG_DRIVER is one of {', '.join(sorted(drivers))}: {settings.driver!r}")
            driver = drivers[settings.driver](settings)
        tell = extensions.on_change
        return cls(settings, extensions, db, driver, sealer, blobs, Orgs(db), Keys(db, tell),
                   Resources(db, blobs, sealer, tell), Turns(db, sealer, tell), Sessions(db, tell),
                   Snapshots(db, blobs), Files(db, blobs, tell), Usage(db), Grants(db, sealer), Once(db, sealer),
                   Rounds(db))

    async def authority(self) -> tuple[str, str]:
        """The installation's certificate authority: its certificate and its private key, as PEM."""
        certificate, key = await self.once.get("authority", lambda: list(new_authority()))
        return certificate, key

    async def view_key(self) -> bytes:
        """The key view links are signed with: the installation's own, and no setting's."""
        return bytes.fromhex(await self.once.get("view-key", lambda: secrets.token_hex(32)))

    async def announce(self, name: str, org_id: str, **about: Any) -> None:
        """Tell the extensions what happened by itself. It cannot be refused, and fails nothing."""
        await self.extensions.on_event(name, {"org_id": org_id, "at": datetime.now(UTC).isoformat(), **about})

    async def remove_session(self, org_id: str, session_id: str, *, by: str | None) -> None:
        """Delete a session: its turns, its snapshots and its folders. `by` is who deletes it; None when nobody
        does (the session of a turn that was not to be stored)."""
        await self.snapshots.release(org_id, await self.sessions.remove(org_id, session_id, by=by))
        for folder in session_folders(session_id).values():
            await self.driver.remove(folder)

    async def forget(self, org_id: str, turn_id: str) -> None:
        """Delete a turn that was not to be stored, and its session with it when nothing else is left in it. One
        that is still running (its caller went away) is left for housekeeping to find later. It is no change of
        anyone's: nothing is told of it."""
        try:
            await self.turns.delete(org_id, turn_id, by=None)
            if (session_id := await self.sessions.emptied_by(org_id, turn_id)) is not None:
                await self.remove_session(org_id, session_id, by=None)
        except Refused:
            pass
