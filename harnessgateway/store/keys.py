"""API keys (spec §16). A key is shown once, when it is made; only a keyed hash of it is kept."""

import secrets
import string
from dataclasses import dataclass
from datetime import datetime

from harnessgateway.domain.principal import Principal, check_role
from harnessgateway.domain.sealing import keyed_hash
from harnessgateway.errors import Invalid, NotFound
from harnessgateway.ids import new_id
from harnessgateway.store.db import Database

PREFIX = "hg_"
_ALPHABET = string.ascii_letters + string.digits
_COLUMNS = "id, name, role, created_at, last_used_at, revoked_at"


@dataclass(frozen=True)
class Key:
    id: str
    name: str
    role: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class Keys:
    def __init__(self, db: Database, secret_key: bytes) -> None:
        self._db = db
        self._secret_key = secret_key

    async def create(self, org_id: str, *, name: object, role: object) -> tuple[Key, str]:
        """Make a key. Returns its record and its text; the text is never available again."""
        if not isinstance(name, str) or not 0 < len(name) <= 200:
            raise Invalid("name is 1 to 200 characters", param="name")
        text = PREFIX + "".join(secrets.choice(_ALPHABET) for _ in range(40))
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                f"INSERT INTO keys (id, org_id, name, role, hash) VALUES (%s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
                [new_id("key"), org_id, name, check_role(role), keyed_hash(self._secret_key, text)])).fetchone()
        assert row is not None
        return Key(**row), text

    async def authenticate(self, text: str) -> Principal | None:
        """Whose key this is; or None when it is not a key, or is revoked."""
        if not text.startswith(PREFIX):
            return None
        async with self._db.system() as conn:
            row = await (await conn.execute(
                "UPDATE keys SET last_used_at = now() WHERE hash = %s AND revoked_at IS NULL "
                "RETURNING org_id, id, role", [keyed_hash(self._secret_key, text)])).fetchone()
        return Principal(row["org_id"], row["id"], row["role"]) if row else None

    async def all(self, org_id: str) -> list[Key]:
        async with self._db.org(org_id) as conn:
            rows = await (await conn.execute(
                f"SELECT {_COLUMNS} FROM keys WHERE org_id = %s ORDER BY created_at, id", [org_id])).fetchall()
        return [Key(**row) for row in rows]

    async def revoke(self, org_id: str, key_id: str) -> None:
        async with self._db.org(org_id) as conn:
            cursor = await conn.execute(
                "UPDATE keys SET revoked_at = now() WHERE org_id = %s AND id = %s AND revoked_at IS NULL",
                [org_id, key_id])
        if cursor.rowcount == 0:
            raise NotFound(f"there is no key {key_id}")
