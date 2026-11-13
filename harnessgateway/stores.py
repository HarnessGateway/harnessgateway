"""Everything a process keeps its data in, made once and passed to whatever needs it."""

from dataclasses import dataclass

from harnessgateway.domain.sealing import Sealer, keyed_hash
from harnessgateway.settings import Settings
from harnessgateway.store.blobs import BlobStore
from harnessgateway.store.db import Database
from harnessgateway.store.environments import Environments
from harnessgateway.store.files import Files
from harnessgateway.store.harnesses import Harnesses
from harnessgateway.store.providers import Providers
from harnessgateway.store.snapshots import Snapshots
from harnessgateway.store.turns import Turns


@dataclass(frozen=True)
class Stores:
    settings: Settings
    db: Database
    blobs: BlobStore
    snapshots: Snapshots
    providers: Providers
    environments: Environments
    harnesses: Harnesses
    files: Files
    turns: Turns

    @classmethod
    def of(cls, settings: Settings, db: Database, blobs: BlobStore) -> "Stores":
        sealer = Sealer(settings.secret_key)
        return cls(
            settings=settings, db=db, blobs=blobs,
            snapshots=Snapshots(db, blobs),
            providers=Providers(db, sealer),
            environments=Environments(db, sealer, settings.sandbox_image),
            harnesses=Harnesses(db, blobs, sealer),
            files=Files(db, blobs),
            turns=Turns(db),
        )

    def hash(self, token: str) -> str:
        """What is stored in place of a key or a token."""
        return keyed_hash(self.settings.secret_key, token)
