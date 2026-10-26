"""The OpenAI Files, Containers and Models APIs."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from starlette.datastructures import UploadFile

from ..errors import Invalid, NotFound
from ..services.principal import Principal
from .http import platform, principal

router = APIRouter(prefix="/v1")


async def _upload(request: Request, field: str = "file") -> tuple[UploadFile, dict[str, Any]]:
    form = await request.form()
    upload = form.get(field)
    if not isinstance(upload, UploadFile):
        raise Invalid(f"a multipart form with a `{field}` file part", param=field)
    upload.file.seek(0, 2)
    size = upload.file.tell()
    upload.file.seek(0)
    return upload, {"size": size, **{k: v for k, v in form.items() if isinstance(v, str)}}


def _attachment(name: str) -> dict[str, str]:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:200] or "file"
    return {"Content-Disposition": f'attachment; filename="{safe}"', "X-Content-Type-Options": "nosniff"}


# files ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.post("/files")
async def upload_file(request: Request, p: Principal = Depends(principal)) -> Any:
    upload, form = await _upload(request)
    return await platform(request).files.upload(p, upload.filename or "", form.get("purpose", "user_data"),
                                               upload.file, form["size"])


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


# containers (sessions) ──────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/containers")
async def list_containers(request: Request, limit: int = 20, after: str | None = None, order: str = "desc",
                          p: Principal = Depends(principal)) -> Any:
    return await platform(request).containers.list(p, limit, after, order)


@router.post("/containers")
async def create_container(request: Request, p: Principal = Depends(principal)) -> Any:
    raise Invalid("a container is created by its first response: POST /v1/responses with model=<harness>; continue it "
                  "with previous_response_id", code="unsupported")


@router.get("/containers/{sid}")
async def get_container(request: Request, sid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).containers.get(p, sid)


@router.delete("/containers/{sid}")
async def delete_container(request: Request, sid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).containers.delete(p, sid)


@router.get("/containers/{sid}/files")
async def list_container_files(request: Request, sid: str, limit: int = 100, after: str | None = None,
                               p: Principal = Depends(principal)) -> Any:
    return await platform(request).containers.list_files(p, sid, limit, after)


@router.post("/containers/{sid}/files")
async def put_container_file(request: Request, sid: str, p: Principal = Depends(principal)) -> Any:
    upload, form = await _upload(request)
    path = form.get("path") or upload.filename or ""
    return await platform(request).containers.put_file(p, sid, path, upload.file, form["size"])


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


# models ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
async def _models(request: Request, p: Principal) -> list[dict[str, Any]]:
    plat = platform(request)
    harnesses = await plat.packages.list(p.org_id)
    providers = await plat.providers.list(p)
    out = []
    for h in harnesses:
        created = h["created_at"]
        if not h["provider_apis"]:
            out.append({"id": h["name"], "object": "model", "created": created, "owned_by": "harnessgateway"})
            continue
        for prov in providers:
            if prov["api"] in h["provider_apis"]:
                mid = f"{h['name']}/{prov['name']}" + (f"/{h['default_model']}" if h["default_model"] else "")
                out.append({"id": mid, "object": "model", "created": created, "owned_by": "harnessgateway"})
    return out


@router.get("/models")
async def list_models(request: Request, p: Principal = Depends(principal)) -> Any:
    return {"object": "list", "data": await _models(request, p)}


@router.get("/models/{model_id:path}")
async def get_model(request: Request, model_id: str, p: Principal = Depends(principal)) -> Any:
    for m in await _models(request, p):
        if m["id"] == model_id:
            return m
    raise NotFound(f"no model {model_id}", param="model")
