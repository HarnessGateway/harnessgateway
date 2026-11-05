"""Management (SPEC §14.3): keys, providers, the organization's egress, harnesses, usage and traces. Each service checks
the principal against the access table; providers and egress are the organization's, so their checks are here."""
from __future__ import annotations

import mimetypes
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ..domain.access import Principal
from ..errors import Invalid
from .http import anyone, form_file, only, platform, principal, read_json

router = APIRouter(prefix="/v1")


@router.get("/platform", dependencies=[Depends(anyone)])
async def platform_info(request: Request) -> Any:
    """The version and the extensions loaded: what a console reads to know which pages exist."""
    return platform(request).operations.platform()


@router.get("/organization")
async def organization(request: Request, p: Principal = Depends(principal)) -> Any:
    """The organization and who the caller is in it."""
    org = await platform(request).orgs.get(p.org_id)
    return {**org, "principal": {"actor": p.actor, "role": p.role, "run_only": p.run_only,
                                 "platform_admin": p.platform_admin, "name": p.name}}


# ── keys ──────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/keys")
async def list_keys(request: Request, p: Principal = Depends(principal)) -> Any:
    p.require("keys", "read")
    return {"object": "list", "data": await platform(request).keys.list(p)}


@router.post("/keys")
async def create_key(request: Request, p: Principal = Depends(principal)) -> Any:
    """{label, role, run_only, expires_in_days}: the label names the application; role defaults to developer."""
    p.require("keys", "create")
    body = await read_json(request)
    only(body, "label", "role", "run_only", "expires_in_days")
    return await platform(request).keys.create(p, body.get("label"), body.get("role", "developer"),
                                               bool(body.get("run_only")), body.get("expires_in_days"))


@router.delete("/keys/{key_id}")
async def revoke_key(request: Request, key_id: str, p: Principal = Depends(principal)) -> Any:
    p.require("keys", "revoke")
    await platform(request).keys.revoke(p, key_id)
    return {"id": key_id, "object": "api_key", "deleted": True}


# ── providers and egress: the organization's ──────────────────────────────────────────────────────────────────────
@router.get("/providers")
async def list_providers(request: Request, p: Principal = Depends(principal)) -> Any:
    p.require("providers", "read")
    return {"object": "list", "data": await platform(request).providers.list(p.org_id)}


@router.put("/providers/{name}")
async def put_provider(request: Request, name: str, p: Principal = Depends(principal)) -> Any:
    p.require("providers", "update")
    plat, body = platform(request), await read_json(request)
    only(body, "api", "base_url", "key")
    out = await plat.providers.put(p.org_id, name, body.get("api"), body.get("base_url"), body.get("key"))
    await plat.audit.write(p.org_id, p.actor, "provider.put", name, {"api": out["api"], "base_url": out["base_url"],
                                                                      "key": "key" in body})
    return out


@router.get("/providers/{name}")
async def get_provider(request: Request, name: str, p: Principal = Depends(principal)) -> Any:
    p.require("providers", "read")
    return await platform(request).providers.get(p.org_id, name)


@router.delete("/providers/{name}")
async def delete_provider(request: Request, name: str, p: Principal = Depends(principal)) -> Any:
    p.require("providers", "delete")
    plat = platform(request)
    await plat.providers.delete(p.org_id, name)
    await plat.audit.write(p.org_id, p.actor, "provider.delete", name)
    return {"id": name, "object": "provider", "deleted": True}


@router.get("/egress")
async def get_egress(request: Request, p: Principal = Depends(principal)) -> Any:
    p.require("egress", "read")
    return {"object": "egress", "egress": await platform(request).orgs.egress(p.org_id)}


@router.put("/egress")
async def put_egress(request: Request, p: Principal = Depends(principal)) -> Any:
    p.require("egress", "update")
    plat, body = platform(request), await read_json(request)
    only(body, "egress")
    out = await plat.orgs.set_egress(p.org_id, body.get("egress"))
    await plat.audit.write(p.org_id, p.actor, "egress.update", "", {"egress": out})
    return {"object": "egress", "egress": out}


# ── harnesses ────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/harnesses")
async def list_harnesses(request: Request, p: Principal = Depends(principal)) -> Any:
    return {"object": "list", "data": await platform(request).harnesses.list(p)}


@router.post("/harnesses")
async def install_harness(request: Request, p: Principal = Depends(principal)) -> Any:
    """Install or upgrade from a multipart `package` archive: the package folder as a tar, gzipped or not."""
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise Invalid("install with a multipart `package` upload: the package folder as a tar archive")
    upload, _ = await form_file(request, "package")
    return await platform(request).harnesses.install(p, await upload.read())


@router.get("/harnesses/{name}")
async def get_harness(request: Request, name: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).harnesses.get(p, name)


@router.get("/harnesses/{name}/files")
async def harness_files(request: Request, name: str, p: Principal = Depends(principal)) -> Any:
    p.require("harnesses", "configure")
    return {"object": "list", "data": await platform(request).harnesses.files(p, name)}


@router.get("/harnesses/{name}/files/{path:path}")
async def harness_file(request: Request, name: str, path: str, p: Principal = Depends(principal)) -> Any:
    p.require("harnesses", "configure")
    data = await platform(request).harnesses.file(p, name, path)
    kind = mimetypes.guess_type(path)[0] or ("text/plain" if _is_text(data) else "application/octet-stream")
    return Response(data, media_type=f"{kind}; charset=utf-8" if kind.startswith("text/") or kind.endswith("json") else kind)


def _is_text(data: bytes) -> bool:
    try:
        data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return b"\0" not in data[:4096]


@router.patch("/harnesses/{name}")
async def configure_harness(request: Request, name: str, p: Principal = Depends(principal)) -> Any:
    body = await read_json(request)
    only(body, "settings", "secrets", "default_provider", "default_model")
    return await platform(request).harnesses.configure(p, name, settings=body.get("settings"), secrets=body.get("secrets"),
                                                       default_provider=body.get("default_provider"),
                                                       default_model=body.get("default_model"))


@router.delete("/harnesses/{name}")
async def uninstall_harness(request: Request, name: str, p: Principal = Depends(principal)) -> Any:
    await platform(request).harnesses.uninstall(p, name)
    return {"id": name, "object": "harness", "deleted": True}


# ── usage and traces ──────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/usage")
async def usage(request: Request, start: int | None = None, end: int | None = None, group_by: str | None = None,
                p: Principal = Depends(principal)) -> Any:
    now = datetime.now(UTC)
    s = datetime.fromtimestamp(start, UTC) if start else now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    e = datetime.fromtimestamp(end, UTC) if end else now
    return await platform(request).usage.summary(p, s, e, group_by)


@router.get("/traces/{rid}")
async def trace(request: Request, rid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).traces.get(p, rid)
