"""The OpenAI Responses API (SPEC §14.1). A foreground turn belongs to the request that made it: when the caller goes
away, the turn is cancelled. A background turn outlives its request and is read or streamed later."""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..domain import contract
from ..services.pages import check_limit, page
from ..services.principal import Principal
from .http import platform, principal, read_json

router = APIRouter(prefix="/v1")
KEEPALIVE = b": keep-alive\n\n"
DISCONNECT_POLL_S = 1


def _stream(request: Request, p: Principal, rid: str, after: int, owns: bool) -> StreamingResponse:
    turns = platform(request).turns

    async def body() -> AsyncIterator[bytes]:
        ended = False
        try:
            async for event in turns.follow(p, rid, after):
                if event is None:
                    yield KEEPALIVE
                    continue
                yield contract.sse(event)
                ended = event["type"] in contract.TERMINAL
        finally:
            if owns and not ended:
                with contextlib.suppress(Exception):
                    await asyncio.shield(turns.cancel(p, rid))

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _await(request: Request, p: Principal, rid: str) -> dict[str, Any]:
    turns = platform(request).turns
    waiting = asyncio.create_task(turns.wait(p, rid))
    try:
        while not waiting.done():
            if await request.is_disconnected():
                await asyncio.shield(turns.cancel(p, rid))
                return await turns.response(p, rid)
            await asyncio.wait({waiting}, timeout=DISCONNECT_POLL_S)
        return waiting.result()
    finally:
        waiting.cancel()


@router.post("/responses")
async def create_response(request: Request, p: Principal = Depends(principal)) -> Any:
    turn = await platform(request).turns.create(p, await read_json(request), request.headers.get("idempotency-key", ""))
    if turn["stream"]:
        return _stream(request, p, turn["id"], -1, owns=not turn["background"])
    if turn["background"]:
        return await platform(request).turns.response(p, turn["id"])
    return await _await(request, p, turn["id"])


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
        items.reverse()
    if after:
        idx = next((i for i, it in enumerate(items) if it["id"] == after), -1)
        items = items[idx + 1:]
    return page(items, check_limit(limit))
