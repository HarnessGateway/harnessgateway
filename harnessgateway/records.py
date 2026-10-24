"""Layer 3: what the platform keeps, as records: one table of (kind, tenant, key, doc), the doc a JSON
object; `tenant` is the organization's name, and a platform-level record has the empty tenant. SQLite
on one box, Postgres for many; the same statements run on both. Keys are unique per kind across
organizations, so a response or a token can be found without knowing whose it is.

Also the queue: a turn is a record whose `status` moves from queued to claimed by a compare-and-set,
and the output chunks are records listed by prefix. No broker.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Protocol

KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
Doc = dict[str, Any]


def checked_key(key: str) -> str:
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise ValueError(f"a record key is letters, digits, dots, dashes, colons and underscores: {key!r}")
    return key


class Records(Protocol):
    def get(self, kind: str, tenant: str, key: str) -> Doc | None: ...
    def put(self, kind: str, tenant: str, key: str, doc: Doc) -> None: ...
    def delete(self, kind: str, tenant: str, key: str) -> bool: ...
    def list(self, kind: str, tenant: str, prefix: str = "") -> list[Doc]: ...
    def find(self, kind: str, key: str) -> tuple[str, Doc] | None: ...
    def update_if(self, kind: str, tenant: str, key: str, field: str, expected: Any, doc: Doc) -> bool: ...
    def where(self, kind: str, field: str, value: Any, limit: int = 100) -> list[tuple[str, str, Doc]]: ...
    def count(self, kind: str, tenant: str, field: str, value: Any) -> int: ...
    def close(self) -> None: ...


class SqlRecords:
    """The shared implementation; a subclass supplies the connection, the placeholder and the JSON
    field expression."""
    placeholder = "?"

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def _field(self, name: str) -> str:
        raise NotImplementedError

    def _run(self, sql: str, args: tuple = ()) -> list[tuple]:
        raise NotImplementedError

    def _q(self, sql: str) -> str:
        return sql.replace("?", self.placeholder)

    @staticmethod
    def _doc(value: Any) -> Doc:
        return value if isinstance(value, dict) else json.loads(value)

    def get(self, kind: str, tenant: str, key: str) -> Doc | None:
        rows = self._run(self._q("SELECT doc FROM records WHERE kind=? AND tenant=? AND key=?"), (kind, tenant, checked_key(key)))
        return self._doc(rows[0][0]) if rows else None

    def put(self, kind: str, tenant: str, key: str, doc: Doc) -> None:
        self._run(self._q("INSERT INTO records (kind, tenant, key, doc, created) VALUES (?, ?, ?, ?, ?) "
                          "ON CONFLICT (kind, tenant, key) DO UPDATE SET doc = excluded.doc"),
                  (kind, tenant, checked_key(key), json.dumps(doc), time.time()))

    def delete(self, kind: str, tenant: str, key: str) -> bool:
        return self._run(self._q("DELETE FROM records WHERE kind=? AND tenant=? AND key=? RETURNING key"),
                         (kind, tenant, checked_key(key))) != []

    def list(self, kind: str, tenant: str, prefix: str = "") -> list[Doc]:
        rows = self._run(self._q("SELECT doc FROM records WHERE kind=? AND tenant=? AND key LIKE ? ESCAPE '\\' ORDER BY key"),
                         (kind, tenant, prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"))
        return [self._doc(r[0]) for r in rows]

    def find(self, kind: str, key: str) -> tuple[str, Doc] | None:
        rows = self._run(self._q("SELECT tenant, doc FROM records WHERE kind=? AND key=?"), (kind, checked_key(key)))
        return (rows[0][0], self._doc(rows[0][1])) if rows else None

    def update_if(self, kind: str, tenant: str, key: str, field: str, expected: Any, doc: Doc) -> bool:
        rows = self._run(self._q(f"UPDATE records SET doc=? WHERE kind=? AND tenant=? AND key=? AND {self._field(field)}=? RETURNING key"),
                         (json.dumps(doc), kind, tenant, checked_key(key), str(expected)))
        return rows != []

    def where(self, kind: str, field: str, value: Any, limit: int = 100) -> list[tuple[str, str, Doc]]:
        rows = self._run(self._q(f"SELECT tenant, key, doc FROM records WHERE kind=? AND {self._field(field)}=? ORDER BY created LIMIT ?"),
                         (kind, str(value), limit))
        return [(t, k, self._doc(d)) for t, k, d in rows]

    def count(self, kind: str, tenant: str, field: str, value: Any) -> int:
        rows = self._run(self._q(f"SELECT COUNT(*) FROM records WHERE kind=? AND tenant=? AND {self._field(field)}=?"),
                         (kind, tenant, str(value)))
        return int(rows[0][0])


SCHEMA = ("CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, tenant TEXT NOT NULL, key TEXT NOT NULL, "
          "doc {doc} NOT NULL, created DOUBLE PRECISION NOT NULL, PRIMARY KEY (kind, tenant, key))",
          "CREATE INDEX IF NOT EXISTS records_kind_key ON records (kind, key)",
          "CREATE INDEX IF NOT EXISTS records_kind_created ON records (kind, created)")


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


class PostgresRecords(SqlRecords):
    placeholder = "%s"

    def __init__(self, dsn: str):
        super().__init__()
        import psycopg
        self._db = psycopg.connect(dsn, autocommit=True)
        for stmt in SCHEMA:
            self._db.execute(stmt.format(doc="JSONB"))

    def _field(self, name: str) -> str:
        return f"(doc->>'{checked_key(name)}')"

    def _run(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            cur = self._db.execute(sql, args)
            return cur.fetchall() if cur.description else []

    def put(self, kind: str, tenant: str, key: str, doc: Doc) -> None:
        self._run("INSERT INTO records (kind, tenant, key, doc, created) VALUES (%s, %s, %s, %s::jsonb, %s) "
                  "ON CONFLICT (kind, tenant, key) DO UPDATE SET doc = excluded.doc",
                  (kind, tenant, checked_key(key), json.dumps(doc), time.time()))

    def update_if(self, kind: str, tenant: str, key: str, field: str, expected: Any, doc: Doc) -> bool:
        rows = self._run(f"UPDATE records SET doc=%s::jsonb WHERE kind=%s AND tenant=%s AND key=%s AND {self._field(field)}=%s RETURNING key",
                         (json.dumps(doc), kind, tenant, checked_key(key), str(expected)))
        return rows != []

    def close(self) -> None:
        self._db.close()


def open_records(url: str) -> Records:
    """`sqlite:///path/to/file.db` or a Postgres URL (`postgresql://…`)."""
    if url.startswith("sqlite:///"):
        return SqliteRecords(url[len("sqlite:///"):])
    if url.startswith(("postgresql://", "postgres://")):
        return PostgresRecords(url)
    raise ValueError(f"records are sqlite:///path or postgresql://…, not {url!r}")
