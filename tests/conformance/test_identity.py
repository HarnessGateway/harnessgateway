"""Conformance: every identity provider turns a code into (subject, verified email, name), refuses an
account whose email the provider does not vouch for, and builds an authorization URL carrying the
state. GitHub and Google as presets; any OpenID Connect issuer through discovery."""
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from harnessgateway.backends.identity import Unverified, open_identities
from harnessgateway.backends.identity.github import GitHub
from harnessgateway.backends.identity.google import Google
from harnessgateway.backends.identity.oidc import Oidc

ISSUER = "https://sso.acme.test"


def fake(verified: bool):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"authorization_endpoint": f"{ISSUER}/authorize", "token_endpoint": f"{ISSUER}/token", "userinfo_endpoint": f"{ISSUER}/userinfo"})
        if request.method == "POST":                                        # every token endpoint
            body = dict(parse_qs(request.content.decode()))
            assert body["grant_type"] == ["authorization_code"] and body["code"] == ["c0de"]
            return httpx.Response(200, json={"access_token": "tok"})
        assert request.headers["authorization"] == "Bearer tok"
        if url.endswith("/user/emails"):
            return httpx.Response(200, json=[{"email": "chase@acme.test", "primary": True, "verified": verified}])
        if url.endswith("api.github.com/user"):
            return httpx.Response(200, json={"id": 42, "login": "chase", "name": "Chase"})
        return httpx.Response(200, json={"sub": "sub-1", "email": "chase@acme.test", "email_verified": verified, "name": "Chase"})
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("make", [lambda t: GitHub("id", "secret", t), lambda t: Google("id", "secret", t), lambda t: Oidc("sso", ISSUER, "id", "secret", t)])
def test_identity_and_refusal(make):
    idp = make(fake(True))
    url = idp.authorize_url("https://hg.test/auth/x/callback", "st4te")
    q = parse_qs(urlsplit(url).query)
    assert q["state"] == ["st4te"] and q["client_id"] == ["id"] and q["redirect_uri"] == ["https://hg.test/auth/x/callback"]
    subject, email, name = idp.identity("c0de", "https://hg.test/auth/x/callback")
    assert subject in ("42", "sub-1") and email == "chase@acme.test" and name == "Chase"
    with pytest.raises(Unverified):
        make(fake(False)).identity("c0de", "https://hg.test/auth/x/callback")


def test_open_identities_by_settings():
    assert open_identities({}) == {}
    found = open_identities({"HG_GITHUB_CLIENT_ID": "g", "HG_OIDC_ISSUER": ISSUER, "HG_OIDC_NAME": "okta"})
    assert set(found) == {"github", "okta"} and found["okta"].name == "okta"
