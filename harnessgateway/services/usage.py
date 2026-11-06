"""Metering (SPEC §12): usage records from where they are measured (the api for time, the door for bytes, the accrue
duty for storage) into an append-only ledger, idempotent by record id, then handed to extensions. The core
measures; pricing is an extension's business."""
from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .. import ids
from ..domain.access import Principal
from ..domain.meters import Kind, Meter
from ..errors import Invalid
from ..extensions import Extensions, UsageRecord
from ..store.db import Database

GIB = 1024 ** 3
GROUPS = {"harness": "harness", "session": "session_id", "day": "to_char(end_at, 'YYYY-MM-DD')",
          "created_by": "(SELECT s.created_by FROM sessions s WHERE s.id = usage.session_id)",
          "user": "(SELECT s.end_user FROM sessions s WHERE s.id = usage.session_id)"}
PLATFORM_GROUPS = {"org": "org_id", "day": "to_char(end_at, 'YYYY-MM-DD')"}


def _record(r: dict[str, Any]) -> UsageRecord:
    try:
        meter, kind = Meter(r["meter"]), Kind(r["kind"])
        quantity, start, end = float(r["quantity"]), float(r["start"]), float(r["end"])
    except (KeyError, ValueError, TypeError):
        raise Invalid(f"not a usage record: {r!r}"[:300]) from None
    if not math.isfinite(quantity) or quantity < 0 or end < start:
        raise Invalid("a usage quantity is finite and not negative, over start <= end")
    rid = str(r.get("id") or "")
    if not rid.startswith("use_"):
        raise Invalid("a usage record has an id use_..., generated where it was measured")
    return UsageRecord(rid, str(r["org_id"]), meter.value, quantity, kind.value, start, end, r.get("harness"),
                       r.get("session_id"), r.get("response_id"))


def _summary(rows: list[dict[str, Any]], start: datetime, end: datetime, group_by: str | None) -> dict[str, Any]:
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        out.setdefault(str(r["grp"]) if group_by else "total", {})[r["meter"]] = float(r["quantity"])
    return {"object": "usage", "start": int(start.timestamp()), "end": int(end.timestamp()), "group_by": group_by,
            "meters": [m.value for m in Meter], "data": out}


def _column(groups: dict[str, str], group_by: str | None) -> str:
    if group_by is not None and group_by not in groups:
        raise Invalid(f"group_by is one of {', '.join(groups)}", param="group_by")
    return groups.get(group_by or "", "NULL")


class Usage:
    def __init__(self, db: Database, extensions: Extensions):
        self.db, self.ext = db, extensions

    async def record(self, rows: Iterable[dict[str, Any]]) -> int:
        records = [r for r in map(_record, rows) if r.quantity > 0]
        if not records:
            return 0
        async with self.db.platform() as tx:
            await tx.many("INSERT INTO usage (id, org_id, meter, quantity, kind, harness, session_id, response_id, start_at, "
                          "end_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s)) "
                          "ON CONFLICT (id) DO NOTHING",
                          [(r.id, r.org_id, r.meter, r.quantity, r.kind, r.harness, r.session_id, r.response_id, r.start,
                            r.end) for r in records])
        await self.ext.meter(records)
        return len(records)

    async def accrue_storage(self) -> int:
        """GiB-hours for every organization since its last accrual (SPEC §12.2): referenced blobs and live objects."""
        now = datetime.now(UTC)
        rows: list[dict[str, Any]] = []
        async with self.db.platform() as tx:
            held = {r["org_id"]: int(r["bytes"]) for r in await tx.all(
                "SELECT org_id, sum(bytes) AS bytes FROM (SELECT org_id, bytes FROM blobs WHERE refs > 0 UNION ALL "
                "SELECT org_id, bytes FROM objects WHERE deleted_at IS NULL AND org_id IS NOT NULL) h GROUP BY org_id")}
            for org in await tx.all("SELECT o.id, coalesce(a.accrued_until, o.created_at) AS since FROM orgs o "
                                    "LEFT JOIN storage_accruals a ON a.org_id = o.id"):
                hours = (now - org["since"]).total_seconds() / 3600
                if hours <= 0:
                    continue
                rows.append({"id": ids.new("usage"), "org_id": org["id"], "meter": Meter.STORAGE_GIB_HOURS.value,
                             "kind": Kind.STORAGE.value, "quantity": held.get(org["id"], 0) / GIB * hours,
                             "start": org["since"].timestamp(), "end": now.timestamp()})
                await tx.run("INSERT INTO storage_accruals (org_id, accrued_until) VALUES (%s, %s) "
                             "ON CONFLICT (org_id) DO UPDATE SET accrued_until = EXCLUDED.accrued_until", org["id"], now)
        return await self.record(rows)

    async def summary(self, p: Principal, start: datetime, end: datetime, group_by: str | None) -> dict[str, Any]:
        """The organization's usage; for a run-only key, that of its own sessions (storage accrues to the organization,
        so such a key never sees it). `created_by` groups by the key or person behind each session."""
        p.require("usage", "read")
        if end <= start:
            raise Invalid("end is after start")
        column = _column(GROUPS, group_by)
        scope, args = "", []
        if p.owns_only:
            scope = " AND EXISTS (SELECT 1 FROM sessions s WHERE s.id = usage.session_id AND s.created_by = %s)"
            args = [p.actor]
        async with self.db.tenant(p.org_id) as tx:
            rows = await tx.all(f"SELECT {column} AS grp, meter, sum(quantity) AS quantity FROM usage WHERE "
                                f"end_at >= %s AND end_at < %s{scope} GROUP BY 1, 2 ORDER BY 1, 2", start, end, *args)
        return _summary(rows, start, end, group_by)

    async def platform_summary(self, start: datetime, end: datetime, group_by: str | None) -> dict[str, Any]:
        """Every organization's usage together, by organization or by day."""
        if end <= start:
            raise Invalid("end is after start")
        column = _column(PLATFORM_GROUPS, group_by)
        async with self.db.platform() as tx:
            rows = await tx.all(f"SELECT {column} AS grp, meter, sum(quantity) AS quantity FROM usage WHERE end_at >= %s "
                                f"AND end_at < %s GROUP BY 1, 2 ORDER BY 1, 2", start, end)
        return _summary(rows, start, end, group_by)
