"""The nodes' routes. A node joins once with a join token, then authenticates with its own credential to pull jobs,
heartbeat, stream a turn's output, report its end and report usage. Blob transfers use signed capability URLs the job
carried; the token is the only authorization they need, so a node never holds storage credentials."""
from __future__ import annotations

import tempfile
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse

from ..errors import Forbidden, Invalid
from ..services.principal import NodeIdentity
from .http import bearer, node, platform, read_json

router = APIRouter(prefix="/internal/v1")
SPOOL = 8 * 1024 * 1024


def _list_of_str(body: dict[str, Any], key: str) -> list[str]:
    value = body.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise Invalid(f"{key} is a list of strings", param=key)
    return value


@router.post("/nodes/join")
async def join(request: Request) -> Any:
    body = await read_json(request)
    return await platform(request).scheduler.join(bearer(request), str(body.get("name", "node")),
                                                  int(body.get("slots", 1)), str(body.get("version", "")))


@router.post("/nodes/heartbeat")
async def heartbeat(request: Request, n: NodeIdentity = Depends(node)) -> Any:
    body = await read_json(request)
    return await platform(request).scheduler.heartbeat(n, _list_of_str(body, "running"), _list_of_str(body, "sandboxes"),
                                                       int(body.get("slots", 0)), str(body.get("version", "")))


@router.post("/jobs/claim")
async def claim(request: Request, n: NodeIdentity = Depends(node)) -> Any:
    job = await platform(request).scheduler.claim(n)
    return job if job is not None else Response(status_code=204)


@router.post("/turns/{rid}/events")
async def events(request: Request, rid: str, n: NodeIdentity = Depends(node)) -> Any:
    body = await read_json(request)
    return await platform(request).delivery.append(n.node_id, rid, int(body.get("batch", 0)), _list_of_str(body, "lines"))


@router.post("/turns/{rid}/finish")
async def finish(request: Request, rid: str, n: NodeIdentity = Depends(node)) -> Any:
    await platform(request).delivery.finish(n.node_id, rid, await read_json(request))
    return {"ok": True}


@router.post("/usage")
async def usage(request: Request, n: NodeIdentity = Depends(node)) -> Any:
    rows = (await read_json(request)).get("rows", [])
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise Invalid("rows is a list of usage events")
    await platform(request).scheduler.check_usage_scope(n, rows)
    await platform(request).metering.record(rows)
    return {"ok": True}


@router.get("/blobs/{token}")
async def blob_get(request: Request, token: str) -> Any:
    key = platform(request).signer.verify(token, "GET")
    if key is None:
        raise Forbidden("the blob URL is invalid or expired")
    return StreamingResponse(platform(request).blobs.stream(key), media_type="application/octet-stream")


@router.put("/blobs/{token}")
async def blob_put(request: Request, token: str) -> Any:
    plat = platform(request)
    key = plat.signer.verify(token, "PUT")
    if key is None or not key.startswith("orgs/org_"):
        raise Forbidden("the blob URL is invalid or expired")
    org_id = key.split("/", 2)[1]
    with tempfile.SpooledTemporaryFile(max_size=SPOOL) as spool:
        async for chunk in request.stream():
            spool.write(chunk)
        spool.seek(0)
        kind = "snapshot" if key.endswith("snapshot.tgz") else "manifest" if key.endswith("manifest.json") else "log"
        size = await plat.blobs.put(key, spool, org_id=org_id, kind=kind)
    return {"key": key, "bytes": size}
