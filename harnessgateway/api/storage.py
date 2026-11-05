"""Files, containers and models, OpenAI-shaped (SPEC §14.1)."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse

from ..domain.access import Principal
from ..errors import NotFound
from .http import form_file, platform, principal, spooled

router = APIRouter(prefix="/v1")


def _attachment(name: str) -> dict[str, str]:
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}


@router.post("/files")
async def upload_file(request: Request, p: Principal = Depends(principal)) -> Any:
    upload, form = await form_file(request, "file")
    async with spooled(upload) as path:
        return await platform(request).files.upload(p, upload.filename or "", form.get("purpose", "user_data"), path)


@router.get("/files")
async def list_files(request: Request, purpose: str | None = None, limit: int = 20, after: str | None = None,
                     p: Principal = Depends(principal)) -> Any:
    return await platform(request).files.list(p, purpose, limit, after)


@router.get("/files/{fid}")
async def get_file(request: Request, fid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).files.get(p, fid)


@router.get("/files/{fid}/content")
async def file_content(request: Request, fid: str, p: Principal = Depends(principal)) -> Any:
    meta, chunks = await platform(request).files.content(p, fid)
    return StreamingResponse(chunks, media_type="application/octet-stream", headers=_attachment(meta["filename"]))


@router.delete("/files/{fid}")
async def delete_file(request: Request, fid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).files.delete(p, fid)


@router.get("/containers")
async def list_containers(request: Request, limit: int = 20, after: str | None = None, order: str = "desc",
                          user: str | None = None, p: Principal = Depends(principal)) -> Any:
    return await platform(request).containers.list(p, limit, after, order, user)


@router.get("/containers/{sid}")
async def get_container(request: Request, sid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).containers.get(p, sid)


@router.delete("/containers/{sid}")
async def delete_container(request: Request, sid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).containers.delete(p, sid)


@router.get("/containers/{sid}/responses")
async def list_container_responses(request: Request, sid: str, limit: int = 100, after: str | None = None,
                                   p: Principal = Depends(principal)) -> Any:
    return await platform(request).containers.list_responses(p, sid, limit, after)


@router.get("/containers/{sid}/traces")
async def container_traces(request: Request, sid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).traces.list_session(p, sid)


@router.get("/containers/{sid}/files")
async def list_container_files(request: Request, sid: str, limit: int = 100, after: str | None = None,
                               p: Principal = Depends(principal)) -> Any:
    return await platform(request).containers.list_files(p, sid, limit, after)


@router.post("/containers/{sid}/files")
async def put_container_file(request: Request, sid: str, p: Principal = Depends(principal)) -> Any:
    upload, form = await form_file(request, "file")
    async with spooled(upload) as path:
        return await platform(request).containers.put_file(p, sid, form.get("path") or upload.filename or "", path)


@router.get("/containers/{sid}/files/{fid}")
async def get_container_file(request: Request, sid: str, fid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).containers.get_file(p, sid, fid)


@router.get("/containers/{sid}/files/{fid}/content")
async def container_file_content(request: Request, sid: str, fid: str, p: Principal = Depends(principal)) -> Any:
    meta, data = await platform(request).containers.file_content(p, sid, fid)
    return Response(data, media_type="application/octet-stream", headers=_attachment(meta["path"].rsplit("/", 1)[-1]))


@router.delete("/containers/{sid}/files/{fid}")
async def delete_container_file(request: Request, sid: str, fid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).containers.delete_file(p, sid, fid)


async def _models(request: Request, p: Principal) -> list[dict[str, Any]]:
    """Every `model` the principal's project can send: each harness, on each provider whose API it speaks. An admin
    without a project sees every project's, a name once."""
    plat = platform(request)
    providers = await plat.providers.list(p.org_id)
    out: dict[str, dict[str, Any]] = {}
    for h in await plat.harnesses.list(p):
        names = [h["name"]] if not h["provider_apis"] else [
            f"{h['name']}/{prov['name']}" + (f"/{h['default_model']}" if h["default_model"] else "")
            for prov in providers if prov["api"] in h["provider_apis"]]
        for mid in names:
            out.setdefault(mid, {"id": mid, "object": "model", "created": h["created_at"], "owned_by": "harnessgateway"})
    return list(out.values())


@router.get("/models")
async def list_models(request: Request, p: Principal = Depends(principal)) -> Any:
    return {"object": "list", "data": await _models(request, p)}


@router.get("/models/{model_id:path}")
async def get_model(request: Request, model_id: str, p: Principal = Depends(principal)) -> Any:
    for m in await _models(request, p):
        if m["id"] == model_id:
            return m
    raise NotFound(f"no model {model_id}", code="model_not_found")
