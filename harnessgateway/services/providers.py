"""An organization's model providers (SPEC §7.3): an API shape, a base URL and a key sealed the moment it arrives and
opened only by the gateway."""
from __future__ import annotations

import asyncio
from typing import Any

from .. import ids
from ..db import Database
from ..domain.provider import PROVIDER_APIS, WIRES
from ..errors import Invalid, NotFound
from ..netguard import check_url
from .pages import ts
from .vault import Vault

KEY_MAX = 8192


def key_context(provider_id: str) -> str:
    return f"provider/{provider_id}"


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "object": "provider", "name": row["name"], "api": row["api"], "base_url": row["base_url"],
            "created_at": ts(row["created_at"]), "updated_at": ts(row["updated_at"])}


class Providers:
    def __init__(self, db: Database, vault: Vault, *, allow_private: bool):
        self.db, self.vault, self.allow_private = db, vault, allow_private

    async def put(self, org_id: str, name: str, api: object, base_url: object, key: object) -> dict[str, Any]:
        ids.check_name(name, "provider")
        if api not in PROVIDER_APIS:
            raise Invalid(f"api is one of {', '.join(PROVIDER_APIS)}", param="api")
        if not isinstance(key, str) or not 1 <= len(key) <= KEY_MAX:
            raise Invalid("key is the provider's API key", param="key")
        url = WIRES[str(api)].base(await asyncio.to_thread(check_url, str(base_url), allow_private=self.allow_private,
                                                           param="base_url"))
        async with self.db.tenant(org_id) as tx:
            row = await tx.one("SELECT id FROM providers WHERE name = %s", name)
        provider_id = row["id"] if row else ids.new("provider")
        sealed = await self.vault.seal(org_id, key_context(provider_id), key)
        async with self.db.tenant(org_id) as tx:
            await tx.run("INSERT INTO providers (id, org_id, name, api, base_url, sealed_key) VALUES (%s, %s, %s, %s, %s, %s) "
                         "ON CONFLICT (org_id, name) DO UPDATE SET api = EXCLUDED.api, base_url = EXCLUDED.base_url, "
                         "sealed_key = EXCLUDED.sealed_key, updated_at = now()",
                         provider_id, org_id, name, api, url, sealed)
            return _view(await tx.get("SELECT * FROM providers WHERE id = %s", provider_id))

    async def list(self, org_id: str) -> list[dict[str, Any]]:
        async with self.db.tenant(org_id) as tx:
            return [_view(r) for r in await tx.all("SELECT * FROM providers ORDER BY name")]

    async def get(self, org_id: str, name: str) -> dict[str, Any]:
        async with self.db.tenant(org_id) as tx:
            row = await tx.one("SELECT * FROM providers WHERE name = %s", name)
        if not row:
            raise NotFound(f"no provider {name}")
        return _view(row)

    async def delete(self, org_id: str, name: str) -> None:
        async with self.db.tenant(org_id) as tx:
            if not await tx.run("DELETE FROM providers WHERE name = %s", name):
                raise NotFound(f"no provider {name}")
