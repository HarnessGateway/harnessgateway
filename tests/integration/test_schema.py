"""The schema: files applied once, each recorded with its owner, and a database newer than the code refused."""

from pathlib import Path

import pytest

from harnessgateway.store.db import CORE, SCHEMA, Database, NewerDatabase

HERE = Path(__file__).parent
SAMPLE, SECOND = ("sample", HERE / "edition_schema"), ("second", HERE / "second_schema")


async def owned(db: Database) -> list[tuple[str, str]]:
    return [(row["owner"], row["name"]) for row in await db.all(None, "SELECT owner, name FROM migrations ORDER BY owner, name")]


async def test_each_file_is_applied_once_and_two_owners_may_each_have_a_file_of_one_name(db: Database):
    assert await db.migrate([(CORE, SCHEMA), SAMPLE, SECOND]) == [
        "core/0001_core.sql", "core/0002_keys.sql", "core/0003_files.sql", "sample/0001_edition.sql", "second/0001_edition.sql"]
    assert await db.migrate([(CORE, SCHEMA), SAMPLE, SECOND]) == []                       # again: nothing to do
    assert await owned(db) == [("core", "0001_core.sql"), ("core", "0002_keys.sql"), ("core", "0003_files.sql"), ("sample", "0001_edition.sql"),
                               ("second", "0001_edition.sql")]
    assert await db.all(None, "SELECT count(*) AS rows FROM sample_audit") == [{"rows": 0}]
    assert await db.all(None, "SELECT count(*) AS rows FROM second_notes") == [{"rows": 0}]


async def test_a_file_that_fails_leaves_nothing_of_its_run_behind(db: Database, tmp_path: Path):
    (tmp_path / "0001_good.sql").write_text("CREATE TABLE made_first (id int);")
    (tmp_path / "0002_bad.sql").write_text("CREATE TABLE made_second (id int); SELECT no_such_function();")
    with pytest.raises(Exception, match="no_such_function"):
        await db.migrate([(CORE, SCHEMA), ("broken", tmp_path)])
    assert await db.all(None, "SELECT to_regclass('made_first') AS first, to_regclass('orgs') AS core") == [{"first": None, "core": None}]


async def test_a_database_a_later_version_changed_is_refused_and_an_owner_that_is_not_loaded_is_left_alone(db: Database, tmp_path: Path):
    later = tmp_path / "later"
    later.mkdir()
    for file in SCHEMA.glob("*.sql"):
        (later / file.name).write_text(file.read_text())
    (later / "0009_later.sql").write_text("CREATE TABLE made_later (id int);")
    await db.migrate([(CORE, later), SAMPLE])                                            # a later version of the core ran here
    with pytest.raises(NewerDatabase, match=r"changed by a later version than this one \(it has core/0009_later.sql\)"):
        await db.migrate([(CORE, SCHEMA), SAMPLE])                                       # this version must not
    with pytest.raises(NewerDatabase):
        await db.migrate([(CORE, SCHEMA)])
    assert await db.migrate([(CORE, later)]) == []                                       # the edition is gone, and its tables stay:
    assert ("sample", "0001_edition.sql") in await owned(db)                             # going back to the core is allowed
    assert await db.all(None, "SELECT count(*) AS rows FROM sample_audit") == [{"rows": 0}]
