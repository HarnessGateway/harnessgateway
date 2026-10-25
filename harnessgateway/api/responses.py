"""The OpenAI Responses API. A foreground turn belongs to the request that made it: when the client goes away, the turn
is stopped. A background turn (`background: true`) outlives its request and is read or streamed later."""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..domain import wire
from ..domain.turn import Reason
from ..services.principal import Principal
from .http import platform, principal, read_json

router = APIRouter(prefix="/v1")
KEEPALIVE = b": keep-alive\n\n"


def _stream(request: Request, p: Principal, rid: str, after: int, owns: bool) -> StreamingResponse:
    turns = platform(request).turns

    async def body() -> AsyncIterator[bytes]:
        ended = False
        try:
            async for event in turns.follow(p, rid, after):
                if event is None:
                    yield KEEPALIVE
                    continue
                yield wire.sse(event)
                ended = event["type"] in wire.TERMINAL
        finally:
            if owns and not ended:                       # the client left before the end: stop its turn
                with contextlib.suppress(Exception):
                    await asyncio.shield(turns.cancel(p, rid, Reason.CLIENT_DISCONNECTED))

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _await(request: Request, p: Principal, rid: str) -> dict[str, Any]:
    turns = platform(request).turns
    waiting = asyncio.create_task(turns.wait(p, rid))
    try:
        while not waiting.done():
            if await request.is_disconnected():
                waiting.cancel()
                await asyncio.shield(turns.cancel(p, rid, Reason.CLIENT_DISCONNECTED))
                return await turns.response(p, rid)
            await asyncio.wait({waiting}, timeout=1)
        return waiting.result()
    finally:
        waiting.cancel()


@router.post("/responses")
async def create_response(request: Request, p: Principal = Depends(principal)) -> Any:
    body = await read_json(request)
    turn = await platform(request).turns.create(p, body, request.headers.get("idempotency-key", ""))
    rid = turn["id"]
    if turn.get("stream") or body.get("stream") is True:
        return _stream(request, p, rid, -1, owns=not body.get("background"))
    if body.get("background") is True:
        return await platform(request).turns.response(p, rid)
    return await _await(request, p, rid)


@router.get("/responses/{rid}")
async def get_response(request: Request, rid: str, stream: bool = False, starting_after: int = -1,
                       p: Principal = Depends(principal)) -> Any:
    if stream:
        await platform(request).turns.row(p, rid)
        return _stream(request, p, rid, starting_after, owns=False)
    return await platform(request).turns.response(p, rid)


@router.post("/responses/{rid}/cancel")
async def cancel_response(request: Request, rid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).turns.cancel(p, rid)


@router.delete("/responses/{rid}")
async def delete_response(request: Request, rid: str, p: Principal = Depends(principal)) -> Any:
    await platform(request).turns.delete(p, rid)
    return {"id": rid, "object": "response", "deleted": True}


@router.get("/responses/{rid}/input_items")
async def input_items(request: Request, rid: str, limit: int = 20, order: str = "desc", after: str | None = None,
                      p: Principal = Depends(principal)) -> Any:
    items = await platform(request).turns.input_items(p, rid)
    if order == "desc":
        items = list(reversed(items))
    if after:
        idx = next((i for i, it in enumerate(items) if it["id"] == after), -1)
        items = items[idx + 1:]
    data = items[:max(1, min(limit, 100))]
    return JSONResponse({"object": "list", "data": data, "first_id": data[0]["id"] if data else None,
                         "last_id": data[-1]["id"] if data else None, "has_more": len(items) > len(data)})
