"""The nodes' routes. A node joins with the join token, then pulls with its own credential: heartbeat
(and receive commands), claim, report a start, post chunks, post the result. Blob URLs for the
directory backend are served here too; their token is their authorization."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from ..backends.blobs.dir import DirBlobs
from ..domain.nodes import Node
from ..errors import Invalid


class JoinBody(BaseModel):
    name: str
    slots: int = 4
    version: str = ""


class HeartbeatBody(BaseModel):
    running: list[str] = []


class ChunksBody(BaseModel):
    chunks: list[dict[str, Any]]


def router(service, version: str) -> APIRouter:
    r = APIRouter(prefix="/internal")
    principals, scheduler, nodes = service.principals, service.scheduler, service.nodes

    @r.post("/join")
    def join(body: JoinBody, _: None = Depends(principals.joiner)) -> dict:
        n, secret = nodes.register(body.name, body.slots, body.version)
        return {"id": n.id, "secret": secret, "version": version}

    @r.post("/heartbeat")
    def heartbeat(body: HeartbeatBody, n: Node = Depends(principals.node)) -> dict:
        return {"commands": scheduler.commands(n, body.running)}

    @r.post("/claim")
    def claim(n: Node = Depends(principals.node)) -> Response:
        job = scheduler.claim(n)
        if job is None:
            return Response(status_code=204)
        return Response(json.dumps(job), media_type="application/json")

    @r.post("/turns/{rid}/started")
    def started(rid: str, n: Node = Depends(principals.node)) -> dict:
        scheduler.started(n, rid)
        return {"ok": True}

    @r.post("/turns/{rid}/chunks")
    def chunks(rid: str, body: ChunksBody, n: Node = Depends(principals.node)) -> dict:
        scheduler.chunks_in(n, rid, body.chunks)
        return {"ok": True}

    @r.post("/turns/{rid}/result")
    def result(rid: str, body: dict, n: Node = Depends(principals.node)) -> dict:
        return {"status": scheduler.result(n, rid, body).status}

    if isinstance(service.blobs, DirBlobs):
        blobs = service.blobs

        @r.get("/blobs/{token}")
        def get_blob(token: str) -> Response:
            key = blobs.verify(token, "GET")
            return Response(blobs.get(key), media_type="application/octet-stream")

        @r.put("/blobs/{token}")
        async def put_blob(token: str, request: Request) -> dict:
            key = blobs.verify(token, "PUT")
            data = await request.body()
            if not data:
                raise Invalid("an empty blob")
            blobs.put(key, data)
            return {"ok": True}

    return r
