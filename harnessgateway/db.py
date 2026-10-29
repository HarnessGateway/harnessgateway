"""Postgres, the one system of record. Every unit of work runs in a transaction that first declares its scope: one
organization (`tenant`) or the platform itself (`platform`); row-level security does the rest. Queries are plain SQL
with parameters; rows come back as dicts. Postgres is not behind a port: it is the standard the core is written
against, and the same code runs on a container, RDS, Cloud SQL, Azure or any other Postgres 16+."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from importlib import resources
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

Row = dict[str, Any]
CORE_MIGRATIONS = "harnessgateway.migrations"


class Tx:
    """One transaction, scoped. The only object services use to touch the database."""

    def __init__(self, cx: AsyncConnection[DictRow]):
        self._cx = cx

    async def one(self, sql: str, *args: Any) -> Row | None:
        return await (await self._cx.execute(sql, _params(args))).fetchone()

    async def all(self, sql: str, *args: Any) -> list[Row]:
        return await (await self._cx.execute(sql, _params(args))).fetchall()

    async def run(self, sql: str, *args: Any) -> int:
        return (await self._cx.execute(sql, _params(args))).rowcount

    async def many(self, sql: str, rows: Iterable[tuple[Any, ...]]) -> None:
        """One statement for many parameter rows, pipelined."""
        params = [_params(r) for r in rows]
        if params:
            async with self._cx.cursor() as cur:
                await cur.executemany(sql, params)

    async def get(self, sql: str, *args: Any) -> Row:
        """The one row the caller knows exists (a row it holds or just wrote); its absence is a bug."""
        row = await self.one(sql, *args)
        assert row is not None, f"expected a row: {sql[:80]}"
        return row

    async def value(self, sql: str, *args: Any) -> Any:
        row = await self.one(sql, *args)
        return next(iter(row.values())) if row else None

    async def notify(self, channel: str, payload: str) -> None:
        await self._cx.execute("SELECT pg_notify(%s, %s)", (channel, payload))


def _params(args: tuple[Any, ...]) -> tuple[Any, ...]:
    """Dicts are stored as jsonb; lists stay Postgres arrays (for = ANY(...))."""
    return tuple(Jsonb(a) if isinstance(a, dict) else a for a in args)


class Database:
    def __init__(self, url: str, *, min_size: int = 2, max_size: int = 20):
        self.url = url
        self._pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
            url, min_size=min_size, max_size=max_size, open=False, kwargs={"row_factory": dict_row, "autocommit": False})

    async def open(self) -> None:
        await self._pool.open(wait=True, timeout=30)

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def tenant(self, org_id: str) -> AsyncIterator[Tx]:
        assert org_id.startswith("org_"), "a tenant transaction names an organization"
        async with self._pool.connection() as cx, cx.transaction():
            await cx.execute("SELECT set_config('hg.org', %s, true), set_config('hg.scope', 'tenant', true)", (org_id,))
            yield Tx(cx)

    @asynccontextmanager
    async def platform(self) -> AsyncIterator[Tx]:
        async with self._pool.connection() as cx, cx.transaction():
            await cx.execute("SELECT set_config('hg.scope', 'platform', true), set_config('hg.org', '', true)")
            yield Tx(cx)

    @asynccontextmanager
    async def listen(self, channel: str) -> AsyncIterator[AsyncConnection]:
        """A dedicated autocommit connection subscribed to a NOTIFY channel."""
        cx = await AsyncConnection.connect(self.url, autocommit=True)
        try:
            await cx.execute(f'LISTEN "{channel}"')
            yield cx
        finally:
            await cx.close()

    async def check_role(self) -> None:
        """Refuse to run as a role that bypasses row-level security."""
        async with self._pool.connection() as cx:
            row = await Tx(cx).one("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        if row and (row["rolsuper"] or row["rolbypassrls"]):
            raise SystemExit("configuration error: the database role is a superuser or has BYPASSRLS, which would "
                             "disable tenant isolation; connect HG_DATABASE_URL as an ordinary role that owns the database")

    async def migrate(self, packages: Iterable[str] = (CORE_MIGRATIONS,)) -> list[str]:
        """Apply every migration not yet applied, package by package (the core first, then each extension's), each file
        in its own transaction, under an advisory lock so processes starting together do not race."""
        applied: list[str] = []
        async with await AsyncConnection.connect(self.url, autocommit=True) as cx:
            await cx.execute("SELECT pg_advisory_lock(727274)")
            try:
                await cx.execute("CREATE TABLE IF NOT EXISTS schema_migrations (package text NOT NULL, version integer NOT "
                                 "NULL, applied_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (package, version))")
                for package in packages:
                    done = {r[0] for r in await (await cx.execute(
                        "SELECT version FROM schema_migrations WHERE package = %s", (package,))).fetchall()}
                    files = sorted(n.name for n in resources.files(package).iterdir() if n.name.endswith(".sql"))
                    for name in files:
                        version = int(name.split("_", 1)[0])
                        if version in done:
                            continue
                        sql = resources.files(package).joinpath(name).read_text()
                        async with cx.transaction():
                            await cx.execute(sql)
                            await cx.execute("INSERT INTO schema_migrations (package, version) VALUES (%s, %s)",
                                             (package, version))
                        applied.append(f"{package}:{name}")
            finally:
                await cx.execute("SELECT pg_advisory_unlock(727274)")
        return applied


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
