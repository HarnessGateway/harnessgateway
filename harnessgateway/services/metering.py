"""Metering: every infrastructure cost the platform accrues, recorded as append-only usage events per organization.

    compute.vcpu_seconds / compute.memory_gb_seconds   from nodes: allocated size x seconds a sandbox or install exists
    network.egress_bytes / network.ingress_bytes       from nodes (the egress door) and the API (client traffic)
    storage.gb_hours                                   accrued hourly from the object ledger

A bill is the usage priced at the deployment's unit costs times the markup."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ..db import Database
from ..domain.pricing import Meter, PriceTable
from ..errors import Invalid

GB = Decimal(1024 ** 3)
log = logging.getLogger("harnessgateway.metering")


class Metering:
    def __init__(self, db: Database, prices: PriceTable):
        self.db, self.prices = db, prices
        self._pending: dict[tuple[str, str], Decimal] = defaultdict(Decimal)

    async def record(self, rows: list[dict[str, Any]]) -> None:
        """Usage events: each {org_id, meter, quantity, session_id?, turn_id?}. Unknown meters are refused."""
        meters = {m.value for m in Meter}
        clean = []
        for r in rows:
            if r["meter"] not in meters or Decimal(str(r["quantity"])) < 0:
                raise Invalid(f"unknown meter or negative quantity: {r['meter']}")
            if Decimal(str(r["quantity"])) > 0:
                clean.append(r)
        if not clean:
            return
        async with self.db.platform() as tx:
            for r in clean:
                await tx.run("INSERT INTO usage_events (org_id, meter, quantity, session_id, turn_id) VALUES (%s, %s, %s, %s, %s)",
                             r["org_id"], r["meter"], Decimal(str(r["quantity"])), r.get("session_id"), r.get("turn_id"))

    def count_api_bytes(self, org_id: str, egress: int, ingress: int) -> None:
        """Client traffic through the API, accumulated in memory and flushed by `flush` (cheap on the hot path)."""
        if egress:
            self._pending[(org_id, Meter.EGRESS_BYTES.value)] += egress
        if ingress:
            self._pending[(org_id, Meter.INGRESS_BYTES.value)] += ingress

    async def flush(self) -> None:
        pending, self._pending = self._pending, defaultdict(Decimal)
        await self.record([{"org_id": o, "meter": m, "quantity": q} for (o, m), q in pending.items()])

    async def accrue_storage(self) -> int:
        """Storage GB-hours for every organization since its last accrual, from the live object ledger."""
        now = datetime.now(UTC)
        rows: list[dict[str, Any]] = []
        async with self.db.platform() as tx:
            sizes = {r["org_id"]: r["bytes"] for r in await tx.all(
                "SELECT org_id, sum(bytes) AS bytes FROM objects WHERE deleted_at IS NULL AND org_id IS NOT NULL GROUP BY org_id")}
            orgs = await tx.all("SELECT o.id, coalesce(a.accrued_until, o.created_at) AS since FROM orgs o "
                                "LEFT JOIN storage_accruals a ON a.org_id = o.id")
            for org in orgs:
                hours = Decimal((now - org["since"]).total_seconds()) / 3600
                if hours <= 0:
                    continue
                gb_hours = Decimal(sizes.get(org["id"], 0)) / GB * hours
                rows.append({"org_id": org["id"], "meter": Meter.STORAGE_GB_HOURS.value, "quantity": round(gb_hours, 9)})
                await tx.run("INSERT INTO storage_accruals (org_id, accrued_until) VALUES (%s, %s) "
                             "ON CONFLICT (org_id) DO UPDATE SET accrued_until = EXCLUDED.accrued_until", org["id"], now)
        await self.record(rows)
        return len(rows)

    async def usage(self, org_id: str, start: datetime, end: datetime) -> dict[str, Any]:
        if end <= start:
            raise Invalid("end is after start")
        async with self.db.tenant(org_id) as tx:
            rows = await tx.all("SELECT meter, sum(quantity) AS quantity FROM usage_events WHERE at >= %s AND at < %s "
                                "GROUP BY meter", start, end)
        bill = self.prices.bill({r["meter"]: Decimal(r["quantity"]) for r in rows})
        return {"object": "usage", "org_id": org_id, "start": int(start.timestamp()), "end": int(end.timestamp()), **bill}

    async def month_to_date_charge(self, org_id: str) -> Decimal:
        now = datetime.now(UTC)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return Decimal((await self.usage(org_id, start, now))["charge_usd"])

    async def storage_bytes(self, org_id: str) -> int:
        async with self.db.platform() as tx:
            return int(await tx.value("SELECT coalesce(sum(bytes), 0) FROM objects WHERE org_id = %s AND deleted_at IS NULL",
                                      org_id) or 0)

    async def flusher(self, every_s: float = 10) -> None:
        while True:
            await asyncio.sleep(every_s)
            try:
                await self.flush()
            except Exception:
                log.exception("flushing API traffic usage failed; retrying next interval")
