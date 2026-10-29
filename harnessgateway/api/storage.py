"""Files, containers and models, OpenAI-shaped (SPEC §14.1)."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse

from ..errors import NotFound
from ..services.principal import Principal
from .http import form_file, platform, principal, spooled

router = APIRouter(prefix="/v1")


def _attachment(name: str) -> dict[str, str]:
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}


@router.post("/files")
async def upload_file(request: Request, p: Principal = Depends(principal)) -> Any:
    upload, form = await form_file(request, "file")
    async with spooled(upload) as path:
        return await platform(request).files.upload(p.org_id, upload.filename or "", form.get("purpose", "user_data"), path)


@router.get("/files")
async def list_files(request: Request, purpose: str | None = None, limit: int = 20, after: str | None = None,
                     p: Principal = Depends(principal)) -> Any:
    return await platform(request).files.list(p.org_id, purpose, limit, after)


@router.get("/files/{fid}")
async def get_file(request: Request, fid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).files.get(p.org_id, fid)


@router.get("/files/{fid}/content")
async def file_content(request: Request, fid: str, p: Principal = Depends(principal)) -> Any:
    meta, chunks = await platform(request).files.content(p.org_id, fid)
    return StreamingResponse(chunks, media_type="application/octet-stream", headers=_attachment(meta["filename"]))


@router.delete("/files/{fid}")
async def delete_file(request: Request, fid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).files.delete(p.org_id, fid)


@router.get("/containers")
async def list_containers(request: Request, limit: int = 20, after: str | None = None, order: str = "desc",
                          p: Principal = Depends(principal)) -> Any:
    return await platform(request).containers.list(p.org_id, limit, after, order)


@router.get("/containers/{sid}")
async def get_container(request: Request, sid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).containers.get(p.org_id, sid)


@router.delete("/containers/{sid}")
async def delete_container(request: Request, sid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).containers.delete(p.org_id, sid)


@router.get("/containers/{sid}/files")
async def list_container_files(request: Request, sid: str, limit: int = 100, after: str | None = None,
                               p: Principal = Depends(principal)) -> Any:
    return await platform(request).containers.list_files(p.org_id, sid, limit, after)


@router.post("/containers/{sid}/files")
async def put_container_file(request: Request, sid: str, p: Principal = Depends(principal)) -> Any:
    upload, form = await form_file(request, "file")
    async with spooled(upload) as path:
        return await platform(request).containers.put_file(p.org_id, sid, form.get("path") or upload.filename or "", path)


@router.get("/containers/{sid}/files/{fid}")
async def get_container_file(request: Request, sid: str, fid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).containers.get_file(p.org_id, sid, fid)


@router.get("/containers/{sid}/files/{fid}/content")
async def container_file_content(request: Request, sid: str, fid: str, p: Principal = Depends(principal)) -> Any:
    meta, data = await platform(request).containers.file_content(p.org_id, sid, fid)
    return Response(data, media_type="application/octet-stream", headers=_attachment(meta["path"].rsplit("/", 1)[-1]))


@router.delete("/containers/{sid}/files/{fid}")
async def delete_container_file(request: Request, sid: str, fid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).containers.delete_file(p.org_id, sid, fid)


async def _models(request: Request, p: Principal) -> list[dict[str, Any]]:
    """Every `model` the organization can send: each harness, on each provider whose API it speaks."""
    plat = platform(request)
    providers = await plat.providers.list(p.org_id)
    out = []
    for h in await plat.harnesses.list(p.org_id):
        if not h["provider_apis"]:
            out.append({"id": h["name"], "object": "model", "created": h["created_at"], "owned_by": "harnessgateway"})
            continue
        for prov in providers:
            if prov["api"] in h["provider_apis"]:
                mid = f"{h['name']}/{prov['name']}" + (f"/{h['default_model']}" if h["default_model"] else "")
                out.append({"id": mid, "object": "model", "created": h["created_at"], "owned_by": "harnessgateway"})
    return out


@router.get("/models")
async def list_models(request: Request, p: Principal = Depends(principal)) -> Any:
    return {"object": "list", "data": await _models(request, p)}


@router.get("/models/{model_id:path}")
async def get_model(request: Request, model_id: str, p: Principal = Depends(principal)) -> Any:
    for m in await _models(request, p):
        if m["id"] == model_id:
            return m
    raise NotFound(f"no model {model_id}", code="model_not_found")
