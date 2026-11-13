"""Uploaded files, and a session's work read as an OpenAI container (spec §13.9, §13.10)."""

import hashlib
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, UploadFile
from fastapi.responses import StreamingResponse

from harnessgateway.domain.names import container_id, session_of_container
from harnessgateway.domain.principal import Principal
from harnessgateway.domain.snapshot import Entry
from harnessgateway.errors import Invalid, NotFound
from harnessgateway.platform import Platform
from harnessgateway.store.files import File

PURPOSES = ("assistants", "batch", "fine-tune", "vision", "user_data", "evals")
_PIECE = 1024 * 1024
_WORK = "work/"


def page_of(items: list[dict[str, Any]], *, after: str | None, limit: int) -> dict[str, Any]:
    """One page of a list, in the shape the OpenAI SDK pages through."""
    if not 1 <= limit <= 100:
        raise Invalid("limit is 1 to 100", param="limit")
    start = 0
    if after is not None:
        ids = [item["id"] for item in items]
        if after not in ids:
            raise Invalid(f"after names nothing in this list: {after}", param="after")
        start = ids.index(after) + 1
    shown = items[start:start + limit]
    return {"object": "list", "data": shown, "first_id": shown[0]["id"] if shown else None,
            "last_id": shown[-1]["id"] if shown else None, "has_more": start + limit < len(items)}


def _file(file: File) -> dict[str, Any]:
    return {"id": file.id, "object": "file", "bytes": file.bytes, "created_at": int(file.created_at.timestamp()),
            "filename": file.filename, "purpose": file.purpose, "status": "processed"}


def files_router(platform: Platform, caller: Any) -> APIRouter:
    router = APIRouter()
    files = platform.stores.files
    Caller = Annotated[Principal, Depends(caller)]

    @router.post("/v1/files")
    async def upload(principal: Caller, file: UploadFile, purpose: Annotated[str, Form()]) -> Any:
        if purpose not in PURPOSES:
            raise Invalid(f"purpose is one of {', '.join(PURPOSES)}", param="purpose")

        async def parts() -> AsyncIterator[bytes]:
            while piece := await file.read(_PIECE):
                yield piece
        return _file(await files.create(principal.org_id, filename=file.filename or "", purpose=purpose,
                                        parts=parts()))

    @router.get("/v1/files")
    async def listing(principal: Caller, limit: int = 20, after: str | None = None) -> Any:
        if not 1 <= limit <= 100:
            raise Invalid("limit is 1 to 100", param="limit")
        found = await files.page(principal.org_id, after=after, limit=limit + 1)
        shown = [_file(file) for file in found[:limit]]
        return {"object": "list", "data": shown, "first_id": shown[0]["id"] if shown else None,
                "last_id": shown[-1]["id"] if shown else None, "has_more": len(found) > limit}

    @router.get("/v1/files/{file_id}")
    async def one(file_id: str, principal: Caller) -> Any:
        return _file(await files.get(principal.org_id, file_id))

    @router.get("/v1/files/{file_id}/content")
    async def content(file_id: str, principal: Caller) -> StreamingResponse:
        file = await files.get(principal.org_id, file_id)
        return StreamingResponse(files.content(file), media_type="application/octet-stream",
                                 headers={"Content-Length": str(file.bytes)})

    @router.delete("/v1/files/{file_id}")
    async def delete(file_id: str, principal: Caller) -> Any:
        await files.delete(principal.org_id, file_id)
        return {"id": file_id, "object": "file", "deleted": True}

    return router


def container_file_id(path: str) -> str:
    """A file's id in a container: made from its path, so it is the same for as long as the path exists."""
    return f"cfile_{hashlib.sha256(path.encode()).hexdigest()[:32]}"


def containers_router(platform: Platform, caller: Any) -> APIRouter:
    router = APIRouter()
    stores = platform.stores
    Caller = Annotated[Principal, Depends(caller)]

    async def work(org_id: str, container: str) -> tuple[str, dict[str, Entry]]:
        """The files of a session's work folder as its head turn left them, by path relative to the folder."""
        try:
            session = await stores.turns.session(org_id, session_of_container(container))
        except (Invalid, NotFound):
            raise NotFound(f"there is no container {container}") from None
        head = await stores.turns.get(org_id, session.head_turn_id) if session.head_turn_id else None
        if head is None or head.snapshot_id is None:
            return session.id, {}
        manifest = await stores.snapshots.manifest(org_id, head.snapshot_id)
        return session.id, {path[len(_WORK):]: entry for path, entry in sorted(manifest.entries.items())
                            if entry.kind == "f" and path.startswith(_WORK)}

    def shown(container: str, path: str, entry: Entry) -> dict[str, Any]:
        return {"id": container_file_id(path), "object": "container.file", "container_id": container,
                "created_at": entry.mtime // 1_000_000_000, "bytes": entry.size, "path": f"/work/{path}",
                "source": "assistant"}

    async def find(org_id: str, container: str, file_id: str) -> tuple[str, Entry]:
        _, files = await work(org_id, container)
        for path, entry in files.items():
            if container_file_id(path) == file_id:
                return path, entry
        raise NotFound(f"container {container} has no file {file_id}")

    @router.get("/v1/containers/{container}")
    async def one(container: str, principal: Caller) -> Any:
        session_id, _ = await work(principal.org_id, container)
        summary = await stores.turns.session_summary(principal.org_id, session_id)
        return {"id": container_id(session_id), "object": "container", "name": session_id, "status": "running",
                "created_at": int(summary.created_at.timestamp()),
                "last_active_at": int(summary.last_active_at.timestamp())}

    @router.get("/v1/containers/{container}/files")
    async def listing(container: str, principal: Caller, limit: int = 20, after: str | None = None) -> Any:
        _, files = await work(principal.org_id, container)
        return page_of([shown(container, path, entry) for path, entry in files.items()], after=after, limit=limit)

    @router.get("/v1/containers/{container}/files/{file_id}")
    async def one_file(container: str, file_id: str, principal: Caller) -> Any:
        path, entry = await find(principal.org_id, container, file_id)
        return shown(container, path, entry)

    @router.get("/v1/containers/{container}/files/{file_id}/content")
    async def content(container: str, file_id: str, principal: Caller) -> StreamingResponse:
        _, entry = await find(principal.org_id, container, file_id)
        return StreamingResponse(stores.snapshots.content(principal.org_id, entry),
                                 media_type="application/octet-stream", headers={"Content-Length": str(entry.size)})

    return router
