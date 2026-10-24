"""Sign in with any OpenID Connect provider, by its issuer: the discovery document names the
endpoints; userinfo gives the subject, the verified email and the name. This is how an organization
brings its own single sign-on (Okta, Entra ID, Keycloak, Auth0, Dex, …)."""
from __future__ import annotations

import httpx

from . import Unverified
from .oauth import CodeExchange


class Oidc:
    def __init__(self, name: str, issuer: str, client_id: str, client_secret: str, transport: httpx.BaseTransport | None = None):
        self.name, self.issuer = name, issuer.rstrip("/")
        self._x = CodeExchange(client_id, client_secret, transport)
        self._http = httpx.Client(timeout=20, transport=transport)
        self._doc: dict | None = None

    def discovery(self) -> dict:
        if self._doc is None:
            r = self._http.get(f"{self.issuer}/.well-known/openid-configuration")
            r.raise_for_status()
            doc = r.json()
            for key in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint"):
                if not isinstance(doc.get(key), str):
                    raise RuntimeError(f"the issuer's discovery document has no {key}")
            self._doc = doc
        return self._doc

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        return self._x.authorize(self.discovery()["authorization_endpoint"], redirect_uri, state, "openid email profile", response_type="code")

    def identity(self, code: str, redirect_uri: str) -> tuple[str, str, str]:
        doc = self.discovery()
        u = self._x.get(doc["userinfo_endpoint"], self._x.access_token(doc["token_endpoint"], code, redirect_uri))
        if not u.get("email") or not u.get("email_verified", False):
            raise Unverified(f"{self.name} did not vouch for the account's email")
        return str(u["sub"]), u["email"], u.get("name") or u.get("preferred_username") or ""
