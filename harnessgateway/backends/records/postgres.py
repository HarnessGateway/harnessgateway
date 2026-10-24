"""Records in Postgres: many replicas, one database."""
from __future__ import annotations

import json
import time

from typing import Any

from . import SCHEMA, Doc, SqlRecords, checked_key


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
