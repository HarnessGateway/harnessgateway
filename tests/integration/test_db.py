"""Spec §17: schema changes are numbered files applied in order, once; organization rows are scoped."""

from pathlib import Path

import psycopg
import pytest

from harnessgateway.store.db import MIGRATIONS, Database


async def test_the_schema_is_applied_and_recorded(db: Database) -> None:
    async with db.system() as conn:
        rows = await (await conn.execute("SELECT name FROM migrations ORDER BY name")).fetchall()
    applied = [row["name"] for row in rows]
    core = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    assert core
    assert set(core) <= set(applied)                    # and, with another edition loaded, that edition's too


async def test_migrating_again_changes_nothing(db: Database) -> None:
    assert await db.migrate() == []


async def test_an_extension_s_folder_is_applied_after_the_core_s_and_once(db: Database, tmp_path: Path) -> None:
    (tmp_path / "ext_0001_things.sql").write_text("CREATE TABLE ext_things (id text PRIMARY KEY);")
    (tmp_path / "ext_0002_more.sql").write_text("ALTER TABLE ext_things ADD COLUMN note text;")
    try:
        assert await db.migrate([MIGRATIONS, tmp_path]) == ["ext_0001_things.sql", "ext_0002_more.sql"]
        assert await db.migrate([MIGRATIONS, tmp_path]) == []
        async with db.system() as conn:
            await conn.execute("INSERT INTO ext_things (id, note) VALUES ('a', 'b')")
    finally:
        async with db.system() as conn:
            await conn.execute("DROP TABLE IF EXISTS ext_things")
            await conn.execute("DELETE FROM migrations WHERE name LIKE 'ext_%'")


async def test_a_failing_migration_applies_nothing_and_is_not_recorded(db: Database, tmp_path: Path) -> None:
    (tmp_path / "bad_0001.sql").write_text("CREATE TABLE bad_one (id text); SELECT no_such_function();")
    with pytest.raises(psycopg.Error):
        await db.migrate([tmp_path])
    async with db.system() as conn:
        row = await (await conn.execute("SELECT to_regclass('bad_one') AS t")).fetchone()
        recorded = await (await conn.execute("SELECT 1 FROM migrations WHERE name = 'bad_0001.sql'")).fetchone()
    assert row == {"t": None}
    assert recorded is None


async def test_an_organization_s_transaction_carries_its_id(db: Database, org: str) -> None:
    async with db.org(org) as conn:
        row = await (await conn.execute("SELECT current_setting('hg.org_id') AS org")).fetchone()
    assert row == {"org": org}
    async with db.system() as conn:
        row = await (await conn.execute("SELECT current_setting('hg.org_id', true) AS org")).fetchone()
    assert row in ({"org": None}, {"org": ""})


async def test_a_transaction_that_raises_changes_nothing(db: Database, org: str) -> None:
    async def change_then_fail() -> None:
        async with db.org(org) as conn:
            await conn.execute("UPDATE orgs SET name = 'changed' WHERE id = %s", [org])
            raise RuntimeError("stop")

    with pytest.raises(RuntimeError):
        await change_then_fail()
    async with db.system() as conn:
        row = await (await conn.execute("SELECT name FROM orgs WHERE id = %s", [org])).fetchone()
    assert row == {"name": "Test"}
