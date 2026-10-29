"""The HarnessGateway console: the user console (one organization: workbench and organization pages, the chat
embedded) at `/`, and the deployer console (the platform: health, nodes, organizations, usage, versions,
configuration) at `/ops`. Both are static pages on the platform's public API; this server holds the keys and the
browser never sees one.

    python3 console/server.py [--api http://localhost:8000] [--host 127.0.0.1] [--port 8800] [--state DIR]
    python3 console/server.py init          # the first users, printed once (install.sh runs this)

Keys come from the environment: HG_KEY (an admin key of the organization), HG_OPERATOR_KEY (the platform's operator
key; without it there is no deployer console). Users, the cookie secret, the members' run key and the audit log live
in the state directory (HG_CONSOLE_DIR, default ./console-state).

Roles: `deployer` (the /ops pages; the operator key for /internal, the admin key for /v1), `admin` (every page of the
organization; the admin key), `member` (the workbench; a run-scoped key this server creates once). A sign-in is a
signed cookie; a first sign-in with an initial password must change it. Every write is in the audit log."""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
import posixpath
import re
import secrets
import shutil
import sys
import tarfile
import threading
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
TEMPLATES = {
    "overlay": {
        "harness.json": '{{\n  "contract": 1,\n  "name": "{name}",\n  "version": "0.1.0",\n  "extends": "{base}",\n'
                        '  "description": "{base} with this organization\'s configuration"\n}}\n',
        "seeds/home/AGENTS.md": "# Instructions\n\nStanding guidance for the agent: how this organization works, "
                                "what to check before finishing, what never to do.\n",
    },
    "base": {
        "harness.json": '{{\n  "contract": 1,\n  "name": "{name}",\n  "version": "0.1.0-1",\n  "description": "",\n'
                        '  "provider": {{"apis": ["anthropic"]}},\n'
                        '  "install": {{"egress": ["registry.npmjs.org"], "timeout_s": 600}},\n'
                        '  "run": {{"egress": [], "stop_grace_s": 10}},\n  "persist": []\n}}\n',
        "install.sh": "#!/usr/bin/env bash\n# Install the CLI into $HG_TOOLS, pinned to a version, by its official method.\n"
                      "set -euo pipefail\nmkdir -p \"$HG_TOOLS/bin\"\necho \"installed: nothing yet\"\n",
        "run.sh": '#!/usr/bin/env bash\n# One turn: the request in (stdin and $HG_REQUEST), Responses events out.\n'
                  'set -uo pipefail\nexec python3 "$HG_PACKAGE_DIR/map.py"\n',
        "map.py": '"""One turn: read the request, run the CLI, print Responses events. See docs/packages.md and hgkit."""\n'
                  "from hgkit import Emitter, Request\n\n\ndef main() -> None:\n    req, out = Request.load(), Emitter()\n"
                  "    names = req.place_inputs()\n"
                  "    out.message(f\"{name} received: {{req.prompt[:200]}}\" + (f\" with {{names}}\" if names else \"\"))\n"
                  "    out.ignored([f for f in (\"instructions\", \"reasoning\", \"text\", \"tools\") if req.get(f)])\n"
                  "    out.completed()\n\n\nif __name__ == \"__main__\":\n    main()\n",
        "seeds/workspace/README.md": "# Workspace\n",
        "README.md": "# {name}\n\nWhat this harness is, which CLI and version, how request fields map, known limits, tests.\n",
    },
}
COOKIE = "hgc"
SESSION_S = 12 * 3600
ROLES = ("deployer", "admin", "member")
MEMBER_PATHS = ("/v1/responses", "/v1/containers", "/v1/files", "/v1/models", "/v1/platform")
PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "frame-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
BODY_MAX = 64 * 1024 * 1024


