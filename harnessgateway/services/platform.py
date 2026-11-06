"""The composition root: each port's adapter chosen from the settings, every service built once, extensions loaded
and bound. Nothing else in the core constructs a service. The api, the worker and the admin commands all start from
here; a door does not (it holds no database)."""
from __future__ import annotations

import logging
from typing import Any

from ..adapters.blob_fs import FileBlobs
from ..adapters.keys_local import LocalKeys
from ..adapters.mail_smtp import SmtpMailer
from ..config import Settings
from ..crypto import DataKeys, derive
from ..domain.access import ADMIN
from ..extensions import Extensions
from ..ports.blob import BlobStore
from ..ports.keys import KeyWrapper
from ..ports.mail import Mailer
from ..store.db import CORE_MIGRATIONS, Database
from ..store.moments import Moments
from ..store.objects import Objects
from .accounts import Accounts, check_email
from .audit import Audit
from .boxes import Boxes
from .containers import Containers
from .files import Files
from .harnesses import Harnesses
from .installs import Installs
from .keys import Keys
from .operations import INIT, Operations
from .orgs import Orgs
from .providers import Providers
from .snapshots import Snapshots
from .sso import Sso
from .traces import Traces
from .turns import Turns
from .usage import Usage
from .vault import Vault

log = logging.getLogger("harnessgateway.platform")


def _wrapper(settings: Settings) -> KeyWrapper:
    if settings.kms_key:
        from ..adapters.keys_kms import KmsKeys
        return KmsKeys(settings.kms_key)
    return LocalKeys(derive(settings.secret_key, "harnessgateway/master"))


def _mailer(settings: Settings) -> Mailer | None:
    return SmtpMailer(settings.smtp_url, settings.mail_from) if settings.smtp_url else None


def _store(settings: Settings) -> BlobStore:
    if settings.blob_url.startswith("s3://"):
        from ..adapters.blob_s3 import S3Blobs
        return S3Blobs(settings.blob_url)
    return FileBlobs(settings.blob_url)


class Platform:
    def __init__(self, settings: Settings, extensions: Extensions | None = None):
        s = self.settings = settings
        self.extensions = extensions or Extensions.load(settings.extensions)
        self.db = Database(settings.database_url)
        self.moments = Moments(settings.redis_url)
        self.data_keys = DataKeys(_wrapper(settings))
        self.store = _store(settings)
        self.objects = Objects(self.db, self.store)
        self.vault = Vault(self.db, self.data_keys)
        self.audit = Audit(self.db)
        self.orgs = Orgs(self.db, self.data_keys, s.defaults)
        self.accounts = Accounts(self.db, s.pepper, self.audit, s.console_url, _mailer(s))
        self.sso = Sso(self.db, self.moments, self.vault, self.accounts, self.audit, s.console_url,
                       allow_private=s.allow_private_egress)
        self.keys = Keys(self.db, s.pepper, self.audit)
        self.providers = Providers(self.db, self.vault, allow_private=s.allow_private_egress)
        self.installs = Installs(self.db, self.objects, self.moments, s.sandbox_image)
        self.harnesses = Harnesses(self.db, self.objects, self.vault, self.audit, self.installs)
        self.files = Files(self.db, self.objects)
        self.snapshots = Snapshots(self.db, self.objects)
        self.containers = Containers(self.db, self.objects, self.snapshots, self.moments)
        self.traces = Traces(self.db, self.objects, self.extensions)
        self.usage = Usage(self.db, self.extensions)
        self.boxes = Boxes(self.db, self.objects, self.moments, self.vault, self.snapshots, self.installs, self.traces,
                           self.usage, self.extensions, s)
        self.turns = Turns(self.db, self.objects, self.vault, self.moments, self.extensions, self.boxes, self.snapshots)
        self.operations = Operations(self.db, self.store, self.moments, self.orgs, self.accounts, s)
        self.extensions.bind(self)

    async def open(self) -> None:
        await self.db.open()
        await self.db.check_role()
        await self.moments.ensure_group()

    async def close(self) -> None:
        await self.sso.close()
        await self.moments.close()
        await self.db.close()

    async def migrate(self) -> list[str]:
        return await self.db.migrate((CORE_MIGRATIONS, *self.extensions.migrations))

    async def init(self, name: str, admin_email: str) -> dict[str, Any]:
        """Self-hosted's one organization (SPEC §10.1): the harness package builder installed, its first admin (the
        deployer: a platform admin too) with the invitation that activates the account, and an admin key for
        automation. Both secrets are in the result once. Run again, it changes nothing and returns neither. The email is
        checked first: nothing is created for an organization that could not get its admin."""
        org = await self.orgs.first()
        if org:
            return {"organization": org, "note": "already initialized: no new invitation or key"}
        check_email(admin_email)
        org = await self.orgs.create(name)
        org_id = str(org["id"])
        builder = self.settings.builder_dir
        if (builder / "harness.json").is_file():
            await self.harnesses.install_folder(org_id, builder)
        else:
            log.warning("no harness package builder at %s (HG_BUILDER_DIR): none installed", builder)
        admin = await self.accounts.create(org_id, INIT, admin_email, "", ADMIN, platform_admin=True)
        key = await self.keys.create_admin(org_id, "init")
        return {"organization": org, "admin": admin, "admin_key": key["key"]}
