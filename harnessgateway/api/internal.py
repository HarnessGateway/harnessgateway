"""The nodes' routes (SPEC §14.3). A node joins once with a join token, then authenticates with its own credential.
Object transfers use the signed links its job carried: a link is the only authorization they need."""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse

from ..errors import Forbidden, Invalid
from ..services.principal import NodeIdentity
from .http import bearer, node, operator, platform, read_json

router = APIRouter(prefix="/internal")


def _strings(body: dict[str, Any], key: str) -> list[str]:
    value = body.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise Invalid(f"{key} is a list of strings", param=key)
    return value


def _int(body: dict[str, Any], key: str, default: int) -> int:
    value = body.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise Invalid(f"{key} is an integer", param=key)
    return value


@router.post("/nodes/join")
async def join(request: Request) -> Any:
    body = await read_json(request)
    return await platform(request).scheduler.join(bearer(request), str(body.get("name", "node")),
                                                  _int(body, "slots", 1), str(body.get("version", "")))


@router.post("/nodes/heartbeat")
async def heartbeat(request: Request, n: NodeIdentity = Depends(node)) -> Any:
    body = await read_json(request)
    return await platform(request).scheduler.heartbeat(n, _strings(body, "running"), _strings(body, "boxes"),
                                                       _strings(body, "saving"), _int(body, "slots", 0),
                                                       str(body.get("version", "")))


@router.post("/nodes/claim")
async def claim(request: Request, n: NodeIdentity = Depends(node)) -> Any:
    job = await platform(request).scheduler.claim(n)
    return Response(status_code=204) if job is None else job


@router.post("/turns/{rid}/events")
async def events(request: Request, rid: str, n: NodeIdentity = Depends(node)) -> Any:
    body = await read_json(request)
    return await platform(request).delivery.events(n.node_id, rid, _int(body, "batch", -1), _strings(body, "lines"))


@router.post("/turns/{rid}/finish")
async def finish(request: Request, rid: str, n: NodeIdentity = Depends(node)) -> Any:
    return await platform(request).delivery.finish(n.node_id, rid, await read_json(request))


@router.post("/turns/{rid}/saved")
async def saved(request: Request, rid: str, n: NodeIdentity = Depends(node)) -> Any:
    await platform(request).delivery.saved(n.node_id, rid, await read_json(request))
    return {"ok": True}


@router.get("/turns/{rid}/snapshot")
async def snapshot(request: Request, rid: str, n: NodeIdentity = Depends(node)) -> Any:
    return await platform(request).scheduler.snapshot(n, rid)


@router.post("/usage")
async def usage(request: Request, n: NodeIdentity = Depends(node)) -> Any:
    records = (await read_json(request)).get("records", [])
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise Invalid("records is a list of usage records", param="records")
    plat = platform(request)
    await plat.scheduler.check_orgs(n, {str(r.get("org_id")) for r in records})
    return {"recorded": await plat.usage.record(records)}


@router.get("/blobs/{token}")
async def blob_get(request: Request, token: str) -> Any:
    plat = platform(request)
    key = plat.signer.verify(token, "GET")
    if key is None:
        raise Forbidden("the link is invalid or expired")
    return StreamingResponse(plat.objects.stream(key), media_type="application/octet-stream")


@router.put("/blobs/{token}")
async def blob_put(request: Request, token: str) -> Any:
    """A file-store upload through its signed link (an S3 store's links go to the store itself)."""
    plat = platform(request)
    key = plat.signer.verify(token, "PUT")
    if key is None:
        raise Forbidden("the link is invalid or expired")
    with tempfile.NamedTemporaryFile(prefix="hg-blob-") as tmp:
        async for chunk in request.stream():
            tmp.write(chunk)
        tmp.flush()
        await plat.store.put_file(key, Path(tmp.name))
    return {"key": key}


@router.post("/node-tokens", dependencies=[Depends(operator)])
async def node_token(request: Request) -> Any:
    body = await read_json(request)
    ttl = body.get("ttl_days")
    token = await platform(request).scheduler.create_token(str(body.get("pool", "")), str(body.get("label", "")),
                                                           _int(body, "uses", 1), ttl if isinstance(ttl, int) else None)
    return {"object": "node_token", "token": token}


@router.post("/nodes/{node_id}/drain", dependencies=[Depends(operator)])
async def drain(request: Request, node_id: str) -> Any:
    await platform(request).scheduler.drain(node_id)
    return {"id": node_id, "object": "node", "status": "draining"}


# ── the operator's view (SPEC §14.3): health, nodes, organizations, usage across them, configuration ──────────────
@router.get("/health", dependencies=[Depends(operator)])
async def health(request: Request) -> Any:
    return await platform(request).operations.health()


@router.get("/nodes", dependencies=[Depends(operator)])
async def nodes(request: Request) -> Any:
    return await platform(request).operations.nodes()


@router.get("/orgs", dependencies=[Depends(operator)])
async def orgs(request: Request) -> Any:
    return await platform(request).operations.orgs()


@router.get("/orgs/{org_id}", dependencies=[Depends(operator)])
async def org(request: Request, org_id: str) -> Any:
    return await platform(request).operations.org(org_id)


@router.patch("/orgs/{org_id}", dependencies=[Depends(operator)])
async def configure_org(request: Request, org_id: str) -> Any:
    """{"capacity": {...}, "status": "active|suspended", "egress": [...]}: each given part replaces the stored one."""
    plat, body = platform(request), await read_json(request)
    unknown = sorted(set(body) - {"capacity", "status", "egress"})
    if unknown:
        raise Invalid(f"an organization is configured with capacity, status and egress, not {', '.join(unknown)}")
    if "capacity" in body or "status" in body:
        capacity = body.get("capacity")
        if capacity is not None and not isinstance(capacity, dict):
            raise Invalid("capacity is an object of capacity fields", param="capacity")
        await plat.orgs.update(org_id, capacity=capacity, status=body.get("status"))
    if "egress" in body:
        await plat.orgs.set_egress(org_id, body["egress"])
    return await plat.operations.org(org_id)


@router.get("/usage", dependencies=[Depends(operator)])
async def usage_all(request: Request, start: int | None = None, end: int | None = None, group_by: str | None = None) -> Any:
    now = datetime.now(UTC)
    s = datetime.fromtimestamp(start, UTC) if start else now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    e = datetime.fromtimestamp(end, UTC) if end else now
    return await platform(request).operations.usage(s, e, group_by)


@router.get("/config", dependencies=[Depends(operator)])
async def config(request: Request) -> Any:
    return platform(request).operations.config()


@router.get("/versions", dependencies=[Depends(operator)])
async def versions(request: Request) -> Any:
    return await platform(request).operations.versions()


@router.post("/sessions/{sid}/evict", dependencies=[Depends(operator)])
async def evict(request: Request, sid: str) -> Any:
    return await platform(request).operations.evict(sid)
