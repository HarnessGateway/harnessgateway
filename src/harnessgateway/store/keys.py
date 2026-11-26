"""API keys. A key is shown once, when it is made; after that only its hash is kept, with its last characters: by
those, someone who finds a key knows which it is. A key acts as someone (its subject), says who made it, and may be
limited: to the actions its scopes match, and until a time."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from harnessgateway.domain import permission
from harnessgateway.domain.change import Change
from harnessgateway.domain.principal import OWNER, Principal
from harnessgateway.errors import Invalid, NotFound
from harnessgateway.ids import fingerprint, new_api_key, new_id
from harnessgateway.store.db import Connection, Database, Tell

_COLUMNS = "id, name, subject, created_by, hint, scopes, created_at, expires_at, last_used_at, revoked_at"
HINT = 4                                       # how many of a key's last characters are kept: enough to tell it by
_LIVE = "revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())"


@dataclass(frozen=True)
class Key:
    id: str
    name: str
    subject: str                               # who it acts as
    created_by: str | None                     # who made it; None: it was made before that was kept
    hint: str | None                           # the last characters of the key itself; None, the same
    scopes: list[str]                          # the patterns it is limited to; none: it is not limited
    created_at: datetime
    expires_at: datetime | None                # from when it is refused; None: never
    last_used_at: datetime | None
    revoked_at: datetime | None

    @property
    def limited(self) -> bool:
        return bool(self.scopes) or self.expires_at is not None

    def shown(self) -> dict[str, Any]:
        def time(value: datetime | None) -> str | None:
            return value.isoformat() if value else None
        return {"id": self.id, "object": "key", "name": self.name, "subject": self.subject,
                "created_by": self.created_by, "hint": self.hint, "scopes": self.scopes,
                "created_at": time(self.created_at), "expires_at": time(self.expires_at),
                "last_used_at": time(self.last_used_at), "revoked_at": time(self.revoked_at)}


def _moment(value: object) -> datetime | None:
    """When a key ends, as it was sent: a time still to come, or nothing."""
    if value is None:
        return None
    try:
        when = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        when = None
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if when is None or when <= datetime.now(UTC):
        raise Invalid("expires_at is a time still to come, such as 2027-01-31T00:00:00Z, or left out for a key that "
                      "does not end", param="expires_at")
    return when


class Keys:
    def __init__(self, db: Database, tell: Tell) -> None:
        self._db = db
        self._tell = tell

    async def create(self, org_id: str, name: object, *, by: str, subject: str = OWNER, expires_at: object = None,
                     scopes: object = None) -> tuple[Key, str]:
        """Make a key that acts as `subject`: the installation's owner, unless an edition has other people. `by` is
        who makes it. Returns its record and the key itself, which is never available again."""
        if not isinstance(name, str) or not 0 < len(name) <= 200:
            raise Invalid("name is 1 to 200 characters: what the key is for", param="name")
        limits, ends = permission.checked(scopes if scopes is not None else [], "scopes"), _moment(expires_at)
        text = new_api_key()
        async with self._db.tx(org_id) as conn:
            row = await (await conn.execute(
                "INSERT INTO keys (id, org_id, name, hash, subject, created_by, hint, scopes, expires_at) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
                [new_id("key"), org_id, name, fingerprint(text), subject, by, text[-HINT:], Jsonb(list(limits)),
                 ends])).fetchone()
            assert row is not None
            key = Key(**row)
            about = {name: key.shown()[name] for name in ("name", "subject", "scopes", "expires_at")}
            await self._tell(conn, Change("keys.created", org_id, by, key.id, about))
        return key, text

    async def authenticate(self, text: str) -> Principal | None:
        """Who this key acts as, with the key and what it is limited to; None when it is no key, a revoked one, or
        one past its time."""
        row = await self._db.one(None, f"UPDATE keys SET last_used_at = now() WHERE hash = %s AND {_LIVE} "
                                       "RETURNING org_id, id, subject, scopes", [fingerprint(text)])
        return Principal(row["org_id"], row["subject"], row["id"], tuple(row["scopes"])) if row else None

    async def get(self, org_id: str, key_id: str) -> Key:
        row = await self._db.one(org_id, f"SELECT {_COLUMNS} FROM keys WHERE org_id = %s AND id = %s",
                                 [org_id, key_id])
        if row is None:
            raise NotFound(f"there is no key {key_id}")
        return Key(**row)

    async def all(self, org_id: str, *, subject: str | None = None) -> list[Key]:
        """The organization's keys, oldest first; with `subject`, those that act as them."""
        rows = await self._db.all(org_id, f"SELECT {_COLUMNS} FROM keys WHERE org_id = %s AND (%s::text IS NULL OR "
                                          "subject = %s) ORDER BY id", [org_id, subject, subject])
        return [Key(**row) for row in rows]

    async def revoke(self, org_id: str, key_id: str, *, by: str) -> None:
        async with self._db.tx(org_id) as conn:
            revoked = await conn.execute("UPDATE keys SET revoked_at = now() WHERE org_id = %s AND id = %s "
                                         "AND revoked_at IS NULL", [org_id, key_id])
            if revoked.rowcount == 0:
                raise NotFound(f"there is no key {key_id} to revoke")
            await self._tell(conn, Change("keys.revoked", org_id, by, key_id))

    async def revoke_of(self, conn: Connection, org_id: str, subject: str, *, by: str) -> int:
        """Revoke every key that acts as `subject`, in the caller's transaction: what an edition does as it removes
        that someone, so both happen or neither. How many."""
        rows = await (await conn.execute(
            "UPDATE keys SET revoked_at = now() WHERE org_id = %s AND subject = %s AND revoked_at IS NULL "
            "RETURNING id", [org_id, subject])).fetchall()
        for row in rows:
            await self._tell(conn, Change("keys.revoked", org_id, by, row["id"]))
        return len(rows)
