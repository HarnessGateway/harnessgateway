"""The operator's routes, behind HG_OPERATOR_KEY: invites for the pilot, organizations and their ceilings, node join
tokens, the fleet and the price table. The operator sees names, limits and usage; an organization's content is its own."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from ..errors import Invalid
from .http import operator, platform, read_json
from .org import window

router = APIRouter(prefix="/admin/v1", dependencies=[Depends(operator)])
ACTOR = "operator"


def _int(body: dict[str, Any], key: str, default: int | None) -> int | None:
    value = body.get(key, default)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise Invalid(f"{key} is an integer", param=key)
    return value


@router.post("/invites")
async def create_invite(request: Request) -> Any:
    body = await read_json(request)
    return await platform(request).tenancy.create_invite(str(body.get("label", "")), _int(body, "uses", 1) or 1,
                                                        _int(body, "ttl_days", 30), body.get("limits"), ACTOR)


@router.post("/organizations")
async def create_org(request: Request) -> Any:
    body = await read_json(request)
    org, secret = await platform(request).create_org(str(body.get("name", "")), str(body.get("owner_email", "")),
                                                     body.get("limits"), ACTOR)
    return {"organization": org, "api_key": secret}


@router.get("/organizations")
async def list_orgs(request: Request) -> Any:
    return {"object": "list", "data": await platform(request).tenancy.list_orgs()}


@router.get("/organizations/{org_id}")
async def get_org(request: Request, org_id: str) -> Any:
    return await platform(request).tenancy.org(org_id)


@router.patch("/organizations/{org_id}")
async def update_org(request: Request, org_id: str) -> Any:
    body = await read_json(request)
    return await platform(request).tenancy.update_org(org_id, limits=body.get("limits"), status=body.get("status"),
                                                      actor=ACTOR)


@router.get("/organizations/{org_id}/usage")
async def org_usage(request: Request, org_id: str, start: int | None = None, end: int | None = None) -> Any:
    await platform(request).tenancy.org(org_id)
    s, e = window(start, end)
    return await platform(request).metering.usage(org_id, s, e)


@router.post("/node-tokens")
async def create_node_token(request: Request) -> Any:
    body = await read_json(request)
    org_id = body.get("org_id")
    if org_id is not None:
        await platform(request).tenancy.org(org_id)
    return await platform(request).scheduler.create_join_token(org_id, str(body.get("label", "")),
                                                               _int(body, "uses", 1) or 1, _int(body, "ttl_days", 7), ACTOR)


@router.get("/nodes")
async def list_nodes(request: Request) -> Any:
    async with platform(request).db.platform() as tx:
        rows = await tx.all("SELECT n.id, n.name, n.org_id, n.slots, n.version, n.status, n.last_seen_at, "
                            "(SELECT count(*) FROM turns t WHERE t.node_id = n.id AND t.status = 'in_progress') AS running, "
                            "(SELECT count(*) FROM sessions s WHERE s.node_id = n.id) AS sandboxes FROM nodes n "
                            "WHERE n.status <> 'removed' ORDER BY n.created_at")
    return {"object": "list", "data": [{**r, "last_seen_at": int(r["last_seen_at"].timestamp()) if r["last_seen_at"] else None}
                                       for r in rows]}


@router.post("/nodes/{node_id}/drain")
async def drain_node(request: Request, node_id: str) -> Any:
    """Stop new claims; running turns finish and warm sessions restore elsewhere from their snapshots."""
    async with platform(request).db.platform() as tx:
        n = await tx.run("UPDATE nodes SET status = 'draining' WHERE id = %s AND status = 'active'", node_id)
    return {"id": node_id, "object": "node", "status": "draining" if n else "unchanged"}


@router.delete("/nodes/{node_id}")
async def remove_node(request: Request, node_id: str) -> Any:
    async with platform(request).db.platform() as tx:
        await tx.run("UPDATE nodes SET status = 'removed' WHERE id = %s", node_id)
    return {"id": node_id, "object": "node", "deleted": True}


@router.get("/prices")
async def prices(request: Request) -> Any:
    table = platform(request).prices
    return {"markup": str(table.markup), "unit_costs_usd": {k: str(v) for k, v in table.unit_costs.items()}}
