"""Management, admin scope (SPEC §14.2): keys, providers, the organization's egress, harnesses, usage and traces."""
from __future__ import annotations

import mimetypes
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ..errors import Invalid
from ..services.principal import Principal
from .http import admin, form_file, platform, principal, read_json

router = APIRouter(prefix="/v1")


@router.get("/platform")
async def platform_info(request: Request, p: Principal = Depends(principal)) -> Any:
    """The version and the extensions loaded: what a console reads to know which pages exist."""
    return platform(request).operations.platform()


@router.get("/organization")
async def organization(request: Request, p: Principal = Depends(principal)) -> Any:
    org = await platform(request).orgs.get(p.org_id)
    return {**org, "scope": p.scope}


@router.get("/keys")
async def list_keys(request: Request, p: Principal = Depends(admin)) -> Any:
    return {"object": "list", "data": await platform(request).keys.list(p.org_id)}


@router.post("/keys")
async def create_key(request: Request, p: Principal = Depends(admin)) -> Any:
    body = await read_json(request)
    return await platform(request).keys.create(p.org_id, str(body.get("scope", "run")), str(body.get("label", "")))


@router.delete("/keys/{key_id}")
async def revoke_key(request: Request, key_id: str, p: Principal = Depends(admin)) -> Any:
    await platform(request).keys.revoke(p.org_id, key_id)
    return {"id": key_id, "object": "api_key", "deleted": True}


@router.get("/providers")
async def list_providers(request: Request, p: Principal = Depends(principal)) -> Any:
    return {"object": "list", "data": await platform(request).providers.list(p.org_id)}


@router.put("/providers/{name}")
async def put_provider(request: Request, name: str, p: Principal = Depends(admin)) -> Any:
    body = await read_json(request)
    return await platform(request).providers.put(p.org_id, name, body.get("api"), body.get("base_url"), body.get("key"))


@router.get("/providers/{name}")
async def get_provider(request: Request, name: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).providers.get(p.org_id, name)


@router.delete("/providers/{name}")
async def delete_provider(request: Request, name: str, p: Principal = Depends(admin)) -> Any:
    await platform(request).providers.delete(p.org_id, name)
    return {"id": name, "object": "provider", "deleted": True}


@router.get("/egress")
async def get_egress(request: Request, p: Principal = Depends(principal)) -> Any:
    return {"object": "egress", "egress": await platform(request).orgs.egress(p.org_id)}


@router.put("/egress")
async def put_egress(request: Request, p: Principal = Depends(admin)) -> Any:
    body = await read_json(request)
    return {"object": "egress", "egress": await platform(request).orgs.set_egress(p.org_id, body.get("egress"))}


@router.get("/harnesses")
async def list_harnesses(request: Request, p: Principal = Depends(principal)) -> Any:
    plat = platform(request)
    return {"object": "list", "data": await plat.harnesses.list(p.org_id), "builtins": plat.harnesses.builtins()}


@router.post("/harnesses")
async def install_harness(request: Request, p: Principal = Depends(admin)) -> Any:
    """Install or upgrade: {"builtin": name}, {"git": {"url", "ref", "path"}}, or a multipart `package` archive."""
    harnesses = platform(request).harnesses
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        upload, _ = await form_file(request, "package")
        return await harnesses.install_upload(p.org_id, await upload.read())
    body = await read_json(request)
    if isinstance(body.get("builtin"), str):
        return await harnesses.install_builtin(p.org_id, body["builtin"])
    git = body.get("git")
    if isinstance(git, dict):
        return await harnesses.install_git(p.org_id, git.get("url"), git.get("ref", "main"), git.get("path", "."))
    raise Invalid('install with {"builtin": name}, {"git": {"url": ..., "ref": ..., "path": ...}} or a multipart '
                  '`package` upload')


@router.get("/harnesses/{name}")
async def get_harness(request: Request, name: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).harnesses.get(p.org_id, name)


@router.get("/harnesses/{name}/files")
async def harness_files(request: Request, name: str, p: Principal = Depends(admin)) -> Any:
    return {"object": "list", "data": await platform(request).harnesses.files(p.org_id, name)}


@router.get("/harnesses/{name}/files/{path:path}")
async def harness_file(request: Request, name: str, path: str, p: Principal = Depends(admin)) -> Any:
    data = await platform(request).harnesses.file(p.org_id, name, path)
    kind = mimetypes.guess_type(path)[0] or ("text/plain" if _is_text(data) else "application/octet-stream")
    return Response(data, media_type=f"{kind}; charset=utf-8" if kind.startswith("text/") or kind.endswith("json") else kind)


def _is_text(data: bytes) -> bool:
    try:
        data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return b"\0" not in data[:4096]


@router.patch("/harnesses/{name}")
async def configure_harness(request: Request, name: str, p: Principal = Depends(admin)) -> Any:
    body = await read_json(request)
    unknown = sorted(set(body) - {"settings", "secrets", "default_provider", "default_model"})
    if unknown:
        raise Invalid(f"a harness is configured with settings, secrets, default_provider and default_model, not "
                      f"{', '.join(unknown)}")
    return await platform(request).harnesses.configure(p.org_id, name, settings=body.get("settings"),
                                                       secrets=body.get("secrets"),
                                                       default_provider=body.get("default_provider"),
                                                       default_model=body.get("default_model"))


@router.delete("/harnesses/{name}")
async def uninstall_harness(request: Request, name: str, p: Principal = Depends(admin)) -> Any:
    await platform(request).harnesses.uninstall(p.org_id, name)
    return {"id": name, "object": "harness", "deleted": True}


@router.get("/usage")
async def usage(request: Request, start: int | None = None, end: int | None = None, group_by: str | None = None,
                p: Principal = Depends(admin)) -> Any:
    now = datetime.now(UTC)
    s = datetime.fromtimestamp(start, UTC) if start else now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    e = datetime.fromtimestamp(end, UTC) if end else now
    return await platform(request).usage.summary(p.org_id, s, e, group_by)


@router.get("/traces/{rid}")
async def trace(request: Request, rid: str, p: Principal = Depends(admin)) -> Any:
    return await platform(request).traces.get(p.org_id, rid)
