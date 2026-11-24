"""The OpenAI Containers API, read-only: a session's work, as its latest saved turn left it. It is how what an
agent made is fetched, by any client that already speaks that API."""

import hashlib
from collections import OrderedDict
from typing import Any

from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from harnessgateway.api.http import Api, page_of
from harnessgateway.domain.principal import Principal
from harnessgateway.domain.snapshot import Entry
from harnessgateway.errors import NotFound
from harnessgateway.ids import container_of, is_id, session_of
from harnessgateway.platform import Platform
from harnessgateway.store.sessions import Session

_WORK = "work/"                                # a container shows the work folder; the mind is the agent's own
_KEPT = 16


class Work:
    """A session's work, as its latest saved turn left it: its files by path. A snapshot never changes and a
    client asks for many files of one, so the last few that were read are kept."""

    def __init__(self, platform: Platform) -> None:
        self._platform = platform
        self._read: OrderedDict[str, dict[str, Entry]] = OrderedDict()

    async def files(self, org_id: str, session_id: str) -> dict[str, Entry]:
        saved = await self._platform.sessions.saved(org_id, session_id)
        if saved is None:
            return {}
        if saved not in self._read:
            self._read[saved] = (await self._platform.snapshots.manifest(org_id, saved)).files(_WORK)
            while len(self._read) > _KEPT:
                self._read.popitem(last=False)
        self._read.move_to_end(saved)
        return self._read[saved]


def routes(api: Api) -> list[Route]:
    sessions, snapshots, made = api.platform.sessions, api.platform.snapshots, Work(api.platform)

    def file_id(path: str) -> str:
        """A file's id in a container: made from its path, so it is the same for as long as the path is there."""
        return f"cfile_{hashlib.sha256(path.encode()).hexdigest()[:32]}"

    async def work(org_id: str, container: str) -> tuple[Session, dict[str, Entry]]:
        try:
            if not is_id(container, "container"):
                raise NotFound(container)
            session = await sessions.get(org_id, session_of(container))
        except NotFound:
            raise NotFound(f"there is no container {container}") from None
        return session, await made.files(org_id, session.id)

    def shown(container: str, path: str, entry: Entry) -> dict[str, Any]:
        return {"id": file_id(path), "object": "container.file", "container_id": container,
                "created_at": entry.mtime // 1_000_000_000, "bytes": entry.size, "path": f"/work/{path}",
                "source": "assistant"}

    async def find(request: Request, who: Principal) -> tuple[str, Entry]:
        container, wanted = request.path_params["id"], request.path_params["file"]
        for path, entry in (await work(who.org_id, container))[1].items():
            if file_id(path) == wanted:
                return path, entry
        raise NotFound(f"container {container} has no file {wanted}")

    async def one(request: Request, who: Principal) -> Any:
        session, _ = await work(who.org_id, request.path_params["id"])
        return {"id": container_of(session.id), "object": "container", "name": session.id, "status": "running",
                "created_at": int(session.created_at.timestamp()),
                "last_active_at": int(session.last_active_at.timestamp())}

    async def files(request: Request, who: Principal) -> Any:
        container = request.path_params["id"]
        found = (await work(who.org_id, container))[1]
        return page_of([shown(container, path, entry) for path, entry in found.items()], request)

    async def file(request: Request, who: Principal) -> Any:
        path, entry = await find(request, who)
        return shown(request.path_params["id"], path, entry)

    async def content(request: Request, who: Principal) -> Any:
        _, entry = await find(request, who)
        return StreamingResponse(snapshots.content(who.org_id, entry), media_type="application/octet-stream",
                                 headers={"Content-Length": str(entry.size)})

    return [
        api.route("/v1/containers/{id}", "GET", "containers.read", one),
        api.route("/v1/containers/{id}/files", "GET", "containers.read", files),
        api.route("/v1/containers/{id}/files/{file}", "GET", "containers.read", file),
        api.route("/v1/containers/{id}/files/{file}/content", "GET", "containers.read", content),
    ]
