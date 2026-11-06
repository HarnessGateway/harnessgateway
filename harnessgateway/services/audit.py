"""The audit log (SPEC §10.4): every change of standing, as a row that never holds a secret. Admins read their
organization's rows; the platform scope reads all."""
from __future__ import annotations

from typing import Any

from .. import ids
from ..store.db import Database
from .pages import check_limit, page, ts


def _view(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": r["id"], "object": "audit", "at": ts(r["at"]), "org_id": r["org_id"], "actor": r["actor"],
            "action": r["action"], "target": r["target"], "detail": r["detail"]}


class Audit:
    def __init__(self, db: Database):
        self.db = db

    async def write(self, org_id: str | None, actor: str, action: str, target: str = "",
                    detail: dict[str, Any] | None = None) -> None:
        async with self.db.platform() as tx:
            await tx.run("INSERT INTO audit (id, org_id, actor, action, target, detail) VALUES (%s, %s, %s, %s, %s, %s)",
                         ids.new("audit"), org_id, actor[:200], action[:100], target[:200], detail or {})

    async def list(self, org_id: str | None, limit: int, after: str | None) -> dict[str, Any]:
        """Newest first; `org_id` None is the platform's view of every organization's rows and its own."""
        limit = check_limit(limit)
        where = "" if org_id is None else "org_id = %s AND "
        args: list[Any] = [] if org_id is None else [org_id]
        async with self.db.platform() as tx:
            rows = await tx.all(f"SELECT * FROM audit WHERE {where}"
                                "(%s::text IS NULL OR at < (SELECT at FROM audit WHERE id = %s)) "
                                "ORDER BY at DESC, id DESC LIMIT %s", *args, after, after, limit + 1)
        return page([_view(r) for r in rows], limit)
