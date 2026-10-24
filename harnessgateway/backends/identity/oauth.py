"""The OAuth 2 authorization-code exchange the providers share."""
from __future__ import annotations

from urllib.parse import urlencode

import httpx


class CodeExchange:
    def __init__(self, client_id: str, client_secret: str, transport: httpx.BaseTransport | None = None):
        self.client_id, self.client_secret = client_id, client_secret
        self._http = httpx.Client(timeout=20, transport=transport)

    def authorize(self, url: str, redirect_uri: str, state: str, scope: str, **extra: str) -> str:
        return f"{url}?{urlencode({'client_id': self.client_id, 'redirect_uri': redirect_uri, 'scope': scope, 'state': state, **extra})}"

    def access_token(self, url: str, code: str, redirect_uri: str) -> str:
        data = {"client_id": self.client_id, "client_secret": self.client_secret, "code": code, "redirect_uri": redirect_uri, "grant_type": "authorization_code"}
        r = self._http.post(url, data=data, headers={"Accept": "application/json"})
        r.raise_for_status()
        token = r.json().get("access_token", "")
        if not token:
            raise RuntimeError("the identity provider returned no token")
        return token

    def get(self, url: str, token: str) -> dict:
        r = self._http.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        r.raise_for_status()
        return r.json()
