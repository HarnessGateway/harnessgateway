"""The deployer's routes: organizations and their ceilings and plans, the fleet, usage, and the
dataset. The deployer sees counts and sizes of every organization; the content of only those that
share their traces, through the dataset export, pseudonymized."""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..domain.members import Member
from ..domain.sessions import RUNNING
from ..services.metering import METERS, SHOWN, daily, grouped, totals


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
    billing_id: str | None = None


def router(service) -> APIRouter:
    principals, orgs, members, teams = service.principals, service.orgs, service.members, service.teams
    harnesses, providers, nodes, keys, records = service.harnesses, service.providers, service.nodes, service.keys, service.records
    r = APIRouter(prefix="/v1", dependencies=[Depends(principals.deployer)])

    def usage_of(org: str) -> dict[str, Any]:
        return {**totals(records.list("usage", org)),
                "running": records.count("response", org, "running", RUNNING),
                "queued": records.count("response", org, "status", "queued")}

    @r.get("/platform/usage")
    def usage(days: int = 0) -> dict:
        """The platform's usage: all time with `days=0`, else the last `days` days; totals, a row per
        day, a row per organization, a row per harness. Counts and sizes only."""
        now = time.time()
        since = now - days * 86400 if days > 0 else 0
        rows = [{**u, "org": o.name} for o in orgs.list() for u in records.list("usage", o.name) if u.get("at", 0) >= since]
        storage = [s for o in orgs.list() for s in records.list("meter", o.name, "storage:")[-1:]]
        return {"days": days, "totals": totals(rows), "daily": daily(rows, days, now), "orgs": grouped(rows, "org"),
                "harnesses": grouped(rows, "harness"),
                "statuses": {st: sum(1 for u in rows if u.get("status") == st) for st in ("completed", "failed", "cancelled", "incomplete")},
                "storage": {"bytes": sum(s["bytes"] for s in storage), "orgs": len(storage)}, "meters": METERS, "shown": SHOWN}

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
        return asdict(orgs.update(name, limits=body.limits, status=body.status, plan=body.plan, trace_sharing=body.trace_sharing,
                                  billing_id=body.billing_id))

    @r.get("/orgs/{name}/usage")
    def org_usage(name: str, days: int = 30) -> dict:
        orgs.get(name)
        return service.metering.summary(name, days)

    # the dataset: the traces of organizations that share them
    @r.get("/dataset/summary")
    def dataset_summary(since: float = 0, until: float = 0) -> dict:
        return service.dataset.summary(since, until or None)

    @r.get("/dataset")
    def dataset(since: float = 0, until: float = 0) -> StreamingResponse:
        """One JSON line per session of the organizations that share their traces, created in
        [since, until): request, response, steers, provider captures; identities pseudonymized."""
        def lines():
            for row in service.dataset.rows(since, until or None):
                yield json.dumps(row, separators=(",", ":")) + "\n"
        return StreamingResponse(lines(), media_type="application/x-ndjson",
                                 headers={"Content-Disposition": 'attachment; filename="dataset.jsonl"'})

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
