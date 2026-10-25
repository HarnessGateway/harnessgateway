"""Accounts, under /auth. An account signs up with an email and a password. A session is a signed
cookie naming the account as its member of one org; every member route accepts it in place of an
API key, so a browser client needs no key. Sign-up is open to anyone when HG_SIGNUP is open, and to invited emails always. An
account with no org gets one of its own, named after it."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import Settings
from ..domain.accounts import SESSION_TTL_S, Account, handle_of, normalize_email
from ..domain.members import Member
from ..errors import NotFound
from .auth import COOKIE


class SignupBody(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginBody(BaseModel):
    email: str
    password: str


class OrgBody(BaseModel):
    name: str


class SwitchBody(BaseModel):
    org: str


def router(service, settings: Settings) -> APIRouter:
    r = APIRouter(prefix="/auth")
    accounts, orgs, principals = service.accounts, service.orgs, service.principals
    secure = settings.public_url.startswith("https://")

    def set_cookie(response: Response, a: Account, org: str) -> None:
        response.set_cookie(COOKIE, accounts.session(a, org), max_age=SESSION_TTL_S, httponly=True, samesite="strict", secure=secure, path="/")

    def me_of(a: Account, m: Member) -> dict[str, Any]:
        t = orgs.get(m.org)
        return {"account": a.public(), "orgs": accounts.memberships(a),
                "current": {"org": t.name, "title": t.title, "member": m.name, "role": m.role, "limits": t.limits, "org_status": t.status}}

    def settle(a: Account, org: str = "") -> tuple[Account, Member]:
        """The account as a member somewhere: its invitations redeemed, an org of its own when it
        has none, and the org asked for or its first."""
        accounts.redeem_invites(a)
        a = accounts.by_id(a.id)
        if not accounts.memberships(a):
            accounts.create_org(a, accounts.free_org_name(handle_of(a.email)))
            a = accounts.by_id(a.id)
        if org:
            m = accounts.member_of(a, org)
            if m is None:
                raise NotFound(f"you are not a member of {org}")
        else:
            m = next((m for m in (accounts.member_of(a, x["org"]) for x in accounts.memberships(a)) if m), None)
        if m is None:
            raise HTTPException(403, "your org is suspended")
        return a, m

    def signed_in(a: Account, org: str = "", status: int = 200) -> JSONResponse:
        a, m = settle(a, org)
        response = JSONResponse(me_of(a, m), status_code=status)
        set_cookie(response, a, m.org)
        return response

    @r.get("/providers")
    def providers() -> dict:
        """What this platform's sign-in offers."""
        return {"signup": settings.signup, "password": True}

    @r.post("/signup")
    def signup(body: SignupBody) -> Any:
        email = normalize_email(body.email)
        if settings.signup != "open" and not accounts.invited(email):
            raise HTTPException(403, "sign-up is closed on this platform; ask an owner for an invitation")
        return signed_in(accounts.create(email, body.name, body.password), status=201)

    @r.post("/login")
    def login(body: LoginBody) -> Any:
        a = accounts.login(body.email, body.password)
        if a is None:
            raise HTTPException(401, "wrong email or password")
        return signed_in(a)

    @r.post("/logout")
    def logout() -> Response:
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @r.get("/me")
    def me(who: tuple[Account, Member] = Depends(principals.account)) -> dict:
        return me_of(*who)

    @r.post("/orgs")
    def create_org(body: OrgBody, who: tuple[Account, Member] = Depends(principals.account)) -> Any:
        accounts.create_org(who[0], body.name)
        return signed_in(accounts.by_id(who[0].id), body.name, status=201)

    @r.post("/switch")
    def switch(body: SwitchBody, who: tuple[Account, Member] = Depends(principals.account)) -> Any:
        return signed_in(who[0], body.org)

    return r
