"""Organizations. A community installation has one, made when it is set up; the core has no way to make another."""

from harnessgateway.store.db import Database


class Orgs:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def all(self) -> list[str]:
        """Every organization's id."""
        return [row["id"] for row in await self._db.all(None, "SELECT id FROM orgs ORDER BY id")]

    async def ensure(self, org_id: str, name: str) -> None:
        await self._db.run(None, "INSERT INTO orgs (id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING", [org_id, name])
