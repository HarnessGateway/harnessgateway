"""What an organization used: the infrastructure the platform is. Processor time and memory, as its sandboxes'
driver measured them; bytes in and out, as the egress proxy counted them; and what it stores, for how long.

One row a day for each organization, added to as things happen. The lifetime is their sum, and any period a
range of them: an edition that bills, or shows an organization its own use, reads the same rows.
"""

from datetime import date
from typing import Any

from harnessgateway.store.db import Database

AMOUNTS = ("cpu_seconds", "memory_byte_seconds", "network_in_bytes", "network_out_bytes", "storage_byte_seconds")
_TODAY = "(now() AT TIME ZONE 'UTC')::date"
# Everything an organization has in the blob store: its snapshots' chunks, its uploaded files and its packages.
_STORED = " + ".join(f"COALESCE((SELECT sum(bytes) FROM {table} WHERE org_id = o.id), 0)"
                     for table in ("chunks", "files", "packages"))


def _adding(columns: tuple[str, ...], values: str) -> str:
    sums = ", ".join(f"{column} = usage.{column} + EXCLUDED.{column}" for column in columns)
    return (f"INSERT INTO usage (org_id, day, {', '.join(columns)}) {values} "
            f"ON CONFLICT (org_id, day) DO UPDATE SET {sums}")


class Usage:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, org_id: str, **amounts: float) -> None:
        """Add to what the organization used today."""
        assert amounts and set(amounts) <= set(AMOUNTS)
        columns = tuple(amounts)
        await self._db.run(org_id, _adding(columns, f"VALUES (%s, {_TODAY}, {', '.join(['%s'] * len(columns))})"),
                           [org_id, *amounts.values()])

    async def add_stored(self, elapsed_s: float) -> None:
        """Add what every organization stores now, held for the time since this was last done."""
        held = f"SELECT o.id, {_TODAY}, ({_STORED}) * %s FROM orgs o"
        await self._db.run(None, _adding(("storage_byte_seconds",), held), [elapsed_s])

    async def total(self, org_id: str, *, start: date | None = None, end: date | None = None) -> dict[str, Any]:
        """What the organization used from `start` up to, not including, `end` (its lifetime when neither is
        given), and how much it stores now."""
        sums = ", ".join(f"COALESCE(sum({column}), 0)::float AS {column}" for column in AMOUNTS)
        used = await self._db.one(org_id, (
            f"SELECT {sums} FROM usage WHERE org_id = %s AND (%s::date IS NULL OR day >= %s) "
            "AND (%s::date IS NULL OR day < %s)"), [org_id, start, start, end, end])
        stored = await self._db.one(org_id, f"SELECT ({_STORED})::bigint AS bytes FROM orgs o WHERE o.id = %s",
                                    [org_id])
        assert used is not None
        counted = {column: int(used[column]) if column.endswith("_bytes") else used[column] for column in AMOUNTS}
        return {**counted, "storage_bytes": stored["bytes"] if stored else 0}
