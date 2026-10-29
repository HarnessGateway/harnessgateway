"""Organizations: the tenant (SPEC §9). Self-hosted has one, made by `harnessgateway init`; a SaaS extension makes one
per customer with the same `create`. An organization holds its capacity, its egress entries and its data key."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from .. import ids
from ..crypto import DataKeys
from ..db import Database
from ..domain import egress
from ..domain.capacity import Capacity
from ..errors import Conflict, Invalid, NotFound
from ..limits import EGRESS_MAX
from .pages import ts


class Orgs:
    def __init__(self, db: Database, data_keys: DataKeys, defaults: Capacity):
        self.db, self.data_keys, self.defaults = db, data_keys, defaults

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "object": "organization", "name": row["name"], "status": row["status"],
                "capacity": Capacity.parse(row["capacity"], self.defaults).model_dump(), "egress": row["egress"],
                "created_at": ts(row["created_at"])}

    async def create(self, name: str, capacity: dict[str, Any] | None = None) -> dict[str, Any]:
        ids.check_name(name, "organization")
        try:
            Capacity.parse(capacity or {}, self.defaults)
        except ValueError as e:
            raise Invalid(str(e), param="capacity") from None
        org_id = ids.new("org")
        wrapped = await asyncio.to_thread(self.data_keys.new, org_id)
        async with self.db.platform() as tx:
            if await tx.one("SELECT 1 FROM orgs WHERE name = %s", name):
                raise Conflict(f"an organization named {name} exists", param="name")
            await tx.run("INSERT INTO orgs (id, name, capacity, data_key) VALUES (%s, %s, %s, %s)",
                         org_id, name, capacity or {}, wrapped)
            return self._view(await tx.get("SELECT * FROM orgs WHERE id = %s", org_id))

    async def get(self, org_id: str) -> dict[str, Any]:
        async with self.db.platform() as tx:
            row = await tx.one("SELECT * FROM orgs WHERE id = %s", org_id)
        if not row:
            raise NotFound(f"no organization {org_id}")
        return self._view(row)

    async def first(self) -> dict[str, Any] | None:
        async with self.db.platform() as tx:
            row = await tx.one("SELECT * FROM orgs ORDER BY created_at LIMIT 1")
        return self._view(row) if row else None

    async def capacity(self, org_id: str) -> Capacity:
        return Capacity.model_validate((await self.get(org_id))["capacity"])

    async def update(self, org_id: str, *, capacity: dict[str, Any] | None = None, status: str | None = None
                     ) -> dict[str, Any]:
        """Capacity fields given replace the stored ones (the rest keep falling back to the configured defaults)."""
        if status is not None and status not in ("active", "suspended"):
            raise Invalid("status is active or suspended", param="status")
        async with self.db.platform() as tx:
            row = await tx.one("SELECT capacity FROM orgs WHERE id = %s FOR UPDATE", org_id)
            if not row:
                raise NotFound(f"no organization {org_id}")
            merged = {**row["capacity"], **(capacity or {})}
            try:
                Capacity.parse(merged, self.defaults)
            except ValueError as e:
                raise Invalid(str(e), param="capacity") from None
            await tx.run("UPDATE orgs SET capacity = %s, status = coalesce(%s, status) WHERE id = %s",
                         merged, status, org_id)
        return await self.get(org_id)

    async def egress(self, org_id: str) -> list[str]:
        return list((await self.get(org_id))["egress"])

    async def set_egress(self, org_id: str, values: object) -> list[str]:
        if not isinstance(values, list) or len(values) > EGRESS_MAX:
            raise Invalid(f"egress is a list of at most {EGRESS_MAX} entries", param="egress")
        try:
            clean = egress.entries(values)
        except ValueError as e:
            raise Invalid(str(e), param="egress") from None
        async with self.db.platform() as tx:
            await tx.run("UPDATE orgs SET egress = %s::jsonb WHERE id = %s", json.dumps(clean), org_id)
        return clean

