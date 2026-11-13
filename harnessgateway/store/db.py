"""PostgreSQL: the connection pool, schema changes, and transactions (spec §17)."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

MIGRATIONS = Path(__file__).parent / "migrations"
_MIGRATION_LOCK = 0x6867_6D69_6772            # "hgmigr": one process changes the schema at a time

Connection = AsyncConnection[dict[str, Any]]


class Listener:
    """A connection that hears what is sent on the channels it listens to."""

    def __init__(self, conn: AsyncConnection[Any]) -> None:
        self._conn = conn

    async def next(self, timeout_s: float) -> str | None:
        """The next message's text; or None when none comes in time."""
        async for message in self._conn.notifies(timeout=timeout_s, stop_after=1):
            return message.payload
        return None


class Database:
    def __init__(self, url: str, *, max_size: int = 10) -> None:
        self._url = url
        self._pool: AsyncConnectionPool[Connection] = AsyncConnectionPool(
            url, min_size=1, max_size=max_size, open=False, connection_class=AsyncConnection[dict[str, Any]],
            kwargs={"row_factory": dict_row},
        )

    async def open(self) -> None:
        await self._pool.open(wait=True)

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def system(self) -> AsyncIterator[Connection]:
        """A transaction on what belongs to the installation, not to one organization."""
        async with self._pool.connection() as conn, conn.transaction():
            yield conn

    @asynccontextmanager
    async def org(self, org_id: str) -> AsyncIterator[Connection]:
        """A transaction on one organization's rows. It carries the organization's id, for row-level security."""
        async with self.system() as conn:
            await conn.execute("SELECT set_config('hg.org_id', %s, true)", [org_id])
            yield conn

    @asynccontextmanager
    async def listen(self, *channels: str) -> AsyncIterator[Listener]:
        """Hear what is sent on these channels, on a connection of its own for as long as the block lasts."""
        conn = await AsyncConnection.connect(self._url, autocommit=True)
        try:
            for channel in channels:
                await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
            yield Listener(conn)
        finally:
            await conn.close()

    async def migrate(self, folders: Sequence[Path] = (MIGRATIONS,)) -> list[str]:
        """Apply every schema file not yet applied: folders in the order given, files by name. All or nothing."""
        applied = []
        async with self.system() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", [_MIGRATION_LOCK])
            await conn.execute("CREATE TABLE IF NOT EXISTS migrations "
                               "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            done = {row["name"] for row in await (await conn.execute("SELECT name FROM migrations")).fetchall()}
            for folder in folders:
                for path in sorted(folder.glob("*.sql")):
                    if path.name in done:
                        continue
                    await conn.execute(path.read_text().encode())
                    await conn.execute("INSERT INTO migrations (name) VALUES (%s)", [path.name])
                    applied.append(path.name)
        return applied
