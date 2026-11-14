"""Everything a process keeps its data in, made once and passed to whatever needs it."""

from dataclasses import dataclass

from harnessgateway.domain.environment import Environment
from harnessgateway.domain.modelpath import ModelPath
from harnessgateway.domain.resolve import Resolved, resolve
from harnessgateway.domain.sealing import Sealer, keyed_hash
from harnessgateway.settings import Settings
from harnessgateway.store.audit import Audit
from harnessgateway.store.blobs import BlobStore
from harnessgateway.store.db import Database
from harnessgateway.store.drafts import Drafts
from harnessgateway.store.egress import Egress
from harnessgateway.store.environments import Environments
from harnessgateway.store.files import Files
from harnessgateway.store.harnesses import Harnesses
from harnessgateway.store.keys import Keys
from harnessgateway.store.orgs import Orgs
from harnessgateway.store.platform import PlatformStore
from harnessgateway.store.providers import Providers
from harnessgateway.store.snapshots import Snapshots
from harnessgateway.store.turns import Turns


@dataclass(frozen=True)
class Stores:
    settings: Settings
    db: Database
    blobs: BlobStore
    orgs: Orgs
    keys: Keys
    snapshots: Snapshots
    providers: Providers
    environments: Environments
    harnesses: Harnesses
    drafts: Drafts
    files: Files
    turns: Turns
    egress: Egress
    audit: Audit
    platform: PlatformStore

    def image(self, environment: Environment) -> str:
        """The image an environment's sandboxes are built from: its own, or else the platform's sandbox image of
        this version, so an environment that names none moves to each new version's image (spec §7.6)."""
        return environment.image or self.settings.sandbox_image

    @classmethod
    def of(cls, settings: Settings, db: Database, blobs: BlobStore) -> "Stores":
        sealer = Sealer(settings.secret_key)
        return cls(
            settings=settings, db=db, blobs=blobs,
            orgs=Orgs(db),
            keys=Keys(db, settings.secret_key),
            snapshots=Snapshots(db, blobs),
            providers=Providers(db, sealer),
            environments=Environments(db, sealer),
            harnesses=Harnesses(db, blobs, sealer),
            drafts=Drafts(db),
            files=Files(db, blobs),
            turns=Turns(db),
            egress=Egress(db),
            audit=Audit(db),
            platform=PlatformStore(db, sealer),
        )

    async def resolve(self, org_id: str, path: ModelPath) -> Resolved:
        """What a model string means for this organization, as its definitions stand now (spec §10.2)."""
        return resolve(
            path,
            environments={environment.name: environment for environment in await self.environments.all(org_id)},
            harnesses={harness.name: harness for harness in await self.harnesses.all(org_id)},
            providers=[record.provider for record in await self.providers.all(org_id)],
        )

    def hash(self, token: str) -> str:
        """What is stored in place of a key or a token."""
        return keyed_hash(self.settings.secret_key, token)
