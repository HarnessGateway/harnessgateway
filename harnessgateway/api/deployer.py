"""The deployer's routes: organizations and their ceilings, and the fleet. The deployer sees names
and counts; an organization's content is its own."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..domain.members import Member
from ..domain.sessions import RUNNING


class OrgBody(BaseModel):
    name: str
    owner: str
    limits: dict[str, Any] | None = None
    title: str = ""


class OrgPatch(BaseModel):
    limits: dict[str, Any] | None = None
    status: str | None = Field(default=None, pattern="^(active|suspended)$")
    title: str | None = None


def router(service) -> APIRouter:
    principals, orgs, members = service.principals, service.orgs, service.members
    harnesses, providers, nodes, keys, records = service.harnesses, service.providers, service.nodes, service.keys, service.records
    r = APIRouter(prefix="/v1", dependencies=[Depends(principals.deployer)])

    def counts(org: str) -> dict[str, Any]:
        return {"sessions": len(records.list("session", org)), "responses": len(records.list("response", org)),
                "running": records.count("response", org, "running", RUNNING), "queued": records.count("response", org, "status", "queued")}

    @r.post("/orgs")
    def create_org(body: OrgBody, _: Member = Depends(principals.deployer)) -> dict:
        o = orgs.create(body.name, body.limits, title=body.title)
        members.create(o.name, body.owner, "owner")
        key, secret = keys.mint(o.name, body.owner, "first")
        return {"org": asdict(o), "owner": body.owner, "key": {"id": key.id, "secret": secret}}

    @r.get("/orgs")
    def list_orgs() -> dict:
        return {"object": "list", "data": [{**asdict(o), "counts": counts(o.name)} for o in orgs.list()]}

    @r.get("/orgs/{name}")
    def get_org(name: str) -> dict:
        o = orgs.get(name)
        return {**asdict(o), "members": [asdict(m) for m in members.list(name)], "counts": counts(name),
                "harnesses": [h.name for h in harnesses.list(name)], "providers": [p.name for p in providers.list(name)]}

    @r.patch("/orgs/{name}")
    def patch_org(name: str, body: OrgPatch) -> dict:
        return asdict(orgs.update(name, limits=body.limits, status=body.status, title=body.title))

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
