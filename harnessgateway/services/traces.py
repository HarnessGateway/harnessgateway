"""Observability, a paid capability (the organization's `observability` limit). When it is on, a turn keeps two
exchanges: the platform and the harness (the request handed to the package, every event and the full stdout and
stderr) and the harness and its provider (each call through the gateway, request and response). When it is off,
none of that is stored beyond what serving the response needs. Captures are objects like any other, so their storage
is metered and billed."""
from __future__ import annotations

import json
from typing import Any

from ..blobs import Blobs, org_key
from ..db import Database
from ..errors import Forbidden
from .principal import Principal
from .tenancy import Tenancy
from .turns import Turns


class Traces:
    def __init__(self, db: Database, blobs: Blobs, tenancy: Tenancy, turns: Turns):
        self.db, self.blobs, self.tenancy, self.turns = db, blobs, tenancy, turns

    async def get(self, p: Principal, rid: str) -> dict[str, Any]:
        if not (await self.tenancy.limits(p.org_id)).observability:
            raise Forbidden("observability is not enabled for this organization", code="observability_disabled")
        turn = await self.turns.row(p, rid)
        async with self.db.tenant(p.org_id) as tx:
            events = [r["data"] for r in await tx.all("SELECT data FROM turn_events WHERE turn_id = %s ORDER BY seq", rid)]
        prefix = org_key(p.org_id, "turns", rid, "")
        async with self.db.platform() as tx:
            keys = [r["key"] for r in await tx.all("SELECT key FROM objects WHERE key LIKE %s AND deleted_at IS NULL "
                                                   "ORDER BY key", prefix.replace("_", r"\_") + "%")]
        provider = [json.loads(await self.blobs.get(k)) for k in keys if "/provider/" in k]
        logs = {name: (await self.blobs.get(prefix + name)).decode("utf-8", "replace")
                for name in ("stdout.log", "stderr.log") if prefix + name in keys}
        return {"object": "trace", "response_id": rid, "session_id": turn["session_id"],
                "harness": {"request": json.loads(await self.blobs.get(turn["request_key"])), "events": events,
                            "stdout": logs.get("stdout.log", turn["log_tail"]),
                            "stderr": logs.get("stderr.log", turn["stderr_tail"]), "exit_code": turn["exit_code"],
                            "started_at": int(turn["started_at"].timestamp()) if turn["started_at"] else None,
                            "ended_at": int(turn["ended_at"].timestamp()) if turn["ended_at"] else None},
                "provider": provider}
