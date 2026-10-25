"""The organization's own resources: sign-up by invite, keys, members, providers, harnesses, usage and traces."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.datastructures import UploadFile

from ..errors import Invalid, NotFound
from ..services.principal import Principal
from .http import platform, principal, read_json

router = APIRouter(prefix="/v1")


def _str(body: dict[str, Any], key: str, default: str | None = None) -> str:
    value = body.get(key, default)
    if not isinstance(value, str):
        raise Invalid(f"{key} is a string", param=key)
    return value


@router.post("/signup")
async def signup(request: Request) -> Any:
    plat = platform(request)
    if plat.settings.signup != "invite":
        raise NotFound("sign-up is closed on this platform")
    body = await read_json(request)
    org, secret = await plat.redeem(_str(body, "invite_code"), _str(body, "organization"), _str(body, "email"))
    return {"organization": org, "api_key": secret}


@router.get("/organization")
async def organization(request: Request, p: Principal = Depends(principal)) -> Any:
    return {**await platform(request).tenancy.org(p.org_id), "member_id": p.member_id, "role": p.role}


# keys and members ───────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/keys")
async def list_keys(request: Request, p: Principal = Depends(principal)) -> Any:
    return {"object": "list", "data": await platform(request).tenancy.list_keys(p)}


@router.post("/keys")
async def create_key(request: Request, p: Principal = Depends(principal)) -> Any:
    body = await read_json(request)
    ttl = body.get("ttl_days")
    if ttl is not None and not isinstance(ttl, int):
        raise Invalid("ttl_days is an integer", param="ttl_days")
    return await platform(request).tenancy.mint_key(p, _str(body, "name", "key"), _str(body, "role", "member"),
                                                   body.get("member_id"), ttl)


@router.delete("/keys/{key_id}")
async def revoke_key(request: Request, key_id: str, p: Principal = Depends(principal)) -> Any:
    await platform(request).tenancy.revoke_key(p, key_id)
    return {"id": key_id, "object": "api_key", "deleted": True}


@router.get("/members")
async def list_members(request: Request, p: Principal = Depends(principal)) -> Any:
    return {"object": "list", "data": await platform(request).tenancy.list_members(p)}


@router.post("/members")
async def add_member(request: Request, p: Principal = Depends(principal)) -> Any:
    body = await read_json(request)
    return await platform(request).tenancy.add_member(p, _str(body, "email"), _str(body, "role", "member"))


@router.delete("/members/{member_id}")
async def remove_member(request: Request, member_id: str, p: Principal = Depends(principal)) -> Any:
    await platform(request).tenancy.remove_member(p, member_id)
    return {"id": member_id, "object": "member", "deleted": True}


# providers ──────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/providers")
async def list_providers(request: Request, p: Principal = Depends(principal)) -> Any:
    return {"object": "list", "data": await platform(request).providers.list(p)}


@router.put("/providers/{name}")
async def put_provider(request: Request, name: str, p: Principal = Depends(principal)) -> Any:
    body = await read_json(request)
    return await platform(request).providers.put(p, name, _str(body, "api"), _str(body, "base_url"), _str(body, "key"))


@router.get("/providers/{name}")
async def get_provider(request: Request, name: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).providers.get(p, name)


@router.delete("/providers/{name}")
async def delete_provider(request: Request, name: str, p: Principal = Depends(principal)) -> Any:
    await platform(request).providers.delete(p, name)
    return {"id": name, "object": "provider", "deleted": True}


# harnesses ──────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/catalog")
async def catalog(request: Request, p: Principal = Depends(principal)) -> Any:
    return {"object": "list", "data": await platform(request).packages.catalog()}


@router.get("/harnesses")
async def list_harnesses(request: Request, p: Principal = Depends(principal)) -> Any:
    return {"object": "list", "data": await platform(request).packages.list(p.org_id)}


@router.post("/harnesses")
async def install_harness(request: Request, p: Principal = Depends(principal)) -> Any:
    """Install or upgrade: {"builtin": name}, {"git": {"url", "ref", "path"}}, or a multipart `package` archive."""
    packages = platform(request).packages
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("package")
        if not isinstance(upload, UploadFile):
            raise Invalid("a multipart form with a `package` file part (a .tar.gz of the package)", param="package")
        return await packages.install_upload(p, await upload.read())
    body = await read_json(request)
    if "builtin" in body:
        p.require_admin()
        return await packages.install_builtin(p.org_id, _str(body, "builtin"), p.actor)
    git = body.get("git")
    if isinstance(git, dict):
        return await packages.install_git(p, _str(git, "url"), _str(git, "ref", "main"), _str(git, "path", "."))
    raise Invalid('install with {"builtin": name}, {"git": {"url": ..., "ref": ...}} or a multipart package upload')


@router.get("/harnesses/{name}")
async def get_harness(request: Request, name: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).packages.get(p.org_id, name)


@router.patch("/harnesses/{name}")
async def configure_harness(request: Request, name: str, p: Principal = Depends(principal)) -> Any:
    body = await read_json(request)
    settings = body.get("settings")
    if settings is not None and not (isinstance(settings, dict) and all(v is None or isinstance(v, str)
                                                                          for v in settings.values())):
        raise Invalid("settings maps names to strings (or null to clear)", param="settings")
    return await platform(request).packages.configure(p, name, settings=settings,
                                                      default_provider=body.get("default_provider"),
                                                      default_model=body.get("default_model"))


@router.delete("/harnesses/{name}")
async def uninstall_harness(request: Request, name: str, p: Principal = Depends(principal)) -> Any:
    await platform(request).packages.uninstall(p, name)
    return {"id": name, "object": "harness", "deleted": True}


# usage and traces ───────────────────────────────────────────────────────────────────────────────────────────────────
def window(start: int | None, end: int | None) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    s = datetime.fromtimestamp(start, UTC) if start else now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    e = datetime.fromtimestamp(end, UTC) if end else now
    return s, e


@router.get("/usage")
async def usage(request: Request, start: int | None = None, end: int | None = None,
                p: Principal = Depends(principal)) -> Any:
    s, e = window(start, end)
    return await platform(request).metering.usage(p.org_id, s, e)


@router.get("/traces/{rid}")
async def trace(request: Request, rid: str, p: Principal = Depends(principal)) -> Any:
    return await platform(request).traces.get(p, rid)
