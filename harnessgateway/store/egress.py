"""The record of what sandboxes connected to (spec §11.6)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from harnessgateway.store.db import Database

_COLUMNS = "subject_id, host, port, allowed, reason, bytes_up, bytes_down, started_at, ended_at"


@dataclass(frozen=True)
class Connection:
    subject_id: str                            # the turn, or the install
    host: str
    port: int
    allowed: bool
    reason: str
    bytes_up: int
    bytes_down: int
    started_at: datetime
    ended_at: datetime


class Egress:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, org_id: str, connections: Sequence[Connection]) -> None:
        async with self._db.org(org_id) as conn, conn.cursor() as cursor:
            await cursor.executemany(
                f"INSERT INTO egress (org_id, {_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(org_id, c.subject_id, c.host, c.port, c.allowed, c.reason, c.bytes_up, c.bytes_down,
                  c.started_at, c.ended_at) for c in connections])

    async def of(self, org_id: str, subject_id: str) -> list[Connection]:
        """A turn's or an install's connections, in the order they were recorded."""
        async with self._db.org(org_id) as conn:
            rows = await (await conn.execute(
                f"SELECT {_COLUMNS} FROM egress WHERE org_id = %s AND subject_id = %s ORDER BY id",
                [org_id, subject_id])).fetchall()
        return [Connection(**row) for row in rows]
