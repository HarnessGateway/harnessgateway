"""The OpenAI Responses API and what hangs off a response: sessions, their workspaces and traces, and
rodeos. `POST /v1/responses` hands the body to the harness package whole; a foreground turn belongs
to the request that made it and stops when the request closes."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from ..contract import wire
from ..domain.members import Member
from ..domain.sessions import Session, Turn
from ..errors import Invalid


class RodeoBody(BaseModel):
    task: str = ""
    entries: list[dict[str, Any]]


class WinnerBody(BaseModel):
    winner: str = ""


class SteerBody(BaseModel):
    input: str


def session_view(s: Session) -> dict[str, Any]:
    return {"id": s.id, "object": "session", "harness": s.harness, "version": s.version, "team": s.team, "member": s.member,
            "title": s.title, "created_at": int(s.created), "stored": s.stored, "status": "running" if s.running else "idle",
            "responses": list(s.responses), "workspace": bool(s.snapshot_key)}


def router(service) -> APIRouter:
    principals, sessions, rodeos = service.principals, service.sessions, service.rodeos
    r = APIRouter(prefix="/v1")
    member = Depends(principals.member)

    def streamed(who: Member, t: Turn, *, owns: bool = False) -> StreamingResponse:
        """The turn's event stream. When the request `owns` the turn (it made it, in the foreground),
        the client closing the stream stops the turn gracefully: a request that is gone has no one
        waiting for it. A follower of a background turn closing its stream changes nothing."""
        async def ended() -> dict[str, Any]:
            return sessions.response_object(await sessions.wait(t.id))

        async def events() -> Any:
            try:
                async for chunk in wire.stream(sessions.response_object(t), sessions.follow(who, t.id), ended):
                    yield chunk
            finally:
                if owns:
                    sessions.disconnected(who, t.id)   # nothing when the turn has ended or runs in the background
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Session-Id": t.session})

    async def awaited(request: Request, who: Member, t: Turn) -> Turn:
        """The turn's end, unless the client stops waiting first: then the turn is stopped gracefully."""
        while not (t := sessions.turn(who, t.id)).final:
            if await request.is_disconnected():
                return sessions.disconnected(who, t.id)
            await asyncio.sleep(0.25)
        return t

    @r.post("/responses")
    async def create_response(request: Request, who: Member = member) -> Any:
        try:
            body = await request.json()
        except ValueError:
            raise Invalid("the request body is JSON") from None
        req = wire.Request.parse(body)
        t = await asyncio.to_thread(service.intake.create, who, req, request.headers.get("idempotency-key", ""))
        if req.stream:
            return streamed(who, t, owns=not req.background)
        if not req.background:
            t = await awaited(request, who, t)
        return JSONResponse(sessions.response_object(t), headers={"X-Session-Id": t.session})

    @r.get("/responses/{rid}")
    def get_response(rid: str, stream: bool = False, who: Member = member) -> Any:
        t = sessions.turn(who, rid)
        return streamed(who, t) if stream else sessions.response_object(t)

    @r.get("/responses/{rid}/detail")
    def response_detail(rid: str, who: Member = member) -> dict:
        """The response plus what the package printed: its log lines, stderr, exit code, the items so far."""
        t = sessions.turn(who, rid)
        log, stderr = sessions.live_log(t)
        return {**sessions.response_object(t), "output": sessions.live_items(t) if not t.final else t.output, "log": log, "stderr": stderr,
                "exit_code": t.exit_code, "worker": t.worker, "started": t.started, "ended": t.ended, "text": t.text, "status_raw": t.status}

    @r.get("/responses/{rid}/request")
    def response_request(rid: str, who: Member = member) -> dict:
        return sessions.request_body(who, rid)

    @r.post("/responses/{rid}/cancel")
    async def cancel_response(rid: str, who: Member = member) -> dict:
        t = sessions.cancel(who, rid)
        if not t.final:
            t = await sessions.wait(rid)
        return sessions.response_object(t)

    @r.get("/responses/{rid}/captures")
    def list_captures(rid: str, who: Member = member) -> dict:
        return {"object": "list", "data": sessions.captures(who, rid)}

    @r.get("/responses/{rid}/captures/{name:path}")
    def get_capture(rid: str, name: str, who: Member = member) -> Response:
        return Response(sessions.capture(who, rid, name), media_type="application/octet-stream")

    # sessions
    @r.get("/sessions")
    def list_sessions(who: Member = member) -> dict:
        return {"object": "list", "data": [session_view(s) for s in sessions.list(who)]}

    @r.get("/sessions/{sid}")
    def get_session(sid: str, who: Member = member) -> dict:
        s = sessions.session(who, sid)
        return {**session_view(s), "turns": [{**sessions.response_object(t), "text": t.text} for t in sessions.turns_of(who, sid)]}

    @r.post("/sessions/{sid}/interrupt")
    def interrupt(sid: str, body: SteerBody, who: Member = member) -> dict:
        return sessions.response_object(sessions.interrupt(who, sid, body.input))

    @r.get("/sessions/{sid}/trace")
    def trace(sid: str, who: Member = member) -> dict:
        return {"object": "list", "data": sessions.trace(who, sid)}

    @r.get("/sessions/{sid}/files")
    def list_files(sid: str, who: Member = member) -> dict:
        return {"object": "list", "data": sessions.files(who, sid)}

    @r.get("/sessions/{sid}/files/{path:path}")
    def get_file(sid: str, path: str, who: Member = member) -> Response:
        return Response(sessions.file(who, sid, path), media_type="application/octet-stream")

    @r.get("/sessions/{sid}/archive")
    def archive(sid: str, who: Member = member) -> Response:
        return Response(sessions.archive(who, sid), media_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="{sid}.tar.gz"'})

    @r.delete("/sessions/{sid}")
    def delete_session(sid: str, who: Member = member) -> dict:
        sessions.delete(who, sid)
        return {"deleted": sid}

    # rodeos
    @r.post("/rodeos")
    def create_rodeo(body: RodeoBody, who: Member = member) -> dict:
        return rodeos.create(who, body.task, body.entries)

    @r.get("/rodeos")
    def list_rodeos(who: Member = member) -> dict:
        return {"object": "list", "data": rodeos.list(who), "leaderboard": rodeos.leaderboard(who)}

    @r.get("/rodeos/{aid}")
    def get_rodeo(aid: str, who: Member = member) -> dict:
        return rodeos.get(who, aid)

    @r.patch("/rodeos/{aid}")
    def pick_winner(aid: str, body: WinnerBody, who: Member = member) -> dict:
        return rodeos.pick(who, aid, body.winner)

    @r.delete("/rodeos/{aid}")
    def delete_rodeo(aid: str, who: Member = member) -> dict:
        rodeos.delete(who, aid)
        return {"deleted": aid}

    return r
