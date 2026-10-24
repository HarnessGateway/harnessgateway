"""The deployer's routes: organizations and their ceilings and plans, the fleet, usage. The deployer
sees counts and sizes, never an organization's prompts, output, packages or keys."""
from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..domain.members import Member
from ..domain.sessions import RUNNING
from ..services.metering import totals


class OrgBody(BaseModel):
    name: str
    owner: str
    limits: dict[str, Any] | None = None
    plan: str = "free"
    title: str = ""


class OrgPatch(BaseModel):
    limits: dict[str, Any] | None = None
    status: str | None = Field(default=None, pattern="^(active|suspended)$")
    plan: str | None = Field(default=None, pattern="^(free|team|enterprise)$")
    trace_sharing: bool | None = None


def router(service) -> APIRouter:
    principals, orgs, members, teams = service.principals, service.orgs, service.members, service.teams
    harnesses, providers, nodes, keys, records = service.harnesses, service.providers, service.nodes, service.keys, service.records
    r = APIRouter(prefix="/v1", dependencies=[Depends(principals.deployer)])

    def usage_of(org: str) -> dict[str, Any]:
        return {**totals(records.list("usage", org)),
                "running": records.count("response", org, "running", RUNNING),
                "queued": records.count("response", org, "status", "queued")}

    @r.get("/usage")
    def usage(days: int = 0) -> dict:
        """The platform's usage: all time with `days=0`, else the last `days` days; totals, a row per
        day, a row per organization, a row per harness. Counts and sizes only."""
        now = time.time()
        since = now - days * 86400 if days > 0 else 0
        rows = [(o.name, u) for o in orgs.list() for u in records.list("usage", o.name) if u.get("at", 0) >= since]
        by_day: dict[str, list] = {}
        by_org: dict[str, list] = {}
        by_harness: dict[str, list] = {}
        for org, u in rows:
            by_day.setdefault(time.strftime("%Y-%m-%d", time.gmtime(u.get("at", 0))), []).append(u)
            by_org.setdefault(org, []).append(u)
            by_harness.setdefault(u.get("harness") or "(in progress)", []).append(u)
        span = days if days > 0 else max(1, int((now - min((u.get("at", now) for _, u in rows), default=now)) // 86400) + 1)
        day_rows = [{"day": time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400)),
                     **totals(by_day.get(time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400)), []))} for i in range(span - 1, -1, -1)]
        return {"days": days, "totals": totals([u for _, u in rows]), "daily": day_rows,
                "orgs": [{"org": o, **totals(us)} for o, us in sorted(by_org.items())],
                "harnesses": [{"harness": h, **totals(us)} for h, us in sorted(by_harness.items(), key=lambda kv: -len(kv[1]))],
                "statuses": {st: sum(1 for _, u in rows if u.get("status") == st) for st in ("completed", "failed", "cancelled", "incomplete")}}

    @r.post("/orgs")
    def create_org(body: OrgBody, _: Member = Depends(principals.deployer)) -> dict:
        o = orgs.create(body.name, body.limits, title=body.title, plan=body.plan, trace_sharing=service.settings.trace_sharing and body.plan == "free")
        members.create(o.name, body.owner, "owner")
        key, secret = keys.mint(o.name, body.owner, "first")
        return {"org": asdict(o), "owner": body.owner, "key": {"id": key.id, "secret": secret}}

    @r.get("/orgs")
    def list_orgs() -> dict:
        return {"object": "list", "data": [{**asdict(o), "usage": usage_of(o.name)} for o in orgs.list()]}

    @r.get("/orgs/{name}")
    def get_org(name: str) -> dict:
        o = orgs.get(name)
        return {**asdict(o), "members": [asdict(m) for m in members.list(name)], "teams": [x.name for x in teams.list(name)],
                "usage": usage_of(name), "harnesses": [h.name for h in harnesses.list(name)], "providers": [p.name for p in providers.list(name)]}

    @r.patch("/orgs/{name}")
    def patch_org(name: str, body: OrgPatch) -> dict:
        return asdict(orgs.update(name, limits=body.limits, status=body.status, plan=body.plan, trace_sharing=body.trace_sharing))

    @r.delete("/orgs/{name}")
    def delete_org(name: str) -> dict:
        harnesses.delete_all(name)
        orgs.delete(name)
        return {"deleted": name}

    @r.get("/workers")
    def list_nodes() -> dict:
        return {"object": "list", "data": [{**asdict(n), "alive": n.alive} for n in nodes.list()]}

    @r.delete("/workers/{node_id}")
    def forget_node(node_id: str) -> dict:
        nodes.forget(node_id)
        return {"deleted": node_id}

    @r.get("/fleet")
    def fleet() -> dict:
        all_orgs, all_nodes = orgs.list(), [n for n in nodes.list() if n.alive]
        return {"orgs": len(all_orgs), "workers": len(all_nodes), "slots": sum(n.slots for n in all_nodes),
                "running": sum(n.running for n in all_nodes),
                "queued": sum(records.count("response", o.name, "status", "queued") for o in all_orgs),
                "lost_recently": service.scheduler.sweep_lost()}

    return r
