"""Identity and projects (SPEC §9, §14.2): signing in, one's own account, the organization's accounts and projects,
and the audit log."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from ..domain.access import ADMIN, Principal
from ..errors import Forbidden, Invalid
from .http import admin, client, only, platform, principal, read_json, signed_in

router = APIRouter(prefix="/v1")


# ── signing in ────────────────────────────────────────────────────────────────────────────────────────────────────
@router.post("/auth/login")
async def login(request: Request) -> Any:
    body = await read_json(request)
    only(body, "email", "password")
    return await platform(request).accounts.login(body.get("email"), body.get("password"), client(request))


@router.post("/auth/accept")
async def accept(request: Request) -> Any:
    """Activate an invited account, or finish a password reset: the one-time token and the new password."""
    body = await read_json(request)
    only(body, "token", "password")
    return await platform(request).accounts.accept(body.get("token"), body.get("password"), client(request))


@router.post("/auth/logout")
async def logout(request: Request, s: dict[str, Any] = Depends(signed_in)) -> Any:
    await platform(request).accounts.logout(s["token"])
    return {"object": "sign_in", "deleted": True}


@router.get("/auth/me")
async def me(request: Request, s: dict[str, Any] = Depends(signed_in)) -> Any:
    """The account, its memberships, and in its organization: the organization, its role and the projects it may
    address (every project for an admin). What a console reads first."""
    plat, account = platform(request), s["account"]
    doc: dict[str, Any] = {"object": "me", "account": account, "memberships": s["memberships"],
                           "platform": plat.operations.platform()}
    if s["memberships"]:
        org_id, role = s["memberships"][0]["org_id"], s["memberships"][0]["role"]
        doc["organization"] = await plat.orgs.get(org_id)
        doc["role"] = role
        doc["projects"] = await plat.projects.list(org_id, None if role == ADMIN else account["id"])
    return doc


@router.post("/auth/password")
async def password(request: Request, s: dict[str, Any] = Depends(signed_in)) -> Any:
    body = await read_json(request)
    only(body, "current", "new")
    await platform(request).accounts.change_password(s["account"]["id"], body.get("current"), body.get("new"), s["token"])
    return {"object": "account", "id": s["account"]["id"], "password": "changed"}


# ── accounts ──────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/accounts")
async def list_accounts(request: Request, p: Principal = Depends(admin)) -> Any:
    return {"object": "list", "data": await platform(request).accounts.list(p.org_id)}


@router.post("/accounts")
async def create_account(request: Request, p: Principal = Depends(admin)) -> Any:
    """{email, name, role, platform_admin?} → the account and its invitation token, once."""
    body = await read_json(request)
    only(body, "email", "name", "role", "platform_admin")
    if body.get("platform_admin") and not p.platform_admin:
        raise Forbidden("only a platform admin grants the platform scope")
    return await platform(request).accounts.create(p.org_id, p.actor, body.get("email"), body.get("name"),
                                                   body.get("role", "developer"), bool(body.get("platform_admin")))


@router.get("/accounts/{account_id}")
async def get_account(request: Request, account_id: str, p: Principal = Depends(principal)) -> Any:
    if p.account_id != account_id:
        p.require("accounts", "read")
    return await platform(request).accounts.get(p.org_id, account_id)


@router.patch("/accounts/{account_id}")
async def update_account(request: Request, account_id: str, p: Principal = Depends(admin)) -> Any:
    body = await read_json(request)
    only(body, "role", "status", "platform_admin")
    if not body:
        raise Invalid("give role, status or platform_admin")
    return await platform(request).accounts.update(p.org_id, p.actor, account_id, role=body.get("role"),
                                                   status=body.get("status"), platform_admin=body.get("platform_admin"),
                                                   by_platform_admin=p.platform_admin)


@router.post("/accounts/{account_id}/reset")
async def reset_account(request: Request, account_id: str, p: Principal = Depends(admin)) -> Any:
    return await platform(request).accounts.reset(p.org_id, p.actor, account_id)


# ── projects ──────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/projects")
async def list_projects(request: Request, p: Principal = Depends(principal)) -> Any:
    p.require("projects", "read")
    return {"object": "list", "data": await platform(request).projects.list(p.org_id, None if p.role == ADMIN else p.account_id)}


@router.post("/projects")
async def create_project(request: Request, p: Principal = Depends(admin)) -> Any:
    body = await read_json(request)
    only(body, "name")
    return await platform(request).projects.create(p.org_id, p.actor, body.get("name"))


@router.get("/projects/{project_id}")
async def get_project(request: Request, project_id: str, p: Principal = Depends(principal)) -> Any:
    p.require("projects", "read")
    plat = platform(request)
    if p.role != ADMIN and not (p.account_id and await plat.projects.is_member(p.org_id, project_id, p.account_id)):
        raise Forbidden("you are not a developer of this project")
    return await plat.projects.get(p.org_id, project_id)


@router.patch("/projects/{project_id}")
async def update_project(request: Request, project_id: str, p: Principal = Depends(admin)) -> Any:
    body = await read_json(request)
    only(body, "name")
    return await platform(request).projects.update(p.org_id, p.actor, project_id, body.get("name"))


@router.delete("/projects/{project_id}")
async def delete_project(request: Request, project_id: str, p: Principal = Depends(admin)) -> Any:
    await platform(request).projects.delete(p.org_id, p.actor, project_id)
    return {"id": project_id, "object": "project", "deleted": True}


@router.get("/projects/{project_id}/members")
async def members(request: Request, project_id: str, p: Principal = Depends(admin)) -> Any:
    return {"object": "list", "data": await platform(request).projects.members(p.org_id, project_id)}


@router.put("/projects/{project_id}/members/{account_id}")
async def add_member(request: Request, project_id: str, account_id: str, p: Principal = Depends(admin)) -> Any:
    await platform(request).projects.add_member(p.org_id, p.actor, project_id, account_id)
    return {"object": "project_member", "project": project_id, "account": account_id}


@router.delete("/projects/{project_id}/members/{account_id}")
async def remove_member(request: Request, project_id: str, account_id: str, p: Principal = Depends(admin)) -> Any:
    await platform(request).projects.remove_member(p.org_id, p.actor, project_id, account_id)
    return {"object": "project_member", "project": project_id, "account": account_id, "deleted": True}


# ── audit ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/audit")
async def audit(request: Request, limit: int = 50, after: str | None = None, p: Principal = Depends(admin)) -> Any:
    return await platform(request).audit.list(p.org_id, limit, after)
