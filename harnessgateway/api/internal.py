"""Layer 6: the workers' routes. A worker joins with the join token, then pulls with its own credential:
heartbeat (and receive commands), claim, report a start, post chunks, post the result. Blob URLs for
the directory backend are served here too; their token is their authorization."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from ..blobs import DirBlobs
from ..errors import Invalid
from ..sessions import Sessions
from ..store import Store, Worker
from .auth import Principals


class JoinBody(BaseModel):
    name: str
    slots: int = 4
    version: str = ""


class HeartbeatBody(BaseModel):
    running: list[str] = []


class ChunksBody(BaseModel):
    chunks: list[dict[str, Any]]


def router(store: Store, sessions: Sessions, principals: Principals, version: str) -> APIRouter:
    r = APIRouter(prefix="/internal")

    @r.post("/join")
    def join(body: JoinBody, _: None = Depends(principals.joiner)) -> dict:
        w, secret = store.register_worker(body.name, body.slots, body.version)
        return {"id": w.id, "secret": secret, "version": version}

    @r.post("/heartbeat")
    def heartbeat(body: HeartbeatBody, w: Worker = Depends(principals.worker)) -> dict:
        return {"commands": sessions.commands(w, body.running)}

    @r.post("/claim")
    def claim(w: Worker = Depends(principals.worker)) -> Response:
        job = sessions.claim(w)
        if job is None:
            return Response(status_code=204)
        import json
        return Response(json.dumps(job), media_type="application/json")

    @r.post("/turns/{rid}/started")
    def started(rid: str, w: Worker = Depends(principals.worker)) -> dict:
        sessions.started(w, rid)
        return {"ok": True}

    @r.post("/turns/{rid}/chunks")
    def chunks(rid: str, body: ChunksBody, w: Worker = Depends(principals.worker)) -> dict:
        sessions.chunks_in(w, rid, body.chunks)
        return {"ok": True}

    @r.post("/turns/{rid}/result")
    def result(rid: str, body: dict, w: Worker = Depends(principals.worker)) -> dict:
        return {"status": sessions.result(w, rid, body).status}

    if isinstance(store.blobs, DirBlobs):
        blobs = store.blobs

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
