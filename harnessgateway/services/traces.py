"""Observability (SPEC §11): per turn, the harness exchange, each gateway exchange and the door's connections, stored
as objects under the turn (so their storage is metered), indexed, and handed to extensions as they are written."""
from __future__ import annotations

import json
import time
from typing import Any

from .. import ids
from ..db import Database
from ..domain.access import Principal
from ..errors import NotFound
from ..extensions import Extensions, TraceRecord
from .objects import Objects, org_key
from .turns import own

KINDS = ("harness", "gateway", "door")
LOG_MAX = 4 * 1024 * 1024


class Traces:
    def __init__(self, db: Database, objects: Objects, extensions: Extensions):
        self.db, self.objects, self.ext = db, objects, extensions

    async def write(self, org_id: str, response_id: str, kind: str, data: dict[str, Any]) -> None:
        assert kind in KINDS
        record = TraceRecord(ids.new("trace"), org_id, response_id, kind, time.time(), data)
        key = org_key(org_id, "turns", response_id, "traces", f"{int(record.at * 1000):013d}-{kind}-{record.id}.json")
        await self.objects.put(key, json.dumps({"id": record.id, "kind": kind, "at": record.at, "data": data}).encode(),
                               org_id=org_id, kind="trace")
        async with self.db.platform() as tx:
            await tx.run("INSERT INTO traces (id, org_id, response_id, kind, object_key) VALUES (%s, %s, %s, %s, %s)",
                         record.id, org_id, response_id, kind, key)
        await self.ext.observe(record)

    async def get(self, p: Principal, response_id: str) -> dict[str, Any]:
        p.require("traces", "read")
        clause, args = own(p)
        async with self.db.tenant(p.org_id) as tx:
            turn = await tx.one(f"SELECT t.id, t.session_id FROM turns t JOIN sessions s ON s.id = t.session_id "
                                f"WHERE t.id = %s{clause}", ids.check(response_id, "turn"), *args)
            if not turn:
                raise NotFound(f"no response {response_id}")
            rows = await tx.all("SELECT object_key FROM traces WHERE response_id = %s ORDER BY at, id", response_id)
        records = [json.loads(await self.objects.get(r["object_key"])) for r in rows]
        for r in records:
            for name in ("stdout", "stderr"):
                key = r["data"].pop(f"{name}_key", None)
                if key:
                    try:
                        r["data"][name] = (await self.objects.get(key))[-LOG_MAX:].decode("utf-8", "replace")
                    except KeyError:
                        r["data"][name] = None
        return {"object": "trace", "response_id": response_id, "session_id": turn["session_id"], "records": records}

    async def list_session(self, p: Principal, sid: str) -> dict[str, Any]:
        """Every record of the session's turns, in turn order, each naming its response; the log bodies are left out
        (GET /v1/traces/{response_id} has them)."""
        p.require("traces", "read")
        clause, args = own(p)
        async with self.db.tenant(p.org_id) as tx:
            if not await tx.one(f"SELECT 1 FROM sessions s WHERE s.id = %s AND s.status = 'active'{clause}",
                                ids.check(sid, "session"), *args):
                raise NotFound(f"no container {sid}")
            rows = await tx.all("SELECT tr.object_key, tr.response_id FROM traces tr JOIN turns t ON t.id = tr.response_id "
                                "WHERE t.session_id = %s ORDER BY t.seq, tr.at, tr.id", sid)
        records = []
        for r in rows:
            doc = json.loads(await self.objects.get(r["object_key"]))
            for name in ("stdout", "stderr"):
                doc["data"].pop(f"{name}_key", None)
            records.append({**doc, "response_id": r["response_id"]})
        return {"object": "list", "session_id": sid, "data": records}
