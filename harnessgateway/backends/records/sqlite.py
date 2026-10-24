"""Records in one SQLite file: the one-box deployment and development."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import SCHEMA, SqlRecords, checked_key


class SqliteRecords(SqlRecords):
    def __init__(self, path: Path | str):
        super().__init__()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=30000")
        for stmt in SCHEMA:
            self._db.execute(stmt.format(doc="TEXT"))

    def _field(self, name: str) -> str:
        return f"json_extract(doc, '$.{checked_key(name)}')"

    def _run(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def close(self) -> None:
        self._db.close()
