"""Layer 6: the deployer's routes: orgs and their ceilings, the fleet, usage. The deployer sees
counts and sizes, never an org's prompts, output, packages or keys."""
from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..sessions import RUNNING, Sessions
from ..store import Member, Store
from .auth import Principals


class OrgBody(BaseModel):
    name: str
    owner: str
    limits: dict[str, Any] | None = None


class OrgPatch(BaseModel):
    limits: dict[str, Any] | None = None
    status: str | None = Field(default=None, pattern="^(active|suspended)$")


def router(store: Store, sessions: Sessions, principals: Principals) -> APIRouter:
    r = APIRouter(prefix="/v1", dependencies=[Depends(principals.deployer)])

    def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {"turns": len(rows), "seconds": round(sum(u.get("seconds", 0) for u in rows), 1),
                "calls": sum(u.get("calls", 0) for u in rows), "bytes_in": sum(u.get("bytes_in", 0) for u in rows),
                "bytes_out": sum(u.get("bytes_out", 0) for u in rows)}

    def usage_of(org: str) -> dict[str, Any]:
        return {**totals(store.records.list("usage", org)),
                "running": store.records.count("response", org, "running", RUNNING),
                "queued": store.records.count("response", org, "status", "queued")}

    @r.get("/usage")
    def usage(days: int = 0) -> dict:
        """The platform's usage: all time with `days=0`, else the last `days` days; totals, a row per
        day, a row per org, a row per harness. Counts and sizes only."""
        now = time.time()
        since = now - days * 86400 if days > 0 else 0
        rows = [(t.name, u) for t in store.orgs() for u in store.records.list("usage", t.name) if u.get("at", 0) >= since]
        by_day: dict[str, list] = {}
        by_tenant: dict[str, list] = {}
        by_harness: dict[str, list] = {}
        for org, u in rows:
            by_day.setdefault(time.strftime("%Y-%m-%d", time.gmtime(u.get("at", 0))), []).append(u)
            by_tenant.setdefault(org, []).append(u)
            by_harness.setdefault(u.get("harness") or "(in progress)", []).append(u)
        span = days if days > 0 else max(1, int((now - min((u.get("at", now) for _, u in rows), default=now)) // 86400) + 1)
        day_rows = []
        for i in range(span - 1, -1, -1):
            day = time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400))
            day_rows.append({"day": day, **totals(by_day.get(day, []))})
        return {"days": days, "totals": totals([u for _, u in rows]),
                "daily": day_rows,
                "orgs": [{"org": t, **totals(us)} for t, us in sorted(by_tenant.items())],
                "harnesses": [{"harness": h, **totals(us)} for h, us in sorted(by_harness.items(), key=lambda kv: -len(kv[1]))],
                "statuses": {st: sum(1 for _, u in rows if u.get("status") == st) for st in ("completed", "failed", "cancelled", "incomplete")}}

    @r.post("/orgs")
    def create_org(body: OrgBody, _: Member = Depends(principals.deployer)) -> dict:
        t = store.create_org(body.name, body.limits)
        store.create_member(t.name, body.owner, "owner")
        key, secret = store.mint_key(t.name, body.owner, "first")
        return {"org": asdict(t), "owner": body.owner, "key": {"id": key.id, "secret": secret}}

    @r.get("/orgs")
    def list_tenants() -> dict:
        return {"object": "list", "data": [{**asdict(t), "usage": usage_of(t.name)} for t in store.orgs()]}

    @r.get("/orgs/{name}")
    def get_tenant(name: str) -> dict:
        t = store.org(name)
        return {**asdict(t), "members": [asdict(m) for m in store.members(name)], "teams": [x.name for x in store.teams(name)],
                "usage": usage_of(name), "harnesses": [h.name for h in store.harnesses(name)], "providers": [p.name for p in store.providers(name)]}

    @r.patch("/orgs/{name}")
    def patch_tenant(name: str, body: OrgPatch) -> dict:
        return asdict(store.update_org(name, limits=body.limits, status=body.status))

    @r.delete("/orgs/{name}")
    def delete_org(name: str) -> dict:
        store.delete_org(name)
        return {"deleted": name}

    @r.get("/workers")
    def list_workers() -> dict:
        now = time.time()
        return {"object": "list", "data": [{**asdict(w), "alive": now - w.last_seen < 45} for w in store.workers()]}

    @r.delete("/workers/{worker_id}")
    def forget_worker(worker_id: str) -> dict:
        store.forget_worker(worker_id)
        return {"deleted": worker_id}

    @r.get("/fleet")
    def fleet() -> dict:
        orgs = store.orgs()
        workers = store.workers()
        now = time.time()
        return {"orgs": len(orgs), "workers": sum(1 for w in workers if now - w.last_seen < 45),
                "slots": sum(w.slots for w in workers if now - w.last_seen < 45),
                "running": sum(w.running for w in workers if now - w.last_seen < 45),
                "queued": sum(store.records.count("response", t.name, "status", "queued") for t in orgs),
                "lost_recently": sessions.sweep_lost()}

    return r
