"""SSO (SPEC §10.3): one OpenID Connect identity provider per organization. The api is the OIDC client: it starts the
flow with state, nonce and PKCE kept in Redis for ten minutes, the console receives the provider's redirect and posts
the code back, and the api exchanges it, verifies the ID token (signature by the provider's JWKS, issuer, audience,
nonce, expiry), maps the claims and signs the person in through the accounts service."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose import errors as jose_errors

from ..domain.access import ROLES
from ..errors import Invalid, NotFound, Unauthenticated
from ..netguard import check_url
from ..store.db import Database
from ..store.moments import Moments
from .accounts import Accounts
from .audit import Audit
from .pages import ts
from .vault import Vault

DISCOVERY_CACHE_S = 3600
SCOPES = "openid email profile"


def secret_context() -> str:
    return "sso/client_secret"


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {"object": "identity_provider", "name": row["name"], "issuer": row["issuer"], "client_id": row["client_id"],
            "domains": row["domains"], "auto_provision": row["auto_provision"], "default_role": row["default_role"],
            "role_claim": row["role_claim"], "admin_values": row["admin_values"], "required": row["required"],
            "created_at": ts(row["created_at"]), "updated_at": ts(row["updated_at"])}


class Sso:
    def __init__(self, db: Database, moments: Moments, vault: Vault, accounts: Accounts, audit: Audit,
                 console_url: str, *, allow_private: bool):
        self.db, self.moments, self.vault, self.accounts, self.audit = db, moments, vault, accounts, audit
        self.console_url, self.allow_private = console_url, allow_private
        self._discovery: dict[str, tuple[float, dict[str, Any]]] = {}
        self._jwks: dict[str, tuple[float, Any]] = {}
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(15, connect=10), trust_env=True)

    async def close(self) -> None:
        await self.http.aclose()

    # ── configuration ─────────────────────────────────────────────────────────────────────────────────────────────
    async def get(self, org_id: str) -> dict[str, Any] | None:
        async with self.db.tenant(org_id) as tx:
            row = await tx.one("SELECT * FROM identity_providers WHERE org_id = %s", org_id)
        return _view(row) if row else None

    async def status(self, org_id: str) -> dict[str, Any]:
        """What the sign-in page needs: whether SSO exists, its display name, whether passwords are allowed."""
        row = await self.get(org_id)
        return {"object": "sso", "enabled": row is not None, "name": row["name"] if row else "",
                "required": bool(row and row["required"])}

    async def put(self, org_id: str, actor: str, doc: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(doc) - {"name", "issuer", "client_id", "client_secret", "domains", "auto_provision",
                                     "default_role", "role_claim", "admin_values", "required"})
        if unknown:
            raise Invalid(f"an identity provider has name, issuer, client_id, client_secret, domains, auto_provision, "
                          f"default_role, role_claim, admin_values and required, not {', '.join(unknown)}", param=unknown[0])
        issuer = await asyncio.to_thread(check_url, doc.get("issuer"), allow_private=self.allow_private, param="issuer")
        client_id = doc.get("client_id")
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 512:
            raise Invalid("client_id is the client registered at the provider", param="client_id")
        domains = doc.get("domains", [])
        if not isinstance(domains, list) or not all(isinstance(d, str) and "." in d and "@" not in d for d in domains):
            raise Invalid("domains is a list of email domains (example.com)", param="domains")
        role = doc.get("default_role", "developer")
        if role not in ROLES:
            raise Invalid(f"default_role is one of {', '.join(ROLES)}", param="default_role")
        claim, admins = doc.get("role_claim", ""), doc.get("admin_values", [])
        if not isinstance(claim, str) or len(claim) > 100 or not isinstance(admins, list) or \
                not all(isinstance(v, str) for v in admins):
            raise Invalid("role_claim names a claim and admin_values lists the values that grant admin", param="role_claim")
        await self.discover(issuer)                                  # the issuer answers, before anything is written
        async with self.db.tenant(org_id) as tx:
            current = await tx.one("SELECT sealed_secret FROM identity_providers WHERE org_id = %s", org_id)
            secret = doc.get("client_secret")
            if isinstance(secret, str) and secret:
                sealed = await self.vault.seal(org_id, secret_context(), secret)
            elif current:
                sealed = current["sealed_secret"]
            else:
                raise Invalid("client_secret is the secret registered at the provider", param="client_secret")
            await tx.run("INSERT INTO identity_providers (org_id, name, issuer, client_id, sealed_secret, domains, "
                         "auto_provision, default_role, role_claim, admin_values, required) VALUES (%s, %s, %s, %s, %s, "
                         "%s::jsonb, %s, %s, %s, %s::jsonb, %s) ON CONFLICT (org_id) DO UPDATE SET name = EXCLUDED.name, "
                         "issuer = EXCLUDED.issuer, client_id = EXCLUDED.client_id, sealed_secret = EXCLUDED.sealed_secret, "
                         "domains = EXCLUDED.domains, auto_provision = EXCLUDED.auto_provision, default_role = "
                         "EXCLUDED.default_role, role_claim = EXCLUDED.role_claim, admin_values = EXCLUDED.admin_values, "
                         "required = EXCLUDED.required, updated_at = now()",
                         org_id, str(doc.get("name", ""))[:100], issuer, client_id, sealed, _json(domains),
                         bool(doc.get("auto_provision", True)), role, claim, _json(admins), bool(doc.get("required", False)))
            row = await tx.get("SELECT * FROM identity_providers WHERE org_id = %s", org_id)
        await self.audit.write(org_id, actor, "sso.put", issuer, {"client_id": client_id, "domains": domains,
                                                                    "required": row["required"]})
        return _view(row)

    async def delete(self, org_id: str, actor: str) -> None:
        async with self.db.tenant(org_id) as tx:
            if not await tx.run("DELETE FROM identity_providers WHERE org_id = %s", org_id):
                raise NotFound("no identity provider is configured")
        await self.audit.write(org_id, actor, "sso.delete")

    # ── the flow ──────────────────────────────────────────────────────────────────────────────────────────────────
    async def discover(self, issuer: str) -> dict[str, Any]:
        hit = self._discovery.get(issuer)
        if hit and time.monotonic() - hit[0] < DISCOVERY_CACHE_S:
            return hit[1]
        try:
            r = await self.http.get(issuer + "/.well-known/openid-configuration")
            r.raise_for_status()
            doc = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise Invalid(f"the issuer's discovery document could not be read: {type(e).__name__}", param="issuer") from None
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
            if not isinstance(doc.get(key), str):
                raise Invalid(f"the issuer's discovery document lacks {key}", param="issuer")
        self._discovery[issuer] = (time.monotonic(), doc)
        return doc

    async def _keys(self, jwks_uri: str) -> Any:
        hit = self._jwks.get(jwks_uri)
        if hit and time.monotonic() - hit[0] < DISCOVERY_CACHE_S:
            return hit[1]
        r = await self.http.get(jwks_uri)
        r.raise_for_status()
        keys = JsonWebKey.import_key_set(r.json())
        self._jwks[jwks_uri] = (time.monotonic(), keys)
        return keys

    async def start(self, org_id: str, redirect: str) -> str:
        """The provider's authorization URL for a sign-in that will return to the console."""
        if not redirect.startswith(self.console_url + "/"):
            raise Invalid("redirect is a page of the console", param="redirect")
        async with self.db.tenant(org_id) as tx:
            row = await tx.one("SELECT * FROM identity_providers WHERE org_id = %s", org_id)
        if not row:
            raise NotFound("SSO is not configured")
        conf = await self.discover(row["issuer"])
        state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        await self.moments.sso_put(state, {"org_id": org_id, "nonce": nonce, "verifier": verifier, "redirect": redirect})
        params = {"response_type": "code", "client_id": row["client_id"], "redirect_uri": self.console_url + "/sso/callback",
                  "scope": SCOPES, "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
        return f"{conf['authorization_endpoint']}?{urlencode(params)}"

    async def exchange(self, code: object, state: object, client: str) -> dict[str, Any]:
        """The provider's code and state, back from the console: a sign-in, and where the console goes next."""
        if not isinstance(code, str) or not isinstance(state, str) or not code or not state:
            raise Invalid("code and state come from the provider's redirect", param="state")
        flight = await self.moments.sso_take(state)
        if flight is None:
            raise Unauthenticated("the sign-in expired or was already used; start again")
        org_id = flight["org_id"]
        async with self.db.tenant(org_id) as tx:
            row = await tx.one("SELECT * FROM identity_providers WHERE org_id = %s", org_id)
        if not row:
            raise NotFound("SSO is not configured")
        conf = await self.discover(row["issuer"])
        secret = await self.vault.open(org_id, secret_context(), row["sealed_secret"])
        try:
            r = await self.http.post(conf["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": self.console_url + "/sso/callback",
                "client_id": row["client_id"], "client_secret": secret, "code_verifier": flight["verifier"]},
                headers={"Accept": "application/json"})
            tokens = r.json()
        except (httpx.HTTPError, ValueError):
            raise Unauthenticated("the provider did not answer the code exchange") from None
        if r.status_code != 200 or not isinstance(tokens.get("id_token"), str):
            why = str(tokens.get("error_description") or tokens.get("error") or r.status_code)[:200]
            raise Unauthenticated(f"the provider refused the code: {why}")
        claims = await self._verify(tokens["id_token"], conf, row["client_id"], flight["nonce"])
        email = str(claims.get("email") or "")
        if not email or not claims.get("email_verified", True):
            raise Unauthenticated("the provider did not vouch for an email address")
        domain = email.rsplit("@", 1)[-1].lower()
        if row["domains"] and domain not in [d.lower() for d in row["domains"]]:
            raise Unauthenticated(f"{domain} is not a domain this organization signs in with")
        role = self._role(claims, row["role_claim"], row["admin_values"])
        signed = await self.accounts.sso_sign_in(org_id, subject=str(claims["sub"]), email=email,
                                                 name=str(claims.get("name") or ""), role=role,
                                                 auto_provision=row["auto_provision"], default_role=row["default_role"],
                                                 client=client)
        return {**signed, "redirect": flight["redirect"]}

    async def _verify(self, id_token: str, conf: dict[str, Any], client_id: str, nonce: str) -> dict[str, Any]:
        keys = await self._keys(conf["jwks_uri"])
        try:
            claims = JsonWebToken(["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256"]).decode(
                id_token, keys, claims_options={"iss": {"essential": True, "value": conf["issuer"]},
                                                 "aud": {"essential": True, "value": client_id},
                                                 "nonce": {"essential": True, "value": nonce},
                                                 "sub": {"essential": True}})
            claims.validate(leeway=60)
        except (jose_errors.JoseError, ValueError) as e:
            raise Unauthenticated(f"the ID token does not verify: {type(e).__name__}") from None
        return dict(claims)

    @staticmethod
    def _role(claims: dict[str, Any], claim: str, admin_values: list[str]) -> str | None:
        """The role the claims grant: admin when the role claim holds an admin value, developer when the claim is
        configured and holds none, None when no claim is configured (the membership keeps its role)."""
        if not claim:
            return None
        value = claims.get(claim)
        values = value if isinstance(value, list) else [value] if value is not None else []
        return "admin" if any(str(v) in admin_values for v in values) else "developer"


def _json(value: Any) -> str:
    return json.dumps(value)

