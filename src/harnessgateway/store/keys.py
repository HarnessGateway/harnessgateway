"""API keys. A key is shown once, when it is made; after that only its hash is kept."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from harnessgateway.domain.principal import Principal
from harnessgateway.errors import Invalid, NotFound
from harnessgateway.ids import fingerprint, new_api_key, new_id
from harnessgateway.store.db import Database

_COLUMNS = "id, name, created_at, last_used_at, revoked_at"


@dataclass(frozen=True)
class Key:
    id: str
    name: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None

    def shown(self) -> dict[str, Any]:
        def time(value: datetime | None) -> str | None:
            return value.isoformat() if value else None
        return {"id": self.id, "object": "key", "name": self.name, "created_at": time(self.created_at),
                "last_used_at": time(self.last_used_at), "revoked_at": time(self.revoked_at)}


class Keys:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, org_id: str, name: object) -> tuple[Key, str]:
        """Make a key. Returns its record and the key itself, which is never available again."""
        if not isinstance(name, str) or not 0 < len(name) <= 200:
            raise Invalid("name is 1 to 200 characters: what the key is for", param="name")
        text = new_api_key()
        row = await self._db.one(org_id, f"INSERT INTO keys (id, org_id, name, hash) VALUES (%s, %s, %s, %s) "
                                         f"RETURNING {_COLUMNS}", [new_id("key"), org_id, name, fingerprint(text)])
        assert row is not None
        return Key(**row), text

    async def authenticate(self, text: str) -> Principal | None:
        """Whose key this is; or None when it is no key, or a revoked one."""
        row = await self._db.one(None, "UPDATE keys SET last_used_at = now() WHERE hash = %s AND revoked_at IS NULL "
                                       "RETURNING org_id, id", [fingerprint(text)])
        return Principal(row["org_id"], row["id"]) if row else None

    async def all(self, org_id: str) -> list[Key]:
        rows = await self._db.all(org_id, f"SELECT {_COLUMNS} FROM keys WHERE org_id = %s ORDER BY id", [org_id])
        return [Key(**row) for row in rows]

    async def revoke(self, org_id: str, key_id: str) -> None:
        if not await self._db.run(org_id, "UPDATE keys SET revoked_at = now() WHERE org_id = %s AND id = %s "
                                          "AND revoked_at IS NULL", [org_id, key_id]):
            raise NotFound(f"there is no key {key_id} to revoke")
