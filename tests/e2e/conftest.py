"""The console in a real browser, on a real API: the processes a deploy runs, started from this checkout on a scratch
database and Redis (tests/conftest.py) with the file object store, and Playwright's Chromium driving the pages. Needs
HG_TEST_DATABASE_URL and Playwright's Chromium; skips without either. No worker runs: nothing here needs a box. The
console is started with a pages directory (tests/e2e/pages), as a deployment with pages of its own starts it."""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
EMAIL = "deployer@example.test"
START_S = 60
CHROMIUM = Path(os.environ.get("HG_TEST_CHROMIUM", "/opt/pw-browsers/chromium"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for(url: str) -> None:
    deadline = time.monotonic() + START_S
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"{url} did not come up in {START_S}s")


@dataclass(frozen=True)
class Stack:
    api: str
    console: str
    invitation: str                     # the first admin's setup link, as `init` printed it
    admin_key: str
    email: str = EMAIL


@pytest.fixture(scope="session")
def stack(scratch_database, redis_url, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    api_port, console_port = free_port(), free_port()
    api, console = f"http://127.0.0.1:{api_port}", f"http://127.0.0.1:{console_port}"
    blobs, state = tmp_path_factory.mktemp("blobs"), tmp_path_factory.mktemp("console")
    env = {**os.environ, "HG_DATABASE_URL": scratch_database(), "HG_REDIS_URL": redis_url, "HG_BLOB_URL": f"file://{blobs}",
           "HG_SECRET_KEY": base64.b64encode(b"e2e-secret-key-of-thirty-two-bytes").decode(), "HG_DOOR_KEY": "hgd_e2e",
           "HG_OPERATOR_KEY": "hgo_e2e", "HG_HOST": "127.0.0.1", "HG_PORT": str(api_port), "HG_API_URL": api,
           "HG_CONSOLE_URL": console, "HG_LOG_LEVEL": "warning",
           "HG_ALLOW_PRIVATE_EGRESS": "1"}                      # the fake identity provider below listens on 127.0.0.1
    procs = [subprocess.Popen([sys.executable, "-m", "harnessgateway", "api"], env=env, cwd=ROOT)]
    try:
        wait_for(f"{api}/readyz")
        out = subprocess.run([sys.executable, "-m", "harnessgateway", "init", "--admin-email", EMAIL], env=env, cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
        procs.append(subprocess.Popen([sys.executable, str(ROOT / "console" / "server.py"), "--api", api, "--host", "127.0.0.1",
                                       "--port", str(console_port), "--state", str(state),
                                       "--pages", str(ROOT / "tests" / "e2e" / "pages")], cwd=ROOT))
        wait_for(f"{console}/login")
        doc = json.loads(out)
        yield Stack(api, console, doc["invitation"], doc["admin_key"])
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=15)


@pytest.fixture(scope="session")
def browser():
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        pytest.skip("needs the playwright package")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(**({"executable_path": str(CHROMIUM)} if CHROMIUM.is_file() else {}))
        except Error:
            pytest.skip("needs Playwright's Chromium (playwright install chromium, or HG_TEST_CHROMIUM)")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    page = context.new_page()
    yield page
    context.close()


@dataclass
class Idp:
    """A fake OpenID Connect provider: discovery, keys, an authorize page that signs the person in at once, and a token
    endpoint that checks the client secret and the PKCE verifier and returns a signed ID token with the nonce."""
    url: str
    client_id: str = "hg-console"
    client_secret: str = "fake-idp-client-secret"
    email: str = "sso.person@sso.example.test"
    name: str = "Sso Person"


@pytest.fixture(scope="session")
def idp() -> Iterator[Idp]:
    import hashlib
    import threading
    import uuid
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlencode, urlsplit

    from authlib.jose import JsonWebKey, JsonWebToken
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    jwks = {"keys": [JsonWebKey.import_key(public, {"kty": "RSA", "kid": "k1", "use": "sig", "alg": "RS256"}).as_dict()]}
    port = free_port()
    me = Idp(f"http://127.0.0.1:{port}")
    codes: dict[str, dict[str, str]] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def reply(self, status: int, doc: object, headers: dict[str, str] | None = None) -> None:
            body = json.dumps(doc).encode() if doc is not None else b""
            self.send_response(status)
            for k, v in {"Content-Type": "application/json", "Content-Length": str(len(body)), **(headers or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            u = urlsplit(self.path)
            if u.path == "/.well-known/openid-configuration":
                return self.reply(200, {"issuer": me.url, "authorization_endpoint": me.url + "/authorize",
                                        "token_endpoint": me.url + "/token", "jwks_uri": me.url + "/jwks"})
            if u.path == "/jwks":
                return self.reply(200, jwks)
            if u.path == "/authorize":
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if q.get("client_id") != me.client_id or q.get("code_challenge_method") != "S256":
                    return self.reply(400, {"error": "invalid_request"})
                code = uuid.uuid4().hex
                codes[code] = q
                back = q["redirect_uri"] + "?" + urlencode({"code": code, "state": q["state"]})
                return self.reply(302, None, {"Location": back})
            self.reply(404, {"error": "not_found"})

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
            form = {k: v[0] for k, v in parse_qs(raw).items()}
            flight = codes.pop(form.get("code", ""), None)
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if not flight or form.get("client_secret") != me.client_secret or challenge != flight["code_challenge"] \
                    or form.get("redirect_uri") != flight["redirect_uri"]:
                return self.reply(400, {"error": "invalid_grant"})
            now = int(time.time())
            claims = {"iss": me.url, "aud": me.client_id, "sub": "fake-subject-1", "email": me.email, "email_verified": True,
                      "name": me.name, "nonce": flight["nonce"], "iat": now, "exp": now + 300}
            token = JsonWebToken(["RS256"]).encode({"alg": "RS256", "kid": "k1"}, claims, private).decode()
            self.reply(200, {"access_token": "fake", "token_type": "Bearer", "id_token": token})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield me
    server.shutdown()