# ── users, sessions, audit: the state directory ───────────────────────────────────────────────────────────────────
class State:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.secret = self._secret()

    def _secret(self) -> bytes:
        p = self.folder / "secret"
        if not p.is_file():
            p.touch(mode=0o600)
            p.write_bytes(secrets.token_bytes(32))
        return p.read_bytes()

    # users.json: {name: {role, salt, hash, must_change, created_at}}
    def users(self) -> dict[str, dict[str, Any]]:
        p = self.folder / "users.json"
        return json.loads(p.read_text()) if p.is_file() else {}

    def save_users(self, users: dict[str, dict[str, Any]]) -> None:
        p = self.folder / "users.json"
        tmp = p.with_suffix(".tmp")
        tmp.touch(mode=0o600)
        tmp.write_text(json.dumps(users, indent=2))
        os.replace(tmp, p)

    @staticmethod
    def _hash(password: str, salt: bytes) -> str:
        return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()

    def set_user(self, name: str, role: str, password: str, must_change: bool) -> None:
        salt = secrets.token_bytes(16)
        with self.lock:
            users = self.users()
            users[name] = {"role": role, "salt": salt.hex(), "hash": self._hash(password, salt),
                           "must_change": must_change, "created_at": int(time.time())}
            self.save_users(users)

    def delete_user(self, name: str) -> None:
        with self.lock:
            users = self.users()
            users.pop(name, None)
            self.save_users(users)

    def check(self, name: str, password: str) -> dict[str, Any] | None:
        u = self.users().get(name)
        if not u or not hmac.compare_digest(self._hash(password, bytes.fromhex(u["salt"])), u["hash"]):
            return None
        return u

    # the cookie: base64(json{u, r, exp}).hmac
    def issue(self, name: str, role: str) -> str:
        doc = {"u": name, "r": role, "exp": int(time.time()) + SESSION_S}
        payload = base64.urlsafe_b64encode(json.dumps(doc).encode()).decode()
        return payload + "." + hmac.new(self.secret, payload.encode(), "sha256").hexdigest()

    def verify(self, cookie: str) -> dict[str, Any] | None:
        payload, _, sig = cookie.partition(".")
        if not payload or not hmac.compare_digest(hmac.new(self.secret, payload.encode(), "sha256").hexdigest(), sig):
            return None
        try:
            doc = json.loads(base64.urlsafe_b64decode(payload))
        except ValueError:
            return None
        if doc.get("exp", 0) < time.time() or doc.get("r") not in ROLES:
            return None
        user = self.users().get(doc.get("u", ""))
        if not user or user["role"] != doc["r"]:              # a deleted or re-roled user signs in again
            return None
        return {"name": doc["u"], "role": doc["r"], "must_change": user["must_change"]}

    def audit(self, user: str, action: str, detail: dict[str, Any]) -> None:
        with self.lock, (self.folder / "audit.jsonl").open("a") as f:
            f.write(json.dumps({"at": int(time.time()), "user": user, "action": action, **detail}) + "\n")

    def audit_tail(self, limit: int = 200) -> list[dict[str, Any]]:
        p = self.folder / "audit.jsonl"
        if not p.is_file():
            return []
        lines = p.read_text().splitlines()[-limit:]
        return [json.loads(line) for line in reversed(lines)]

    # drafts/<name>/: a package being written here; `meta.json` says where it came from
    @property
    def drafts(self) -> Path:
        p = self.folder / "drafts"
        p.mkdir(exist_ok=True)
        return p

    def draft(self, name: str) -> Path:
        if not DRAFT_NAME.fullmatch(name):
            raise ValueError("a draft name is lower-case letters, digits, dot, dash or underscore")
        return self.drafts / name

    def draft_file(self, name: str, rel: str) -> Path:
        root = self.draft(name)
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

    def member_key(self, admin: httpx.Client) -> str:
        """The run-scoped key members use, created once through the platform and kept here."""
        p = self.folder / "member-key"
        if not p.is_file():
            r = admin.post("/v1/keys", json={"scope": "run", "label": "console members"})
            r.raise_for_status()
            p.touch(mode=0o600)
            p.write_text(r.json()["key"])
        return p.read_text().strip()


