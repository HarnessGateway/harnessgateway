"""Identity (SPEC §10, §14.2): signing in with a password or through SSO, one's own account, the organization's
accounts and identity provider, and the audit log. Self-hosted has one organization: the SSO routes that run before
anyone is signed in act for it."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from ..domain.access import Principal
from ..errors import Forbidden, Invalid, NotFound
from .http import admin, client, only, platform, principal, read_json, signed_in

router = APIRouter(prefix="/v1")


async def _the_org(request: Request) -> str:
    org = await platform(request).orgs.first()
    if org is None:
        raise NotFound("the platform is not initialized (harnessgateway init)")
    return str(org["id"])


# ── signing in ────────────────────────────────────────────────────────────────────────────────────────────────────
@router.post("/auth/login")
async def login(request: Request) -> Any:
    body = await read_json(request)
    only(body, "email", "password")
    plat = platform(request)
    sso = await plat.sso.status(await _the_org(request))
    if sso["required"]:
        found = await plat.accounts.login(body.get("email"), body.get("password"), client(request))
        if not found["account"]["platform_admin"]:
            await plat.accounts.logout(found["token"])
            raise Forbidden("sign in through your organization's identity provider")
        return found
    return await plat.accounts.login(body.get("email"), body.get("password"), client(request))


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
    """The account, its memberships, and its organization and role in it. What a console reads first."""
    plat, account = platform(request), s["account"]
    doc: dict[str, Any] = {"object": "me", "account": account, "memberships": s["memberships"],
                           "platform": plat.operations.platform()}
    if s["memberships"]:
        org_id, role = s["memberships"][0]["org_id"], s["memberships"][0]["role"]
        doc["organization"] = await plat.orgs.get(org_id)
        doc["role"] = role
    return doc


@router.post("/auth/password")
async def password(request: Request, s: dict[str, Any] = Depends(signed_in)) -> Any:
    body = await read_json(request)
    only(body, "current", "new")
    await platform(request).accounts.change_password(s["account"]["id"], body.get("current"), body.get("new"), s["token"])
    return {"object": "account", "id": s["account"]["id"], "password": "changed"}


# ── SSO ───────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/auth/sso")
async def sso_status(request: Request) -> Any:
    """Whether SSO is configured, its display name, whether passwords are allowed: the sign-in page reads it."""
    return await platform(request).sso.status(await _the_org(request))


@router.get("/auth/sso/start")
async def sso_start(request: Request, redirect: str = "") -> Any:
    """Off to the identity provider; it returns to the console's /sso/callback, which posts to /exchange."""
    url = await platform(request).sso.start(await _the_org(request), redirect)
    return RedirectResponse(url, status_code=302)


@router.post("/auth/sso/exchange")
async def sso_exchange(request: Request) -> Any:
    body = await read_json(request)
    only(body, "code", "state")
    return await platform(request).sso.exchange(body.get("code"), body.get("state"), client(request))


@router.get("/sso")
async def get_sso(request: Request, p: Principal = Depends(admin)) -> Any:
    found = await platform(request).sso.get(p.org_id)
    if found is None:
        raise NotFound("no identity provider is configured")
    return found


@router.put("/sso")
async def put_sso(request: Request, p: Principal = Depends(admin)) -> Any:
    return await platform(request).sso.put(p.org_id, p.actor, await read_json(request))


@router.delete("/sso")
async def delete_sso(request: Request, p: Principal = Depends(admin)) -> Any:
    await platform(request).sso.delete(p.org_id, p.actor)
    return {"object": "identity_provider", "deleted": True}


# ── accounts ──────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/accounts")
async def list_accounts(request: Request, p: Principal = Depends(admin)) -> Any:
    return {"object": "list", "data": await platform(request).accounts.list(p.org_id)}


@router.post("/accounts")
async def create_account(request: Request, p: Principal = Depends(admin)) -> Any:
    """{email, name, role, platform_admin?} → the account and its invitation link, once (mailed too when mail is set)."""
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


# ── audit ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/audit")
async def audit(request: Request, limit: int = 50, after: str | None = None, p: Principal = Depends(admin)) -> Any:
    return await platform(request).audit.list(p.org_id, limit, after)
