"""The HarnessGateway console: the organization's console (harnesses, the chat, keys, sessions, accounts) at `/` and
the platform console (health, nodes, organizations, usage, configuration, audit) at `/ops`, both static pages on the
platform's public API. This server holds no key: a person signs in with the platform's own accounts (SPEC §9.3), the
sign-in token lives in an HttpOnly cookie, and every call is relayed with it as the bearer. The platform decides what
each person may do.

    python3 console/server.py [--api http://localhost:8000] [--host 127.0.0.1] [--port 8800] [--state DIR]

The state directory (HG_CONSOLE_DIR, default ./console-state) holds drafts: packages being written here. An invitation
link (`/setup?token=hgi_…`, mailed or printed by `harnessgateway init` and by inviting an account) activates an account
by setting its password."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import sys
import tarfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "chat"))
import server as chat  # noqa: E402  (the chat UI, mounted under /chat/)

STATIC = Path(__file__).resolve().parent / "static"
DRAFT_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
DRAFT_FILE_MAX = 4 * 1024 * 1024
# every new harness starts as a copy of the template: the shell package, the smallest complete package on the contract
TEMPLATE_DIR = Path(os.environ.get("HG_TEMPLATE_DIR") or Path(__file__).resolve().parents[1] / "packages" / "shell")
EXECUTABLES = ("install", "run")                            # the package's two executables
BUILDER = "harness-package-builder"                        # the built-in whose sessions' workspaces hold packages to import
SIGN_IN = "hgs"                                             # the cookie: the sign-in token
SESSION_S = 12 * 3600
PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "frame-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
BODY_MAX = 64 * 1024 * 1024


class Bound:
    """The shared platform client, bound to one request's credential: the sign-in as bearer. `who` keys the chat's
    caches per person."""

    def __init__(self, client: httpx.Client, token: str):
        self.client, self.token = client, token
        self.who = hashlib.sha256(token.encode()).hexdigest()[:16]

    def headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", **(extra or {})}

    def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        return self.client.request(method, url, headers=self.headers(kw.pop("headers", None)), **kw)

    def get(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("POST", url, **kw)

    def stream(self, method: str, url: str, **kw: Any) -> Any:
        return self.client.stream(method, url, headers=self.headers(kw.pop("headers", None)), **kw)


# ── drafts: the state directory ───────────────────────────────────────────────────────────────────────────────────
class State:
    """drafts/<name>/: a package being written here; `meta.json` says where it came from."""

    def __init__(self, folder: Path):
        self.drafts = folder / "drafts"
        self.drafts.mkdir(parents=True, exist_ok=True)

    def draft(self, name: str) -> Path:
        if not DRAFT_NAME.fullmatch(name):
            raise ValueError("a draft name is lower-case letters, digits, dot, dash or underscore")
        return self.drafts / name

    @staticmethod
    def draft_file(root: Path, rel: str) -> Path:
        norm = posixpath.normpath(rel)
        if not rel or rel.startswith("/") or norm in (".", "..") or norm.startswith("../") or "\0" in rel or len(rel) > 512:
            raise ValueError("a file path inside the draft")
        return root / norm

    def draft_list(self) -> list[dict[str, Any]]:
        out = []
        for d in sorted(self.drafts.iterdir()):
            if not d.is_dir():
                continue
            meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").is_file() else {}
            files = [p for p in d.rglob("*") if p.is_file() and p.name != "meta.json"]
            out.append({"name": d.name, "based_on": meta.get("based_on", ""), "files": len(files),
                        "updated_at": int(max((p.stat().st_mtime for p in files), default=d.stat().st_mtime))})
        return out


# ── the server ────────────────────────────────────────────────────────────────────────────────────────────────────
class Handler(chat.Handler):
    """The console's pages and API, with the chat UI's handler for everything under /chat/."""
    state: State
    platform: httpx.Client                        # the API, no credential: each request binds its own
    console_static: dict[str, Path]

    # ── who ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def cookie(self, name: str) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def bound(self) -> Bound | None:
        """This request's credential, or None without a sign-in cookie. The platform decides whether it is still good."""
        token = self.cookie(SIGN_IN)
        return Bound(self.platform, token) if token.startswith("hgl_") else None

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or urlsplit(origin).netloc == self.headers.get("Host")

    @staticmethod
    def _cookie(name: str, value: str, clear: bool = False) -> str:
        attrs = "Path=/; HttpOnly; SameSite=Lax" + ("; Max-Age=0" if clear else f"; Max-Age={SESSION_S}")
        return f"{name}={value}; {attrs}"

    def _json_h(self, status: int, doc: object, cookies: list[str]) -> None:
        body = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for c in cookies:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(body)

    # ── routing ──────────────────────────────────────────────────────────────────────────────────────────────────
    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_PUT(self) -> None:
        self._route("PUT")

    def do_PATCH(self) -> None:
        self._route("PATCH")

    def do_DELETE(self) -> None:
        self._route("DELETE")

    def _route(self, method: str) -> None:
        url = urlsplit(self.path)
        path = url.path
        try:
            if path.startswith("/static/"):
                self._console_static(path.removeprefix("/static/"))
            elif path in ("/", "/ops", "/login", "/setup"):
                self._page(path)
            elif path.startswith("/chat/") or path == "/chat":
                self._chat_ui(method, path)
            elif path.startswith("/api/"):
                self._api(method, path, url.query)
            else:
                self._text(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except PermissionError as e:
            self._json(403, {"error": str(e)})
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except httpx.HTTPStatusError as e:
            self._reply(e.response.status_code, e.response.content, e.response.headers.get("content-type", "application/json"))
        except httpx.HTTPError as e:
            self._json(502, {"error": {"message": f"the platform is unreachable: {e}"}})

    def _console_static(self, name: str) -> None:
        path = self.console_static.get(name)
        if not path:
            self._text(404, "not found")
            return
        kinds = {"css": "text/css", "js": "text/javascript", "html": "text/html", "svg": "image/svg+xml", "png": "image/png"}
        kind = kinds.get(path.suffix[1:], "application/octet-stream")
        self._reply(200, path.read_bytes(), kind if kind.startswith("image/") else f"{kind}; charset=utf-8",
                    {"Cache-Control": "no-cache"})

    def _page(self, path: str) -> None:
        if path in ("/login", "/setup"):
            name = "login.html"
        elif not self.bound():
            self._reply(302, b"", "text/plain", {"Location": "/login" + ("?next=/ops" if path == "/ops" else "")})
            return
        else:
            name = "ops.html" if path == "/ops" else "index.html"
        self._reply(200, self.console_static[name].read_bytes(), "text/html; charset=utf-8",
                    {"Cache-Control": "no-cache", "Content-Security-Policy": PAGE_CSP})

    # ── the chat UI under /chat/ ─────────────────────────────────────────────────────────────────────────────────
    def _chat_ui(self, method: str, path: str) -> None:
        http = self.bound()
        if http is None:
            if method == "POST":
                self._json(401, {"error": "sign in"})
            else:
                self._reply(302, b"", "text/plain", {"Location": "/login"})
            return
        self.http = http  # type: ignore[assignment]
        self.path = self.path[len("/chat"):] or "/"
        if method == "GET":
            super().do_GET()
        elif method == "POST":
            super().do_POST()
        else:
            self._text(405, "method not allowed")

    # ── the console's own API, and the relay to the platform ─────────────────────────────────────────────────────
    def _api(self, method: str, path: str, query: str) -> None:
        if method != "GET" and not self._same_origin():
            raise PermissionError("requests come from this page")
        if path in ("/api/login", "/api/accept") and method == "POST":
            self._sign_in(path)
            return
        http = self.bound()
        if http is None:
            self._json(401, {"error": "sign in"})
            return
        if path == "/api/logout" and method == "POST":
            with contextlib.suppress(httpx.HTTPError):
                http.post("/v1/auth/logout")
            self._json_h(200, {"ok": True}, [self._cookie(SIGN_IN, "", clear=True)])
        elif path == "/api/me" and method == "GET":
            r = http.get("/v1/auth/me")
            r.raise_for_status()
            self._json(200, r.json())
        elif path.startswith("/api/drafts"):
            self._drafts(method, path.removeprefix("/api/drafts").strip("/"), http)
        elif path.startswith("/api/v1/") or path.startswith("/api/internal/"):
            self._proxy(method, path.removeprefix("/api"), query, http)
        else:
            self._text(404, "not found")

    def _sign_in(self, path: str) -> None:
        """Sign in, or accept an invitation: the platform answers with a sign-in, which becomes the cookie and never
        reaches the page."""
        doc = self._body()
        time.sleep(0.3)                                  # a slow answer blunts guessing
        r = self.platform.post("/v1/auth" + path.removeprefix("/api"), json=doc,
                               headers={"User-Agent": f"console {self.headers.get('User-Agent', '')[:120]}"})
        if r.status_code != 200:
            self._reply(r.status_code, r.content, r.headers.get("content-type", "application/json"))
            return
        signed = r.json()
        token = signed.pop("token")
        self._json_h(200, signed, [self._cookie(SIGN_IN, token)])

    # ── drafts: packages written in the console ──────────────────────────────────────────────────────────────────
    def _drafts(self, method: str, rest: str, http: Bound) -> None:
        """/api/drafts (GET list, POST create), /api/drafts/<name> (DELETE), /api/drafts/<name>/files (GET list),
        /api/drafts/<name>/files/<path> (GET, PUT, DELETE), /api/drafts/<name>/install (POST: installs it and, when that
        succeeds, removes the draft)."""
        st = self.state
        if not rest:
            if method == "GET":
                self._json(200, {"data": st.draft_list()})
            elif method == "POST":
                self._draft_create(self._body(), http)
            else:
                self._text(405, "method not allowed")
            return
        name, _, tail = rest.partition("/")
        root = st.draft(name)
        if not root.is_dir():
            self._json(404, {"error": f"no draft {name}"})
            return
        if not tail and method == "DELETE":
            shutil.rmtree(root)
            self._json(200, {"ok": True})
        elif tail == "files" and method == "GET":
            files = [{"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size,
                      "executable": bool(p.stat().st_mode & 0o111)}
                     for p in sorted(root.rglob("*")) if p.is_file() and p.name != "meta.json"]
            meta = json.loads((root / "meta.json").read_text()) if (root / "meta.json").is_file() else {}
            self._json(200, {"name": name, "based_on": meta.get("based_on", ""), "data": files})
        elif tail.startswith("files/"):
            rel = tail[len("files/"):]
            p = st.draft_file(root, rel)
            if p.name == "meta.json" and p.parent == root:
                raise PermissionError("meta.json is the console's")
            if method == "GET":
                if not p.is_file():
                    self._json(404, {"error": "no such file"})
                    return
                kind = "text/plain; charset=utf-8" if self._text_like(p) else "application/octet-stream"
                self._reply(200, p.read_bytes(), kind)
            elif method == "PUT":
                size = int(self.headers.get("Content-Length") or 0)
                if size > DRAFT_FILE_MAX:
                    raise ValueError("a draft file is at most 4 MB")
                data = self.rfile.read(size)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                if p.name in EXECUTABLES:
                    p.chmod(0o755)
                self._json(200, {"path": rel, "bytes": len(data)})
            elif method == "DELETE":
                if p.is_file():
                    p.unlink()
                    for parent in p.parents:                 # empty folders go with their last file
                        if parent == root or any(parent.iterdir()):
                            break
                        parent.rmdir()
                self._json(200, {"ok": True})
            else:
                self._text(405, "method not allowed")
        elif tail == "install" and method == "POST":
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                for p in sorted(root.rglob("*")):
                    if p.is_file() and p.name != "meta.json":
                        tar.add(p, arcname=p.relative_to(root).as_posix())
            r = http.post("/v1/harnesses", files={"package": (f"{name}.tgz", buf.getvalue())})
            if r.status_code == 200:                         # installed: the harness is the record, the draft is done
                shutil.rmtree(root, ignore_errors=True)
            self._reply(r.status_code, r.content, r.headers.get("content-type", "application/json"))
        else:
            self._text(404, "not found")

    @staticmethod
    def _text_like(p: Path) -> bool:
        try:
            data = p.read_bytes()[:4096]
            data.decode("utf-8")
            return b"\0" not in data
        except UnicodeDecodeError:
            return False

    def _draft_create(self, doc: dict[str, Any], http: Bound) -> None:
        """{"name"} starts a new harness as a copy of the template under that name; {"name", "edit": true} opens the
        installed harness of that name as a draft (installing the draft upgrades it in place); {"name", "session": id,
        "folder": f} copies /workspace/<folder>/ of a harness-package-builder session, where the builder wrote a package."""
        st = self.state
        name = str(doc.get("name", "")).strip()
        root = st.draft(name)
        if root.exists():
            raise ValueError(f"draft {name} exists")
        root.mkdir(parents=True)
        try:
            if doc.get("edit"):
                r = http.get(f"/v1/harnesses/{name}/files")
                r.raise_for_status()
                for f in r.json()["data"]:
                    content = http.get(f"/v1/harnesses/{name}/files/{f['path']}")
                    content.raise_for_status()
                    p = st.draft_file(root, f["path"])
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(content.content)
                    if f.get("executable"):
                        p.chmod(0o755)
                based_on = "the installed harness"
            elif doc.get("session"):
                sid, folder = str(doc["session"]), str(doc.get("folder", "")).strip().strip("/")
                if not folder or "/" in folder or folder in (".", ".."):
                    raise ValueError("folder is a directory at the top of the session's workspace")
                r = http.get(f"/v1/containers/{sid}")
                r.raise_for_status()
                if r.json().get("metadata", {}).get("harness") != BUILDER:
                    raise ValueError(f"only a {BUILDER} session's workspace holds packages to import")
                prefix, after, found = f"/workspace/{folder}/", None, 0
                while True:
                    r = http.get(f"/v1/containers/{sid}/files", params={"limit": 100, **({"after": after} if after else {})})
                    r.raise_for_status()
                    page = r.json()
                    for f in page["data"]:
                        if not f["path"].startswith(prefix) or "/__pycache__/" in f["path"]:
                            continue
                        content = http.get(f"/v1/containers/{sid}/files/{f['id']}/content")
                        content.raise_for_status()
                        p = st.draft_file(root, f["path"][len(prefix):])
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(content.content)
                        if p.name in EXECUTABLES:
                            p.chmod(0o755)
                        found += 1
                    if not page.get("has_more"):
                        break
                    after = page["last_id"]
                if not found:
                    raise ValueError(f"the session has no files under {prefix}")
                if not (root / "harness.json").is_file():
                    raise ValueError(f"{prefix} holds no harness.json")
                based_on = f"session {sid}, {folder}/"
            else:
                if not (TEMPLATE_DIR / "harness.json").is_file():
                    raise ValueError(f"the harness template is missing at {TEMPLATE_DIR}")
                for src in sorted(TEMPLATE_DIR.rglob("*")):       # not its tests: they run in a checkout, not here
                    if not src.is_file() or "__pycache__" in src.parts or src.relative_to(TEMPLATE_DIR).parts[0] == "tests":
                        continue
                    p = st.draft_file(root, src.relative_to(TEMPLATE_DIR).as_posix())
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(src.read_bytes())
                    if src.stat().st_mode & 0o111:
                        p.chmod(0o755)
                manifest = root / "harness.json"
                m = json.loads(manifest.read_text())
                m["name"] = name
                manifest.write_text(json.dumps(m, indent=2) + "\n")
                based_on = "the template"
            (root / "meta.json").write_text(json.dumps({"based_on": based_on, "created_at": int(time.time())}))
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        self._json(200, {"name": name, "based_on": based_on})

    def _proxy(self, method: str, path: str, query: str, http: Bound) -> None:
        """A platform call as the signed-in person; bodies pass through as they are (JSON, multipart, or a streamed
        SSE reply). The platform refuses what the person may not do."""
        size = int(self.headers.get("Content-Length") or 0)
        if size > BODY_MAX:
            raise ValueError("the body is too large")
        body = self.rfile.read(size) if size else None
        headers = {k: v for k, v in self.headers.items() if k.lower() in ("content-type", "accept", "idempotency-key")}
        url = path + (f"?{query}" if query else "")
        try:
            with http.stream(method, url, headers=headers, content=body, timeout=httpx.Timeout(60, read=600)) as r:
                self.send_response(r.status_code)
                kind = r.headers.get("content-type", "application/octet-stream")
                self.send_header("Content-Type", kind)
                self.send_header("X-Content-Type-Options", "nosniff")
                if r.headers.get("content-disposition"):
                    self.send_header("Content-Disposition", r.headers["content-disposition"])
                if kind.startswith("text/event-stream"):
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for chunk in r.iter_raw():
                        self.wfile.write(chunk)
                        self.wfile.flush()
                else:
                    data = r.read()
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
        except httpx.HTTPError as e:
            self._json(502, {"error": {"message": f"the platform is unreachable: {e}"}})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.environ.get("HG_API", "http://localhost:8000"))
    ap.add_argument("--state", default=os.environ.get("HG_CONSOLE_DIR", "console-state"))
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to reach it from outside this machine")
    ap.add_argument("--port", type=int, default=8800)
    args = ap.parse_args()
    api = args.api.rstrip("/")
    Handler.state = State(Path(args.state))
    Handler.platform = httpx.Client(base_url=api, timeout=120)
    Handler.workspace = chat.Workspace()
    Handler.default_model = os.environ.get("HG_MODEL", "")
    Handler.static = {p.name: p for p in chat.STATIC.iterdir() if p.is_file()}
    Handler.console_static = {p.name: p for p in STATIC.iterdir() if p.is_file()}
    server = chat.Server((args.host, args.port), Handler)
    print(f"HarnessGateway console on http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
          f" (API {api}); Ctrl+C to stop", file=sys.stderr)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
