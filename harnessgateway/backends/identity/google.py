"""Sign in with Google: OpenID Connect userinfo, the email only when Google says it is verified."""
from __future__ import annotations

import httpx

from . import Unverified
from .oauth import CodeExchange

AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"


class Google:
    name = "google"

    def __init__(self, client_id: str, client_secret: str, transport: httpx.BaseTransport | None = None):
        self._x = CodeExchange(client_id, client_secret, transport)

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        return self._x.authorize(AUTHORIZE, redirect_uri, state, "openid email profile", response_type="code", access_type="online", prompt="select_account")

    def identity(self, code: str, redirect_uri: str) -> tuple[str, str, str]:
        u = self._x.get(USERINFO, self._x.access_token(TOKEN, code, redirect_uri))
        if not u.get("email_verified"):
            raise Unverified("your Google account's email is not verified")
        return str(u["sub"]), u["email"], u.get("name") or ""
