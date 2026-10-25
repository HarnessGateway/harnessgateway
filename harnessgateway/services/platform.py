"""The composition root: every service built once from the settings, in one place."""
from __future__ import annotations

import logging

from ..blobs import Blobs
from ..config import Settings
from ..crypto import UrlSigner, build_sealer, derive
from ..db import Database
from ..domain.pricing import PriceTable
from .housekeeping import Housekeeping
from .metering import Metering
from .packages import Packages
from .providers import Providers
from .scheduler import Scheduler
from .storage import Containers, Files
from .tenancy import Tenancy
from .traces import Traces
from .turns import Delivery, EventBus, Turns

log = logging.getLogger("harnessgateway.platform")


class Platform:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.database_url)
        self.pepper = derive(settings.secret_key, "harnessgateway/pepper")
        self.sealer = build_sealer(settings.secret_key, settings.kms_key_id)
        self.signer = UrlSigner(settings.secret_key)
        self.prices = PriceTable.build(settings.prices, settings.markup)
        self.blobs = Blobs(self.db, bucket=settings.s3_bucket, endpoint=settings.s3_endpoint, region=settings.s3_region,
                           access_key=settings.s3_access_key, secret_key=settings.s3_secret_key)
        self.tenancy = Tenancy(self.db, self.pepper)
        self.metering = Metering(self.db, self.prices)
        self.providers = Providers(self.db, self.sealer, allow_private=settings.allow_private_egress)
        self.packages = Packages(self.db, self.blobs, self.sealer, settings.builtin_dir,
                                 allow_private=settings.allow_private_egress)
        self.bus = EventBus(self.db)
        self.turns = Turns(self.db, self.blobs, self.tenancy, self.metering, self.bus)
        self.delivery = Delivery(self.db, self.blobs)
        self.scheduler = Scheduler(self.db, self.blobs, self.packages, self.signer, self.pepper)
        self.files = Files(self.db, self.blobs)
        self.containers = Containers(self.db, self.blobs, self.tenancy)
        self.traces = Traces(self.db, self.blobs, self.tenancy, self.turns)
        self.housekeeping = Housekeeping(self.db, self.blobs, self.scheduler, self.metering)

    async def open(self) -> None:
        await self.db.open()
        await self.db.check_role()

    async def close(self) -> None:
        await self.db.close()

    async def create_org(self, name: str, owner_email: str, limits: dict | None, actor: str) -> tuple[dict, str]:
        """An organization, its owner, the owner's key, and the default harnesses installed."""
        org, secret = await self.tenancy.create_org(name, owner_email, limits, actor)
        await self._install_defaults(org["id"], actor)
        return org, secret

    async def redeem(self, code: str, org_name: str, owner_email: str) -> tuple[dict, str]:
        """Invite-only sign-up: the same as create_org, paid for with an invite code."""
        org, secret = await self.tenancy.redeem(code, org_name, owner_email)
        await self._install_defaults(org["id"], f"signup:{org['id']}")
        return org, secret

    async def _install_defaults(self, org_id: str, actor: str) -> None:
        """Best effort: an organization that exists without a default harness can still install it."""
        for name in self.settings.default_packages or tuple(self.packages.builtins):
            if name not in self.packages.builtins:
                continue
            try:
                await self.packages.install_builtin(org_id, name, actor)
            except Exception:
                log.exception("installing the default harness %s for %s failed", name, org_id)