# ── the server ────────────────────────────────────────────────────────────────────────────────────────────────────
class Handler(chat.Handler):
    """The console's pages and API, with the chat UI's handler for everything under /chat/."""
    state: State
    clients: dict[str, httpx.Client]             # admin, member, operator (operator absent without HG_OPERATOR_KEY)
    console_static: dict[str, Path]

    # ── who ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def user(self) -> dict[str, Any] | None:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return self.state.verify(value)
        return None

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or urlsplit(origin).netloc == self.headers.get("Host")

    def _set_cookie(self, value: str, clear: bool = False) -> dict[str, str]:
        attrs = "Path=/; HttpOnly; SameSite=Lax" + ("; Max-Age=0" if clear else f"; Max-Age={SESSION_S}")
        return {"Set-Cookie": f"{COOKIE}={value}; {attrs}"}

    def _json_h(self, status: int, doc: object, headers: dict[str, str]) -> None:
        self._reply(status, json.dumps(doc).encode(), "application/json", headers)

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
            elif path in ("/", "/ops", "/login"):
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
        user = self.user()
        if path == "/login":
            name = "login.html"
        elif not user:
            self._reply(302, b"", "text/plain", {"Location": "/login" + ("?next=/ops" if path == "/ops" else "")})
            return
        elif path == "/ops":
            if user["role"] != "deployer":
                self._reply(302, b"", "text/plain", {"Location": "/"})
                return
            name = "ops.html"
        else:
            if user["role"] == "deployer" and "operator" in self.clients:
                pass                                    # a deployer may use the organization's console too
            name = "index.html"
        self._reply(200, self.console_static[name].read_bytes(), "text/html; charset=utf-8",
                    {"Cache-Control": "no-cache", "Content-Security-Policy": PAGE_CSP})

    # ── the chat UI under /chat/ ─────────────────────────────────────────────────────────────────────────────────
    def _chat_ui(self, method: str, path: str) -> None:
        user = self.user()
        if not user or user["must_change"]:
            if method == "POST":
                self._json(401, {"error": "sign in"})
            else:
                self._reply(302, b"", "text/plain", {"Location": "/login"})
            return
        self.http = self.clients["admin" if user["role"] != "member" else "member"]
        self.path = self.path[len("/chat"):] or "/"
        if method == "GET":
            super().do_GET()
        elif method == "POST":
            if self.path == "/api/chat":
                self.state.audit(user["name"], "chat.turn", {})
            super().do_POST()
        else:
            self._text(405, "method not allowed")

    # ── the console's own API, and the relay to the platform ─────────────────────────────────────────────────────
    def _api(self, method: str, path: str, query: str) -> None:
        if method != "GET" and not self._same_origin():
            raise PermissionError("requests come from this page")
        if path == "/api/login" and method == "POST":
            self._login()
            return
        user = self.user()
        if not user:
            self._json(401, {"error": "sign in"})
            return
        if path == "/api/logout" and method == "POST":
            self.state.audit(user["name"], "logout", {})
            self._json_h(200, {"ok": True}, self._set_cookie("", clear=True))
        elif path == "/api/password" and method == "POST":
            self._password(user)
        elif user["must_change"]:
            self._json(403, {"error": "change the initial password first", "must_change": True})
        elif path == "/api/me" and method == "GET":
            self._me(user)
        elif path == "/api/users":
            self._users(method, user)
        elif path == "/api/audit" and method == "GET":
            if user["role"] != "deployer":
                raise PermissionError("the audit log is the deployer's")
            self._json(200, {"data": self.state.audit_tail()})
        elif path.startswith("/api/drafts"):
            if user["role"] == "member":
                raise PermissionError("drafts are the organization's to edit")
            self._drafts(method, path.removeprefix("/api/drafts").strip("/"), user)
        elif path.startswith("/api/v1/") or path.startswith("/api/internal/"):
            self._proxy(method, path.removeprefix("/api"), query, user)
        else:
            self._text(404, "not found")

    def _login(self) -> None:
        doc = self._body()
        name, password = str(doc.get("username", "")), str(doc.get("password", ""))
        time.sleep(0.3)                                  # a slow answer blunts guessing
        u = self.state.check(name, password)
        if not u:
            self._json(401, {"error": "unknown user or wrong password"})
            return
        self.state.audit(name, "login", {"role": u["role"]})
        self._json_h(200, {"name": name, "role": u["role"], "must_change": u["must_change"]},
                     self._set_cookie(self.state.issue(name, u["role"])))

    def _password(self, user: dict[str, Any]) -> None:
        doc = self._body()
        current, new = str(doc.get("current", "")), str(doc.get("new", ""))
        if not self.state.check(user["name"], current):
            self._json(401, {"error": "the current password is wrong"})
            return
        if len(new) < 12:
            raise ValueError("a new password is at least 12 characters")
        self.state.set_user(user["name"], user["role"], new, must_change=False)
        self.state.audit(user["name"], "password.change", {})
        self._json(200, {"ok": True})

    def _me(self, user: dict[str, Any]) -> None:
        plat: dict[str, Any] = {}
        with contextlib.suppress(httpx.HTTPError):
            r = self.clients["admin"].get("/v1/platform")
            plat = r.json() if r.status_code == 200 else {}
        self._json(200, {"name": user["name"], "role": user["role"], "platform": plat, "api": str(self.clients["admin"].base_url),
                         "ops": "operator" in self.clients and user["role"] == "deployer"})

    def _users(self, method: str, user: dict[str, Any]) -> None:
        """Admins manage the organization's members; deployers manage deployer users."""
        manages = {"deployer": ("deployer",), "admin": ("admin", "member")}.get(user["role"], ())
        if not manages:
            raise PermissionError("members do not manage users")
        if method == "GET":
            rows = [{"name": n, "role": u["role"], "must_change": u["must_change"], "created_at": u["created_at"]}
                    for n, u in sorted(self.state.users().items()) if u["role"] in manages]
            self._json(200, {"data": rows})
            return
        doc = self._body()
        name = str(doc.get("username", "")).strip()
        if not name.replace("-", "").replace("_", "").replace(".", "").isalnum() or len(name) > 64:
            raise ValueError("a username is letters, digits, dot, dash or underscore")
        if method == "POST":
            role = str(doc.get("role", ""))
            if role not in manages:
                raise PermissionError(f"you may create {', '.join(manages)} users")
            if name in self.state.users():
                raise ValueError(f"{name} exists")
            password = secrets.token_urlsafe(12)
            self.state.set_user(name, role, password, must_change=True)
            self.state.audit(user["name"], "user.create", {"target": name, "role": role})
            self._json(200, {"name": name, "role": role, "password": password})   # shown once
        elif method == "DELETE":
            target = self.state.users().get(name)
            if not target or target["role"] not in manages:
                raise PermissionError("not a user you manage")
            if name == user["name"]:
                raise ValueError("you cannot delete yourself")
            self.state.delete_user(name)
            self.state.audit(user["name"], "user.delete", {"target": name})
            self._json(200, {"ok": True})
        else:
            self._text(405, "method not allowed")

    # ── drafts: packages written in the console ──────────────────────────────────────────────────────────────────
    def _drafts(self, method: str, rest: str, user: dict[str, Any]) -> None:
        """/api/drafts (GET list, POST create), /api/drafts/<name> (DELETE), /api/drafts/<name>/files (GET list),
        /api/drafts/<name>/files/<path> (GET, PUT, DELETE), /api/drafts/<name>/install (POST)."""
        st, admin = self.state, self.clients["admin"]
        if not rest:
            if method == "GET":
                self._json(200, {"data": st.draft_list()})
            elif method == "POST":
                self._draft_create(self._body(), user)
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
            st.audit(user["name"], "draft.discard", {"draft": name})
            self._json(200, {"ok": True})
        elif tail == "files" and method == "GET":
            files = [{"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size,
                      "executable": bool(p.stat().st_mode & 0o111)}
                     for p in sorted(root.rglob("*")) if p.is_file() and p.name != "meta.json"]
            meta = json.loads((root / "meta.json").read_text()) if (root / "meta.json").is_file() else {}
            self._json(200, {"name": name, "based_on": meta.get("based_on", ""), "data": files})
        elif tail.startswith("files/"):
            rel = tail[len("files/"):]
            p = st.draft_file(name, rel)
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
                if p.suffix == ".sh":
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
            r = admin.post("/v1/harnesses", files={"package": (f"{name}.tgz", buf.getvalue())})
            if r.status_code == 200:
                st.audit(user["name"], "draft.install", {"draft": name, "harness": r.json().get("name")})
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

    def _draft_create(self, doc: dict[str, Any], user: dict[str, Any]) -> None:
        """{"name", "fork": <installed harness>} copies that package's files; {"name", "template": "overlay"|"base",
        "extends": <base>} starts from a template."""
        st, admin = self.state, self.clients["admin"]
        name = str(doc.get("name", "")).strip()
        root = st.draft(name)
        if root.exists():
            raise ValueError(f"draft {name} exists")
        root.mkdir(parents=True)
        try:
            if doc.get("fork"):
                source = str(doc["fork"])
                r = admin.get(f"/v1/harnesses/{source}/files")
                r.raise_for_status()
                for f in r.json()["data"]:
                    content = admin.get(f"/v1/harnesses/{source}/files/{f['path']}")
                    content.raise_for_status()
                    p = st.draft_file(name, f["path"])
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(content.content)
                    if f.get("executable"):
                        p.chmod(0o755)
                manifest = root / "harness.json"
                if manifest.is_file():                      # a fork is its own package: a new name
                    try:
                        m = json.loads(manifest.read_text())
                        m["name"] = name
                        manifest.write_text(json.dumps(m, indent=2) + "\n")
                    except ValueError:
                        pass
                based_on = f"fork of {source}"
            else:
                template = str(doc.get("template", "overlay"))
                base = str(doc.get("extends", "")).strip()
                if template not in TEMPLATES or (template == "overlay" and not base):
                    raise ValueError("template is overlay (with extends) or base")
                for rel, text in TEMPLATES[template].items():
                    p = st.draft_file(name, rel)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(text.format(name=name, base=base))
                    if rel.endswith(".sh"):
                        p.chmod(0o755)
                based_on = f"overlay on {base}" if template == "overlay" else "base template"
            meta = {"based_on": based_on, "created_by": user["name"], "created_at": int(time.time())}
            (root / "meta.json").write_text(json.dumps(meta))
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        st.audit(user["name"], "draft.create", {"draft": name, "based_on": based_on})
        self._json(200, {"name": name, "based_on": based_on})

    def _proxy(self, method: str, path: str, query: str, user: dict[str, Any]) -> None:
        """A platform call with the key the user's role gets; bodies pass through as they are (JSON, multipart, or
        a streamed SSE reply)."""
        role = user["role"]
        if path.startswith("/internal/"):
            if role != "deployer" or "operator" not in self.clients:
                raise PermissionError("the deployer console does this")
            client = self.clients["operator"]
        elif role == "member":
            if not any(path == p or path.startswith(p + "/") or path.startswith(p + "?") for p in MEMBER_PATHS):
                raise PermissionError("members use the workbench only")
            client = self.clients["member"]
        else:
            client = self.clients["admin"]
        size = int(self.headers.get("Content-Length") or 0)
        if size > BODY_MAX:
            raise ValueError("the body is too large")
        body = self.rfile.read(size) if size else None
        headers = {k: v for k, v in self.headers.items() if k.lower() in ("content-type", "accept", "idempotency-key")}
        url = path + (f"?{query}" if query else "")
        if method != "GET":
            self.state.audit(user["name"], "api", {"method": method, "path": path})
        try:
            with client.stream(method, url, headers=headers, content=body, timeout=httpx.Timeout(60, read=600)) as r:
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


