"""The console's sign-in, under /auth. An account signs up with an email and a password, or
arrives from GitHub or Google; with mail configured a sign-up verifies its email first. A session
is a signed cookie naming the account as its member of one org; every member route accepts it
in place of an API key. Sign-up is open to anyone when HG_SIGNUP is open, and to invited emails
always. An account with no org gets one of its own, named after it."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from ..backends.identity import Unverified
from ..config import Settings
from ..domain.accounts import SESSION_TTL_S, Account, handle_of, normalize_email
from ..domain.members import Member
from ..errors import NotFound
from .auth import COOKIE

VERIFY_TTL_S, RESET_TTL_S, STATE_TTL_S = 86400, 3600, 600


class SignupBody(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginBody(BaseModel):
    email: str
    password: str


class EmailBody(BaseModel):
    email: str


class ResetBody(BaseModel):
    token: str
    password: str


class PasswordBody(BaseModel):
    password: str
    current: str = ""


class NameBody(BaseModel):
    name: str


class OrgBody(BaseModel):
    name: str


class SwitchBody(BaseModel):
    org: str


def router(service, settings: Settings) -> APIRouter:
    r = APIRouter(prefix="/auth")
    accounts, orgs, principals, mail, identities = service.accounts, service.orgs, service.principals, service.mail, service.identities
    secure = settings.public_url.startswith("https://")

    def set_cookie(response: Response, a: Account, org: str) -> None:
        response.set_cookie(COOKIE, accounts.session(a, org), max_age=SESSION_TTL_S, httponly=True, samesite="strict", secure=secure, path="/")

    def me_of(a: Account, m: Member) -> dict[str, Any]:
        t = orgs.get(m.org)
        return {"account": a.public(), "orgs": accounts.memberships(a),
                "current": {"org": t.name, "member": m.name, "role": m.role, "limits": t.limits, "org_status": t.status}}

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

    def may_sign_up(email: str) -> bool:
        return settings.signup == "open" or bool(accounts.invited(email))

    def send_verification(a: Account) -> None:
        link = f"{settings.public_url}/auth/verify?token={accounts.token('verify', a.id, VERIFY_TTL_S)}"
        mail.send(a.email, "Verify your HarnessGateway email", f"Hello {a.name},\n\nConfirm your email to finish signing up:\n\n{link}\n\nThe link is good for a day.\n")

    @r.get("/providers")
    def providers() -> dict:
        """What this platform's sign-in offers, for the app to draw."""
        return {"signup": settings.signup, "password": True, "mail": mail.enabled, "identities": sorted(identities),
                "github": "github" in identities, "google": "google" in identities}

    @r.post("/signup")
    def signup(body: SignupBody) -> Any:
        email = normalize_email(body.email)
        if not may_sign_up(email):
            raise HTTPException(403, "sign-up is closed on this platform; ask an owner for an invitation")
        a = accounts.create(email, body.name, body.password, verified=not mail.enabled)
        if mail.enabled:
            send_verification(a)
            return {"verify": True, "email": a.email}
        return signed_in(a, status=201)

    @r.post("/login")
    def login(body: LoginBody) -> Any:
        a = accounts.login(body.email, body.password)
        if a is None:
            raise HTTPException(401, "wrong email or password")
        if not a.verified:
            raise HTTPException(403, "verify your email first; we can send the link again")
        return signed_in(a)

    @r.post("/logout")
    def logout() -> Response:
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @r.get("/me")
    def me(who: tuple[Account, Member] = Depends(principals.account)) -> dict:
        return me_of(*who)

    @r.get("/verify")
    def verify(token: str) -> Response:
        claim = accounts.open_token(token, "verify")
        if not claim:
            raise HTTPException(400, "the verification link is not valid or has expired")
        a = accounts.mark_verified(accounts.by_id(claim["a"]))
        a, m = settle(a)
        response = RedirectResponse("/app", status_code=303)
        set_cookie(response, a, m.org)
        return response

    @r.post("/resend")
    def resend(body: EmailBody) -> dict:
        a = accounts.by_email(body.email)
        if a and not a.verified and mail.enabled:
            send_verification(a)
        return {"ok": True}

    @r.post("/reset")
    def reset(body: EmailBody) -> dict:
        a = accounts.by_email(body.email)
        if a and mail.enabled:
            link = f"{settings.public_url}/app#reset={accounts.token('reset', a.id, RESET_TTL_S)}"
            mail.send(a.email, "Reset your HarnessGateway password", f"Hello {a.name},\n\nSet a new password here:\n\n{link}\n\nThe link is good for an hour. If you did not ask for this, ignore it.\n")
        return {"ok": True}

    @r.post("/reset/confirm")
    def reset_confirm(body: ResetBody) -> Any:
        claim = accounts.open_token(body.token, "reset")
        if not claim:
            raise HTTPException(400, "the reset link is not valid or has expired")
        a = accounts.mark_verified(accounts.set_password(accounts.by_id(claim["a"]), body.password))
        return signed_in(a)

    @r.post("/password")
    def password(body: PasswordBody, who: tuple[Account, Member] = Depends(principals.account)) -> dict:
        a = who[0]
        if a.password and not accounts.login(a.email, body.current):
            raise HTTPException(403, "the current password is wrong")
        return accounts.set_password(a, body.password).public()

    @r.patch("/account")
    def rename(body: NameBody, who: tuple[Account, Member] = Depends(principals.account)) -> dict:
        return accounts.set_name(who[0], body.name).public()

    @r.post("/orgs")
    def create_org(body: OrgBody, who: tuple[Account, Member] = Depends(principals.account)) -> Any:
        accounts.create_org(who[0], body.name)
        return signed_in(accounts.by_id(who[0].id), body.name, status=201)

    @r.post("/switch")
    def switch(body: SwitchBody, who: tuple[Account, Member] = Depends(principals.account)) -> Any:
        return signed_in(who[0], body.org)

    # identity providers
    def configured(provider: str):
        if provider not in identities:
            raise HTTPException(404, f"sign-in with {provider} is not configured on this platform")
        return identities[provider]

    def callback_url(provider: str) -> str:
        return f"{settings.public_url}/auth/{provider}/callback"

    @r.get("/{provider}")
    def start(provider: str) -> Response:
        idp = configured(provider)
        state = accounts.token("state", "", STATE_TTL_S, v=provider)
        return RedirectResponse(idp.authorize_url(callback_url(provider), state), status_code=302)

    @r.get("/{provider}/callback")
    def callback(provider: str, code: str = "", state: str = "", error: str = "") -> Response:
        idp = configured(provider)
        claim = accounts.open_token(state, "state")
        if error or not code or not claim or claim.get("v") != provider:
            return RedirectResponse("/app#error=sign-in-cancelled", status_code=303)
        try:
            subject, email, name = idp.identity(code, callback_url(provider))
        except Unverified as e:
            raise HTTPException(403, str(e)) from None
        a = accounts.by_oauth(provider, subject)
        if a is None:
            a = accounts.by_email(email)
            if a is None:
                if not may_sign_up(email):
                    return RedirectResponse("/app#error=sign-up-closed", status_code=303)
                a = accounts.create(email, name, oauth={provider: subject}, verified=True)
            else:
                a = accounts.link_oauth(a, provider, subject)
        elif not a.verified:
            a = accounts.mark_verified(a)
        a, m = settle(a)
        response = RedirectResponse("/app", status_code=303)
        set_cookie(response, a, m.org)
        return response

    return r

