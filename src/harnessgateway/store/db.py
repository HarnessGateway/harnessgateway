"""PostgreSQL: the connection pool, the schema, transactions, and hearing what other processes announce."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

SCHEMA = Path(__file__).parent / "schema"
_MIGRATING = 0x6867_6D69_6772                  # an advisory lock: one process changes the schema at a time

Connection = AsyncConnection[dict[str, Any]]


async def announce(conn: Connection, channel: str, text: str = "") -> None:
    """Tell every listener on a channel, when this connection's transaction commits."""
    await conn.execute("SELECT pg_notify(%s, %s)", [channel, text])


class Listener:
    """A connection that hears what is announced on the channels it listens to."""

    def __init__(self, conn: AsyncConnection[Any]) -> None:
        self._conn = conn

    async def next(self, timeout_s: float) -> str | None:
        """The next announcement's text; or None when none comes in time."""
        async for message in self._conn.notifies(timeout=timeout_s, stop_after=1):
            return message.payload
        return None


class Database:
    def __init__(self, url: str, *, max_size: int = 20) -> None:
        self._url = url
        self._pool: AsyncConnectionPool[Connection] = AsyncConnectionPool(
            url, min_size=1, max_size=max_size, open=False, kwargs={"row_factory": dict_row})

    async def open(self) -> None:
        await self._pool.open(wait=True)

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def tx(self, org_id: str | None = None) -> AsyncIterator[Connection]:
        """A transaction. When it is on one organization's rows it carries that organization's id as the setting
        `hg.org_id`: the core's own queries name the organization in every WHERE, and an edition with many
        organizations adds row-level policies on this setting as a second wall."""
        async with self._pool.connection() as conn, conn.transaction():
            if org_id is not None:
                await conn.execute("SELECT set_config('hg.org_id', %s, true)", [org_id])
            yield conn

    async def one(self, org_id: str | None, query: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        async with self.tx(org_id) as conn:
            return await (await conn.execute(query, params)).fetchone()

    async def all(self, org_id: str | None, query: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        async with self.tx(org_id) as conn:
            return await (await conn.execute(query, params)).fetchall()

    async def run(self, org_id: str | None, query: str, params: Sequence[Any] = ()) -> int:
        """Run one statement. How many rows it changed."""
        async with self.tx(org_id) as conn:
            return (await conn.execute(query, params)).rowcount

    @asynccontextmanager
    async def listen(self, channel: str) -> AsyncIterator[Listener]:
        """Hear a channel, on a connection of its own for as long as the block lasts."""
        conn = await AsyncConnection.connect(self._url, autocommit=True)
        try:
            await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
            yield Listener(conn)
        finally:
            await conn.close()

    async def migrate(self, folders: Sequence[Path] = (SCHEMA,)) -> list[str]:
        """Apply every schema file not yet applied: folders in the order given, files by name. All or nothing.
        A released file is never edited: installations are updated in place, so a change is a new file."""
        applied = []
        async with self.tx() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", [_MIGRATING])
            await conn.execute("CREATE TABLE IF NOT EXISTS migrations "
                               "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            done = {row["name"] for row in await (await conn.execute("SELECT name FROM migrations")).fetchall()}
            for path in (path for folder in folders for path in sorted(folder.glob("*.sql"))):
                if path.name not in done:
                    await conn.execute(path.read_text().encode())
                    await conn.execute("INSERT INTO migrations (name) VALUES (%s)", [path.name])
                    applied.append(path.name)
        return applied
