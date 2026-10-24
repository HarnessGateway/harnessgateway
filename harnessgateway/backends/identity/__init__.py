"""Backend: who an account is, by an identity provider. Each provider turns an authorization code
into (subject, verified email, name); the sign-in does the rest. `github` and `google` are presets;
`oidc` is any OpenID Connect provider by its issuer URL (Okta, Entra, Keycloak, Auth0, …), which is
how an organization brings its own single sign-on."""
from __future__ import annotations

from typing import Mapping, Protocol

import httpx


class IdentityProvider(Protocol):
    name: str

    def authorize_url(self, redirect_uri: str, state: str) -> str: ...
    def identity(self, code: str, redirect_uri: str) -> tuple[str, str, str]:
        """(subject, verified email, display name) for the code the provider sent back; raises
        `Unverified` when the provider does not vouch for the email."""
        ...


class Unverified(Exception):
    """The provider has no verified email for the account."""


def open_identities(env: Mapping[str, str], transport: httpx.BaseTransport | None = None) -> dict[str, IdentityProvider]:
    """The providers the deployer configured, by name."""
    found: dict[str, IdentityProvider] = {}
    if env.get("HG_GITHUB_CLIENT_ID"):
        from .github import GitHub
        found["github"] = GitHub(env["HG_GITHUB_CLIENT_ID"], env.get("HG_GITHUB_CLIENT_SECRET", ""), transport)
    if env.get("HG_GOOGLE_CLIENT_ID"):
        from .google import Google
        found["google"] = Google(env["HG_GOOGLE_CLIENT_ID"], env.get("HG_GOOGLE_CLIENT_SECRET", ""), transport)
    if env.get("HG_OIDC_ISSUER"):
        from .oidc import Oidc
        name = env.get("HG_OIDC_NAME", "sso")
        found[name] = Oidc(name, env["HG_OIDC_ISSUER"], env.get("HG_OIDC_CLIENT_ID", ""), env.get("HG_OIDC_CLIENT_SECRET", ""), transport)
    return found
