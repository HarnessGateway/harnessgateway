"""Sessions, as the requests and responses they are made of: everything needed to draw a session's conversation,
whichever application held it. A session is its owner's; anyone else reaches it only as `reaches` allows."""

from datetime import UTC, datetime
from typing import Any

from starlette.requests import Request
from starlette.routing import Route

from harnessgateway.api.http import READ_ALL, Api, limit_of, listing, may, reaches
from harnessgateway.domain.line import NONE
from harnessgateway.domain.principal import Principal
from harnessgateway.errors import NotFound
from harnessgateway.ids import container_of, is_id
from harnessgateway.store.sessions import Session
from harnessgateway.store.turns import Turn


def _time(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _session(session: Session) -> dict[str, Any]:
    ran_with = [session.environment, session.harness, session.provider or NONE, session.model or NONE]
    return {"id": session.id, "object": "session", "owner": session.owner, "container_id": container_of(session.id),
            "head_response_id": session.head_turn_id, "forked_from_response_id": session.forked_from_turn_id,
            "turns": session.turns, "bytes": session.bytes, "status": session.status,
            "model": "/".join(ran_with) if session.environment and session.harness else None,     # type: ignore[arg-type]
            "created_at": _time(session.created_at), "last_active_at": _time(session.last_active_at)}


def _turn(turn: Turn) -> dict[str, Any]:
    """A turn as its two payloads: the request as it was sent, and the response as the Responses API returns it.
    The log is what its sandbox printed beside them, and `used` what that sandbox used. A deleted turn has neither
    payload."""
    kept = turn.deleted_at is None
    return {"id": turn.id, "object": "session.turn", "number": turn.number, "parent_id": turn.parent_id,
            "status": turn.status, "model": str(turn.line), "deleted": not kept, "request": turn.request,
            "response": turn.response() if kept else None, "log": turn.log, "used": turn.used,
            "created_at": _time(turn.created_at),
            "started_at": _time(turn.started_at), "completed_at": _time(turn.completed_at)}


def routes(api: Api) -> list[Route]:
    platform = api.platform
    sessions = platform.sessions

    def session_id(request: Request) -> str:
        if not is_id(request.path_params["id"], "session"):
            raise NotFound(f"there is no session {request.path_params['id']}")
        return str(request.path_params["id"])

    async def page(request: Request, who: Principal) -> Any:
        limit = limit_of(request)
        mine = None if await may(platform, who, READ_ALL) else who.subject
        found = await sessions.page(who.org_id, owner=mine, after=request.query_params.get("after"), limit=limit + 1)
        return listing([_session(session) for session in found[:limit]], has_more=len(found) > limit)

    async def one(request: Request, who: Principal) -> Any:
        await reaches(platform, who, session_id(request))
        return _session(await sessions.get(who.org_id, session_id(request)))

    async def turns(request: Request, who: Principal) -> Any:
        await reaches(platform, who, session_id(request))
        return listing([_turn(turn) for turn in await sessions.turns(who.org_id, session_id(request))])

    async def delete(request: Request, who: Principal) -> Any:
        await reaches(platform, who, session_id(request), to_change=True)
        await platform.remove_session(who.org_id, session_id(request), by=who.subject)
        return {"id": request.path_params["id"], "object": "session", "deleted": True}

    return [
        api.route("/v1/sessions", "GET", "sessions.read", page),
        api.route("/v1/sessions/{id}", "GET", "sessions.read", one),
        api.route("/v1/sessions/{id}", "DELETE", "sessions.write", delete),
        api.route("/v1/sessions/{id}/turns", "GET", "sessions.read", turns),
    ]