def init_users(state: State) -> dict[str, str]:
    """The first users: a deployer and an organization admin, with passwords shown once and changed at first sign-in."""
    if state.users():
        return {}
    out = {}
    for name, role in (("deployer", "deployer"), ("admin", "admin")):
        password = secrets.token_urlsafe(12)
        state.set_user(name, role, password, must_change=True)
        out[name] = password
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="serve", choices=["serve", "init"])
    ap.add_argument("--api", default=os.environ.get("HG_API", "http://localhost:8000"))
    ap.add_argument("--key", default=os.environ.get("HG_KEY", ""))
    ap.add_argument("--operator-key", default=os.environ.get("HG_OPERATOR_KEY", ""))
    ap.add_argument("--state", default=os.environ.get("HG_CONSOLE_DIR", "console-state"))
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to reach it from outside this machine (it holds your keys)")
    ap.add_argument("--port", type=int, default=8800)
    args = ap.parse_args()
    state = State(Path(args.state))
    if args.command == "init":
        made = init_users(state)
        print(json.dumps({"users": made, "note": "shown once; each must be changed at first sign-in" if made
                          else "users exist already"}, indent=2))
        return
    if not args.key:
        sys.exit("set HG_KEY (or pass --key) to an admin key of your organization")
    api = args.api.rstrip("/")
    admin = httpx.Client(base_url=api, headers={"Authorization": f"Bearer {args.key}"}, timeout=120)
    clients = {"admin": admin}
    try:
        member_key = state.member_key(admin)
        clients["member"] = httpx.Client(base_url=api, headers={"Authorization": f"Bearer {member_key}"}, timeout=120)
    except httpx.HTTPError as e:
        sys.exit(f"cannot create the members' key through {api}: {e}")
    if args.operator_key:
        clients["operator"] = httpx.Client(base_url=api, headers={"Authorization": f"Bearer {args.operator_key}"}, timeout=120)
    if not state.users():
        made = init_users(state)
        print("first users (shown once; change at first sign-in): " + json.dumps(made), file=sys.stderr)
    Handler.state, Handler.clients = state, clients
    Handler.http, Handler.workspace = admin, chat.Workspace(admin)
    Handler.default_model = os.environ.get("HG_MODEL", "")
    Handler.static = {p.name: p for p in chat.STATIC.iterdir() if p.is_file()}
    Handler.console_static = {p.name: p for p in STATIC.iterdir() if p.is_file()}
    server = chat.Server((args.host, args.port), Handler)
    print(f"HarnessGateway console on http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
          f" (API {api}; deployer console {'on' if args.operator_key else 'off: no HG_OPERATOR_KEY'}); Ctrl+C to stop",
          file=sys.stderr)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
