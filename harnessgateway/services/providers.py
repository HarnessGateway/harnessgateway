"""An organization's own model providers: an endpoint, its API shape and its key. The key is sealed the moment it
arrives and opened only by the gateway, per request; it never reaches a sandbox, a node or a log. The organization
pays its provider directly; the platform never resells model access."""
from __future__ import annotations

from typing import Any

from .. import ids
from ..crypto import Sealer
from ..db import Database
from ..domain.package import PROVIDER_APIS
from ..errors import Invalid, NotFound
from ..netguard import check_url
from .principal import Principal
from .tenancy import audit


def context(provider_id: str) -> str:
    return f"provider:{provider_id}"


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "object": "provider", "name": row["name"], "api": row["api"], "base_url": row["base_url"],
            "created_at": int(row["created_at"].timestamp()), "updated_at": int(row["updated_at"].timestamp())}


class Providers:
    def __init__(self, db: Database, sealer: Sealer, *, allow_private: bool):
        self.db, self.sealer, self.allow_private = db, sealer, allow_private

    async def put(self, p: Principal, name: str, api: str, base_url: str, key: str) -> dict[str, Any]:
        p.require_admin()
        ids.check_name(name, "provider")
        if api not in PROVIDER_APIS:
            raise Invalid(f"api is one of {', '.join(PROVIDER_APIS)}", param="api")
        url = check_url(base_url, allow_private=self.allow_private)
        if url.endswith("/v1"):                       # the base is the URL before /v1: harnesses call <base>/v1/...
            url = url[:-3]
        if not isinstance(key, str) or not 1 <= len(key) <= 4096:
            raise Invalid("key is the provider's API key", param="key")
        async with self.db.tenant(p.org_id) as tx:
            row = await tx.one("SELECT id FROM providers WHERE name = %s", name)
            provider_id = row["id"] if row else ids.new("provider")
            sealed = self.sealer.seal(key, context(provider_id))
            await tx.run("INSERT INTO providers (id, org_id, name, api, base_url, sealed_key) VALUES (%s, %s, %s, %s, %s, %s) "
                         "ON CONFLICT (org_id, name) DO UPDATE SET api = EXCLUDED.api, base_url = EXCLUDED.base_url, "
                         "sealed_key = EXCLUDED.sealed_key, updated_at = now()",
                         provider_id, p.org_id, name, api, url, sealed)
            await audit(tx, p.org_id, p.actor, "provider.put", provider_id, name=name, api=api, base_url=url)
            out = await tx.one("SELECT * FROM providers WHERE id = %s", provider_id)
        return _view(out)

    async def list(self, p: Principal) -> list[dict[str, Any]]:
        async with self.db.tenant(p.org_id) as tx:
            return [_view(r) for r in await tx.all("SELECT * FROM providers ORDER BY name")]

    async def get(self, p: Principal, name: str) -> dict[str, Any]:
        async with self.db.tenant(p.org_id) as tx:
            row = await tx.one("SELECT * FROM providers WHERE name = %s", name)
        if not row:
            raise NotFound(f"no provider {name}")
        return _view(row)

    async def delete(self, p: Principal, name: str) -> None:
        p.require_admin()
        async with self.db.tenant(p.org_id) as tx:
            row = await tx.one("DELETE FROM providers WHERE name = %s RETURNING id", name)
            if not row:
                raise NotFound(f"no provider {name}")
            await audit(tx, p.org_id, p.actor, "provider.delete", row["id"], name=name)
