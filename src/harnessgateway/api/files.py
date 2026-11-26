"""The OpenAI Files API: an uploaded file is what a request's input may name."""

from collections.abc import AsyncIterator
from typing import Any

from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from harnessgateway.api.http import Api, limit_of, listing
from harnessgateway.domain.principal import Principal
from harnessgateway.errors import Invalid

PURPOSES = ("assistants", "batch", "fine-tune", "vision", "user_data", "evals")      # the ones that API names
_PIECE = 1024 * 1024


def routes(api: Api) -> list[Route]:
    files = api.platform.files

    async def upload(request: Request, who: Principal) -> Any:
        form = await request.form()
        file, purpose = form.get("file"), form.get("purpose")
        if not isinstance(file, UploadFile):
            raise Invalid("upload the file as the form field `file`", param="file")
        if purpose not in PURPOSES:
            raise Invalid(f"purpose is one of {', '.join(PURPOSES)}", param="purpose")

        async def parts() -> AsyncIterator[bytes]:
            while piece := await file.read(_PIECE):
                yield piece
        kept = await files.create(who.org_id, filename=file.filename or "", purpose=str(purpose), parts=parts(),
                                  by=who.subject)
        return kept.shown()

    async def page(request: Request, who: Principal) -> Any:
        limit = limit_of(request)
        found = await files.page(who.org_id, after=request.query_params.get("after"), limit=limit + 1)
        return listing([file.shown() for file in found[:limit]], has_more=len(found) > limit)

    async def one(request: Request, who: Principal) -> Any:
        return (await files.get(who.org_id, request.path_params["id"])).shown()

    async def content(request: Request, who: Principal) -> Any:
        file = await files.get(who.org_id, request.path_params["id"])
        return StreamingResponse(files.content(who.org_id, file.id), media_type="application/octet-stream",
                                 headers={"Content-Length": str(file.bytes)})

    async def delete(request: Request, who: Principal) -> Any:
        await files.delete(who.org_id, request.path_params["id"], by=who.subject)
        return {"id": request.path_params["id"], "object": "file", "deleted": True}

    return [
        api.route("/v1/files", "POST", "files.write", upload),
        api.route("/v1/files", "GET", "files.read", page),
        api.route("/v1/files/{id}", "GET", "files.read", one),
        api.route("/v1/files/{id}", "DELETE", "files.write", delete),
        api.route("/v1/files/{id}/content", "GET", "files.read", content),
    ]
