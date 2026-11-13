"""The audit record: who changed what, and when (spec §22.2)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from harnessgateway.domain.principal import Principal
from harnessgateway.store.db import Database

_COLUMNS = "id, at, key_id, action, target, detail"


@dataclass(frozen=True)
class AuditRecord:
    id: int
    at: datetime
    key_id: str | None
    action: str
    target: str
    detail: dict[str, Any]


class Audit:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(self, principal: Principal, action: str, target: str,
                     detail: Mapping[str, Any] | None = None) -> AuditRecord:
        """Record a change. `detail` says which fields changed, never what a secret was changed to."""
        async with self._db.org(principal.org_id) as conn:
            row = await (await conn.execute(
                f"INSERT INTO audit (org_id, key_id, action, target, detail) VALUES (%s, %s, %s, %s, %s) "
                f"RETURNING {_COLUMNS}",
                [principal.org_id, principal.key_id, action, target, Jsonb(dict(detail or {}))])).fetchone()
        assert row is not None
        return AuditRecord(**row)

    async def page(self, org_id: str, *, after: int | None = None, limit: int = 20) -> list[AuditRecord]:
        """Records, newest first; those before `after` when it is given."""
        async with self._db.org(org_id) as conn:
            rows = await (await conn.execute(
                f"SELECT {_COLUMNS} FROM audit WHERE org_id = %s AND (%s::bigint IS NULL OR id < %s) "
                "ORDER BY id DESC LIMIT %s", [org_id, after, after, limit])).fetchall()
        return [AuditRecord(**row) for row in rows]
