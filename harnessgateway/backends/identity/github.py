"""Sign in with GitHub: the account's primary, verified email."""
from __future__ import annotations

import httpx

from . import Unverified
from .oauth import CodeExchange

AUTHORIZE = "https://github.com/login/oauth/authorize"
TOKEN = "https://github.com/login/oauth/access_token"
USER = "https://api.github.com/user"
EMAILS = "https://api.github.com/user/emails"


class GitHub:
    name = "github"

    def __init__(self, client_id: str, client_secret: str, transport: httpx.BaseTransport | None = None):
        self._x = CodeExchange(client_id, client_secret, transport)

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        return self._x.authorize(AUTHORIZE, redirect_uri, state, "read:user user:email")

    def identity(self, code: str, redirect_uri: str) -> tuple[str, str, str]:
        token = self._x.access_token(TOKEN, code, redirect_uri)
        u = self._x.get(USER, token)
        primary = next((e["email"] for e in self._x.get(EMAILS, token) if e.get("primary") and e.get("verified")), "")
        if not primary:
            raise Unverified("your GitHub account has no verified primary email")
        return str(u["id"]), primary, u.get("name") or u.get("login") or ""
