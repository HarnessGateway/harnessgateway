"""The organization (spec §16, §17). The core has one."""

from dataclasses import dataclass

from harnessgateway.errors import Invalid, NotFound
from harnessgateway.store.db import Database


@dataclass(frozen=True)
class Org:
    id: str
    name: str
    retention_days: int


class Orgs:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def ensure(self, org_id: str, name: str) -> None:
        """Make the organization unless it is there."""
        async with self._db.system() as conn:
            await conn.execute("INSERT INTO orgs (id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING", [org_id, name])

    async def get(self, org_id: str) -> Org:
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                "SELECT id, name, retention_days FROM orgs WHERE id = %s", [org_id])).fetchone()
        if row is None:
            raise NotFound(f"there is no organization {org_id}")
        return Org(**row)

    async def all(self) -> list[Org]:
        async with self._db.system() as conn:
            rows = await (await conn.execute("SELECT id, name, retention_days FROM orgs ORDER BY id")).fetchall()
        return [Org(**row) for row in rows]

    async def set_retention(self, org_id: str, days: object) -> Org:
        """How long an idle session is kept, in days. 0 means for ever."""
        if not isinstance(days, int) or isinstance(days, bool) or not 0 <= days <= 36_500:
            raise Invalid("retention_days is a whole number from 0 (keep for ever) to 36500", param="retention_days")
        async with self._db.org(org_id) as conn:
            await conn.execute("UPDATE orgs SET retention_days = %s WHERE id = %s", [days, org_id])
        return await self.get(org_id)
