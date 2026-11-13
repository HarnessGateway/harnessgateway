"""Providers and their keys (spec §10.1, §14.1)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from harnessgateway.domain.provider import Provider
from harnessgateway.domain.sealing import Sealer
from harnessgateway.errors import Conflict, Invalid, NotFound
from harnessgateway.store.db import Database
from harnessgateway.store.sealed import check_value

_KIND = "provider"
_CHANGEABLE = frozenset({"apis", "default_model", "api_key"})


@dataclass(frozen=True)
class ProviderRecord:
    provider: Provider
    has_key: bool
    created_at: datetime


def _record(row: Mapping[str, Any]) -> ProviderRecord:
    provider = Provider.load({"name": row["name"], "apis": row["apis"], "default_model": row["default_model"]})
    return ProviderRecord(provider, row["has_key"], row["created_at"])


_COLUMNS = "name, apis, default_model, key_sealed IS NOT NULL AS has_key, created_at"


class Providers:
    def __init__(self, db: Database, sealer: Sealer) -> None:
        self._db = db
        self._sealer = sealer

    def _seal(self, org_id: str, name: str, key: object) -> bytes | None:
        if key is None:
            return None
        return self._sealer.seal(check_value(key, "api_key"), org=org_id, kind=_KIND, name=name)

    async def create(self, org_id: str, body: Mapping[str, Any]) -> ProviderRecord:
        fields = dict(body)
        key = fields.pop("api_key", None)
        provider = Provider.load(fields)
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                "INSERT INTO providers (org_id, name, apis, default_model, key_sealed) VALUES (%s, %s, %s, %s, %s) "
                f"ON CONFLICT DO NOTHING RETURNING {_COLUMNS}",
                [org_id, provider.name, Jsonb(provider.apis), provider.default_model,
                 self._seal(org_id, provider.name, key)])).fetchone()
        if row is None:
            raise Conflict(f"a provider named {provider.name!r} already exists")
        return _record(row)

    async def update(self, org_id: str, name: str, changes: Mapping[str, Any]) -> ProviderRecord:
        """Replace the fields named. `api_key: null` removes the key."""
        unknown = sorted(set(changes) - _CHANGEABLE)
        if unknown:
            raise Invalid(f"{unknown[0]} cannot be changed; what can: {', '.join(sorted(_CHANGEABLE))}")
        current = (await self.get(org_id, name)).provider
        fields = {key: value for key, value in changes.items() if key != "api_key"}
        provider = Provider.load({**current.dump(), **fields})
        async with self._db.org(org_id) as conn:
            if "api_key" in changes:
                await conn.execute("UPDATE providers SET key_sealed = %s WHERE org_id = %s AND name = %s",
                                   [self._seal(org_id, name, changes["api_key"]), org_id, name])
            row = await (await conn.execute(
                "UPDATE providers SET apis = %s, default_model = %s, updated_at = now() "
                f"WHERE org_id = %s AND name = %s RETURNING {_COLUMNS}",
                [Jsonb(provider.apis), provider.default_model, org_id, name])).fetchone()
        if row is None:
            raise NotFound(f"there is no provider named {name!r}")
        return _record(row)

    async def delete(self, org_id: str, name: str) -> None:
        async with self._db.org(org_id) as conn:
            cursor = await conn.execute("DELETE FROM providers WHERE org_id = %s AND name = %s", [org_id, name])
        if cursor.rowcount == 0:
            raise NotFound(f"there is no provider named {name!r}")

    async def get(self, org_id: str, name: str) -> ProviderRecord:
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                f"SELECT {_COLUMNS} FROM providers WHERE org_id = %s AND name = %s", [org_id, name])).fetchone()
        if row is None:
            raise NotFound(f"there is no provider named {name!r}")
        return _record(row)

    async def all(self, org_id: str) -> list[ProviderRecord]:
        """Every provider, in the order they were created."""
        async with self._db.org(org_id) as conn:
            rows = await (await conn.execute(
                f"SELECT {_COLUMNS} FROM providers WHERE org_id = %s ORDER BY created_at, name", [org_id])).fetchall()
        return [_record(row) for row in rows]

    async def key(self, org_id: str, name: str) -> str | None:
        """The provider's key, opened. Only the egress proxy's policy ever asks."""
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                "SELECT key_sealed FROM providers WHERE org_id = %s AND name = %s", [org_id, name])).fetchone()
        if row is None:
            raise NotFound(f"there is no provider named {name!r}")
        sealed = row["key_sealed"]
        return self._sealer.open(bytes(sealed), org=org_id, kind=_KIND, name=name) if sealed is not None else None
