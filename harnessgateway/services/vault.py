"""Sealing and opening an organization's secrets (SPEC §8.1): provider keys, harness secret values, a turn's caller
credentials. Each is sealed under its organization's data key, bound to the record it belongs to."""
from __future__ import annotations

import asyncio

from ..crypto import DataKeys, seal, unseal
from ..db import Database


class Vault:
    def __init__(self, db: Database, data_keys: DataKeys):
        self.db, self.data_keys = db, data_keys

    async def _key(self, org_id: str) -> bytes:
        async with self.db.platform() as tx:
            wrapped = await tx.value("SELECT data_key FROM orgs WHERE id = %s", org_id)
        assert wrapped, f"{org_id} has a data key"
        return await asyncio.to_thread(self.data_keys.open, org_id, wrapped)

    async def seal(self, org_id: str, context: str, value: str) -> str:
        return seal(await self._key(org_id), value, f"{org_id}/{context}")

    async def open(self, org_id: str, context: str, sealed: str) -> str:
        return unseal(await self._key(org_id), sealed, f"{org_id}/{context}")
