"""PostgreSQL: the connection pool, the schema, transactions, and hearing what other processes announce."""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from harnessgateway.domain.change import Change

SCHEMA = Path(__file__).parent / "schema"
CORE = "core"                                  # who owns the schema files of this package
_MIGRATING = 0x6867_6D69_6772                  # an advisory lock: one process changes the schema at a time

Connection = AsyncConnection[dict[str, Any]]
Tell = Callable[[Connection, Change], Awaitable[None]]         # how a store tells of a change, in its transaction


class NewerDatabase(Exception):
    """The database's schema was changed by a later version of the program than this one. Nothing is started."""


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

    async def migrate(self, folders: Sequence[tuple[str, Path]] = ((CORE, SCHEMA),)) -> list[str]:
        """Apply every schema file not yet applied: each owner's folder in the order given, its files by name. All
        or nothing. Each file is recorded with its owner, so two owners may each have a file of one name. A
        released file is never edited: installations are updated in place, so a change is a new file.

        NewerDatabase when the record names a file of one of these owners that its folder does not hold: a later
        version changed the schema, and this one must not run on it. An owner that is not among these is left
        alone: its tables stay as they were."""
        files = [(owner, path) for owner, folder in folders for path in sorted(folder.glob("*.sql"))]
        assert len({owner for owner, _ in folders}) == len(folders), "each owner has one folder of schema files"
        applied = []
        async with self.tx() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", [_MIGRATING])
            await conn.execute("CREATE TABLE IF NOT EXISTS migrations (owner text NOT NULL, name text NOT NULL, "
                               "applied_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (owner, name))")
            rows = await (await conn.execute("SELECT owner, name FROM migrations")).fetchall()
            done = {(row["owner"], row["name"]) for row in rows}
            unknown = sorted(f"{owner}/{name}" for owner, name in done - {(owner, path.name) for owner, path in files}
                             if owner in dict(folders))
            if unknown:
                raise NewerDatabase(f"the database was changed by a later version than this one (it has "
                                    f"{', '.join(unknown)}): run that version, or a later one")
            for owner, path in files:
                if (owner, path.name) not in done:
                    await conn.execute(path.read_text().encode())
                    await conn.execute("INSERT INTO migrations (owner, name) VALUES (%s, %s)", [owner, path.name])
                    applied.append(f"{owner}/{path.name}")
        return applied
