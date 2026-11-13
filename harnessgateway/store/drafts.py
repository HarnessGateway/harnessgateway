"""Harness packages being written, before they are installed (spec §14.2)."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from psycopg.errors import UniqueViolation

from harnessgateway.domain.archive import EXECUTABLES, FILES_MAX, PACKED_MAX, PackageFile, package_path
from harnessgateway.domain.names import check_name
from harnessgateway.errors import Conflict, NotFound, TooLarge
from harnessgateway.store.db import Connection, Database

FILE_MAX = 4 * 1024 * 1024                     # one file written through the API: drafts are edited by hand
DRAFT_MAX = PACKED_MAX                         # a whole draft
PATH_MAX = 512


@dataclass(frozen=True)
class DraftFile:
    path: str
    bytes: int
    executable: bool


@dataclass(frozen=True)
class Draft:
    name: str
    based_on: str
    updated_at: datetime
    files: tuple[DraftFile, ...]               # in path order


def draft_path(value: str) -> str:
    path = package_path(value)
    if len(path) > PATH_MAX:
        raise TooLarge(f"a path in a draft is at most {PATH_MAX} characters")
    return path


class Drafts:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, org_id: str, name: str, based_on: str, files: Iterable[PackageFile]) -> Draft:
        """A new draft holding these files. A draft of that name must not exist."""
        check_name(name, "name")
        files = list(files)
        if len(files) > FILES_MAX:
            raise TooLarge(f"a draft holds at most {FILES_MAX} files")
        if sum(len(file.content) for file in files) > DRAFT_MAX:
            raise TooLarge(f"a draft is at most {DRAFT_MAX >> 20} MiB")
        try:
            async with self._db.org(org_id) as conn:
                await conn.execute("INSERT INTO drafts (org_id, name, based_on) VALUES (%s, %s, %s)",
                                   [org_id, name, based_on])
                for file in files:
                    await conn.execute(
                        "INSERT INTO draft_files (org_id, draft, path, executable, content) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        [org_id, name, draft_path(file.path), file.executable, file.content])
        except UniqueViolation:
            raise Conflict(f"there is already a draft named {name!r}") from None
        return await self.get(org_id, name)

    async def all(self, org_id: str) -> list[Draft]:
        async with self._db.org(org_id) as conn:
            names = await (await conn.execute("SELECT name FROM drafts WHERE org_id = %s ORDER BY name",
                                              [org_id])).fetchall()
            return [await self._get(conn, org_id, row["name"]) for row in names]

    async def get(self, org_id: str, name: str) -> Draft:
        async with self._db.org(org_id) as conn:
            return await self._get(conn, org_id, name)

    @staticmethod
    async def _get(conn: Connection, org_id: str, name: str) -> Draft:
        row = await (await conn.execute("SELECT name, based_on, updated_at FROM drafts WHERE org_id = %s AND name = %s",
                                        [org_id, name])).fetchone()
        if row is None:
            raise NotFound(f"there is no draft named {name!r}")
        files = await (await conn.execute(
            "SELECT path, octet_length(content) AS bytes, executable FROM draft_files "
            "WHERE org_id = %s AND draft = %s ORDER BY path", [org_id, name])).fetchall()
        return Draft(files=tuple(DraftFile(**file) for file in files), **row)

    async def files(self, org_id: str, name: str) -> list[PackageFile]:
        """Everything in a draft, as the files of a package."""
        async with self._db.org(org_id) as conn:
            await self._get(conn, org_id, name)
            rows = await (await conn.execute(
                "SELECT path, executable, content FROM draft_files WHERE org_id = %s AND draft = %s ORDER BY path",
                [org_id, name])).fetchall()
        return [PackageFile(row["path"], row["executable"], bytes(row["content"])) for row in rows]

    async def read(self, org_id: str, name: str, path: str) -> bytes:
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                "SELECT content FROM draft_files WHERE org_id = %s AND draft = %s AND path = %s",
                [org_id, name, path])).fetchone()
        if row is None:
            raise NotFound(f"draft {name!r} has no file {path!r}")
        return bytes(row["content"])

    async def write(self, org_id: str, name: str, path: str, content: bytes) -> DraftFile:
        """Write one file. `install` and `run` are executable; any other file keeps what it was."""
        path = draft_path(path)
        if len(content) > FILE_MAX:
            raise TooLarge(f"a file written to a draft is at most {FILE_MAX >> 20} MiB")
        async with self._db.org(org_id) as conn:
            draft = await self._get(conn, org_id, name)
            others = [file for file in draft.files if file.path != path]
            if len(others) >= FILES_MAX:
                raise TooLarge(f"a draft holds at most {FILES_MAX} files")
            if sum(file.bytes for file in others) + len(content) > DRAFT_MAX:
                raise TooLarge(f"a draft is at most {DRAFT_MAX >> 20} MiB")
            row = await (await conn.execute(
                "INSERT INTO draft_files (org_id, draft, path, executable, content) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (org_id, draft, path) DO UPDATE SET content = EXCLUDED.content, "
                "executable = draft_files.executable OR EXCLUDED.executable "
                "RETURNING path, octet_length(content) AS bytes, executable",
                [org_id, name, path, path in EXECUTABLES, content])).fetchone()
            await conn.execute("UPDATE drafts SET updated_at = now() WHERE org_id = %s AND name = %s", [org_id, name])
        assert row is not None
        return DraftFile(**row)

    async def remove_file(self, org_id: str, name: str, path: str) -> None:
        async with self._db.org(org_id) as conn:
            await self._get(conn, org_id, name)
            gone = await conn.execute("DELETE FROM draft_files WHERE org_id = %s AND draft = %s AND path = %s",
                                      [org_id, name, path])
            if gone.rowcount == 0:
                raise NotFound(f"draft {name!r} has no file {path!r}")
            await conn.execute("UPDATE drafts SET updated_at = now() WHERE org_id = %s AND name = %s", [org_id, name])

    async def delete(self, org_id: str, name: str) -> None:
        async with self._db.org(org_id) as conn:
            gone = await conn.execute("DELETE FROM drafts WHERE org_id = %s AND name = %s", [org_id, name])
        if gone.rowcount == 0:
            raise NotFound(f"there is no draft named {name!r}")
