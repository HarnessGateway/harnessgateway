"""A ChatGPT-style page for talking to a harness, with artifacts: conversations in a sidebar, replies streamed with
markdown, the agent's thinking and steps folded under each reply, and a side panel with the session's workspace files
(browse, preview, download) that opens what the agent builds: HTML pages run live, images, PDFs, markdown, CSV and
code render in place. Files you attach go to the agent with your message. The header picks the harness and the
provider from what your organization has installed and connected; type the model, or leave it empty for the
harness's default.

    HG_KEY=sk-hg-... python chatgui.py [--api http://localhost:8000] [--host 127.0.0.1] [--port 8800]

Then open http://localhost:8800 (from a Mac with the platform in an OrbStack VM: run with --host 0.0.0.0 and open
http://hg.orb.local:8800). This server holds your key; the page never sees it. Each conversation is one session: its
workspace, files and the agent's memory carry from message to message. Workspace files are read from the session's
last snapshot, so they appear when a turn finishes. Agent-made pages run sandboxed, in an origin of their own.
Conversations are kept in the browser. Needs: pip install openai (httpx comes with it).
"""
# ruff: noqa: E501  (the page below is HTML and JavaScript, not Python)
from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import json
import mimetypes
import os
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from openai import OpenAI

BODY_MAX = 32 * 1024 * 1024                    # an attachment (20 MB) as base64, with room to spare
ATTACH_MAX = 20 * 1024 * 1024
SESSION = re.compile(r"cntr_[0-9a-f]{8,64}")
# what the agent made runs in an origin of its own: it cannot read this page or call this server as the page
SANDBOX = ("sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads; "
           "default-src * data: blob: 'unsafe-inline' 'unsafe-eval'")
for ext, kind in ((".md", "text/markdown"), (".ts", "text/plain"), (".tsx", "text/plain"), (".jsx", "text/plain"),
                  (".py", "text/plain"), (".sh", "text/plain"), (".yaml", "text/plain"), (".yml", "text/plain"),
                  (".toml", "text/plain"), (".go", "text/plain"), (".rs", "text/plain"), (".webp", "image/webp")):
    mimetypes.add_type(kind, ext)


def describe(item: dict) -> dict | None:
    """One output item as the page shows it: a tool step (with its call id, to pair its output) or nothing."""
    kind, cid = item.get("type"), item.get("call_id") or item.get("id")
    if kind == "shell_call":
        return {"type": "tool", "id": cid, "icon": "terminal",
                "label": " && ".join((item.get("action") or {}).get("commands", []))}
    if kind == "shell_call_output":
        results = item.get("output") or []
        text = "".join((r.get("stdout") or "") + (r.get("stderr") or "") for r in results)
        codes = [(r.get("outcome") or {}).get("exit_code") for r in results]
        return {"type": "tool_output", "id": cid, "text": text, "exit_code": next((c for c in codes if c), 0)}
    if kind == "apply_patch_call":
        op = item.get("operation") or {}
        verb = {"create_file": "Created", "update_file": "Edited", "delete_file": "Deleted"}.get(op.get("type"), "Changed")
        return {"type": "tool", "id": cid, "icon": "file", "verb": verb, "path": op.get("path", ""),
                "label": f"{verb} {op.get('path', '')}", "detail": op.get("diff") or ""}
    if kind == "function_call":
        return {"type": "tool", "id": cid, "icon": "tool", "label": item.get("name", "tool"),
                "detail": item.get("arguments") or ""}
    if kind == "function_call_output":
        return {"type": "tool_output", "id": cid, "text": str(item.get("output") or ""), "exit_code": 0}
    if kind == "mcp_call":
        return {"type": "tool", "id": cid, "icon": "tool", "label": f"{item.get('server_label')}.{item.get('name')}",
                "detail": item.get("arguments") or "", "output": item.get("output") or ""}
    if kind == "web_search_call":
        return {"type": "tool", "id": cid, "icon": "search", "label": "Searched the web"}
    return None


class Workspace:
    """A session's files as the platform keeps them (its last snapshot), cached for a few seconds so that a page and
    the files it links to resolve quickly."""

    def __init__(self, http: httpx.Client):
        self.http = http
        self._cache: dict[str, tuple[float, dict[str, dict]]] = {}
        self._lock = threading.Lock()

    def files(self, sid: str, fresh: bool = False) -> dict[str, dict]:
        with self._lock:
            hit = self._cache.get(sid)
        if hit and not fresh and time.monotonic() - hit[0] < 5:
            return hit[1]
        out: dict[str, dict] = {}
        after = None
        for _ in range(100):                             # 100 pages of 100: the platform lists at most 20,000
            r = self.http.get(f"/v1/containers/{sid}/files", params={"limit": 100, **({"after": after} if after else {})})
            r.raise_for_status()
            doc = r.json()
            for f in doc["data"]:
                rel = f["path"].removeprefix("/workspace/")
                out[rel] = {"id": f["id"], "path": rel, "bytes": f.get("bytes", 0), "at": f.get("created_at", 0)}
            if not doc.get("has_more"):
                break
            after = doc.get("last_id")
        with self._lock:
            self._cache[sid] = (time.monotonic(), out)
        return out

    def content(self, sid: str, fid: str) -> bytes:
        r = self.http.get(f"/v1/containers/{sid}/files/{fid}/content")
        r.raise_for_status()
        return r.content


class Handler(BaseHTTPRequestHandler):
    client: OpenAI
    http: httpx.Client
    workspace: Workspace
    default_model: str
    snippets: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:       # quiet: one line per chat turn is printed instead
        pass

    # ── responses ────────────────────────────────────────────────────────────────────────────────────────────────
    def _reply(self, status: int, body: bytes, kind: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, doc: object) -> None:
        self._reply(status, json.dumps(doc).encode(), "application/json")

    def _body(self) -> dict:
        """A JSON request from this server's own page: anything else (a form, a page the agent made) is refused."""
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            raise PermissionError("requests are JSON")
        origin = self.headers.get("Origin")
        if origin is not None and urlsplit(origin).netloc != self.headers.get("Host"):
            raise PermissionError("requests come from this page")
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= BODY_MAX:
            raise ValueError("a JSON body is required")
        doc = json.loads(self.rfile.read(size))
        if not isinstance(doc, dict):
            raise ValueError("the body is a JSON object")
        return doc

    def _file(self, name: str, data: bytes, download: bool) -> None:
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind in ("application/json", "application/javascript", "image/svg+xml"):
            kind += "; charset=utf-8"
        headers = {"Cache-Control": "no-store"}
        if not kind.startswith("application/pdf"):         # a browser shows no PDF in a sandboxed document
            headers["Content-Security-Policy"] = SANDBOX
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{name.rsplit("/", 1)[-1].replace(chr(34), "")}"'
        self._reply(200, data, kind, headers)

    def catalog(self) -> dict:
        """What the picker offers: the organization's harnesses and its providers (no keys: the API never returns them)."""
        def listing(path: str) -> list[dict]:
            r = self.http.get(path)
            r.raise_for_status()
            return r.json()["data"]
        harnesses = [{k: h.get(k) for k in ("name", "description", "provider_apis", "default_provider", "default_model")}
                     for h in listing("/v1/harnesses")]
        providers = [{"name": p["name"], "api": p["api"]} for p in listing("/v1/providers")]
        return {"harnesses": harnesses, "providers": providers}

    # ── GET ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == "/":
                page = PAGE.replace("__DEFAULT_MODEL__", json.dumps(self.default_model)).encode()
                self._reply(200, page, "text/html; charset=utf-8", {
                    "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                               "img-src 'self' data: blob:; frame-src 'self'; object-src 'none'"})
            elif url.path == "/api/catalog":
                self._json(200, self.catalog())
            elif url.path == "/api/files":
                sid = (query.get("session") or [""])[0]
                if not SESSION.fullmatch(sid):
                    self._json(400, {"error": "a session id (cntr_...)"})
                    return
                files = self.workspace.files(sid, fresh=True)
                self._json(200, {"files": sorted(files.values(), key=lambda f: f["path"])})
            elif url.path.startswith("/ws/"):
                sid, _, rel = url.path[4:].partition("/")
                rel = unquote(rel)
                if not SESSION.fullmatch(sid) or not rel:
                    self._json(404, {"error": "not found"})
                    return
                found = self.workspace.files(sid).get(rel) or self.workspace.files(sid, fresh=True).get(rel)
                if not found:
                    self._reply(404, f"{rel} is not in the workspace (yet): files appear when a turn finishes".encode(),
                                "text/plain; charset=utf-8")
                    return
                self._file(rel, self.workspace.content(sid, found["id"]), "download" in query)
            elif url.path.startswith("/snippet/"):
                hit = self.snippets.get(url.path[9:])
                if not hit:
                    self._json(404, {"error": "not found"})
                    return
                self._file(hit[0], hit[1], "download" in query)
            else:
                self._json(404, {"error": "not found"})
        except httpx.HTTPStatusError as e:
            self._json(e.response.status_code, {"error": e.response.text[:500]})
        except Exception as e:                             # the platform is down or the key is wrong
            self._json(502, {"error": str(getattr(e, "message", e))})

    # ── POST ─────────────────────────────────────────────────────────────────────────────────────────────────────
    def do_POST(self) -> None:
        try:
            doc = self._body()
        except PermissionError as e:
            self._json(403, {"error": str(e)})
            return
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return
        if self.path == "/api/chat":
            self._chat(doc)
        elif self.path == "/api/cancel":
            with contextlib.suppress(Exception):
                self.client.responses.cancel(str(doc.get("id", "")))
            self._json(200, {"ok": True})
        elif self.path == "/api/attach":
            self._attach(doc)
        elif self.path == "/api/snippet":
            name = "snippet.svg" if doc.get("lang") == "svg" else "snippet.html"
            sid = uuid.uuid4().hex
            self.snippets[sid] = (name, str(doc.get("content", "")).encode()[:BODY_MAX])
            while len(self.snippets) > 100:
                self.snippets.popitem(last=False)
            self._json(200, {"id": sid, "url": f"/snippet/{sid}"})
        else:
            self._json(404, {"error": "not found"})

    def _attach(self, doc: dict) -> None:
        """A file for the next message: stored as an OpenAI file; the harness finds it in the sandbox."""
        try:
            data = base64.b64decode(str(doc.get("data", "")), validate=True)
        except (binascii.Error, ValueError):
            self._json(400, {"error": "data is base64"})
            return
        name = os.path.basename(str(doc.get("name") or "file"))[:200] or "file"
        if not 0 < len(data) <= ATTACH_MAX:
            self._json(400, {"error": f"a file of 1 byte to {ATTACH_MAX // 2**20} MB"})
            return
        r = self.http.post("/v1/files", files={"file": (name, data)}, data={"purpose": "user_data"})
        if r.status_code >= 400:
            self._json(r.status_code, {"error": r.text[:500]})
            return
        f = r.json()
        self._json(200, {"id": f["id"], "name": f.get("filename", name), "bytes": f.get("bytes", len(data))})

    def _send(self, event: dict) -> None:
        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
        self.wfile.flush()

    def _chat(self, doc: dict) -> None:
        """One turn, relayed to the page as server-sent events. If the page goes away, the turn is cancelled."""
        model, text, previous = str(doc.get("model") or self.default_model), str(doc.get("input") or ""), doc.get("previous")
        files = [f for f in doc.get("attachments") or [] if isinstance(f, str) and f.startswith("file")]
        request_input: object = text
        if files:
            request_input = [{"role": "user", "content": [{"type": "input_text", "text": text},
                                                          *({"type": "input_file", "file_id": f} for f in files)]}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        rid = None
        try:
            kwargs = {"previous_response_id": previous} if previous else {}
            for event in self.client.responses.create(model=model, input=request_input, stream=True, **kwargs):
                kind = event.type
                if kind == "response.created":
                    rid = event.response.id
                    self._send({"type": "start", "id": rid, "session": (event.response.metadata or {}).get("session_id")})
                elif kind == "response.output_text.delta":
                    self._send({"type": "text", "delta": event.delta})
                elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                    self._send({"type": "thinking", "delta": event.delta})
                elif kind == "response.output_item.done":
                    shown = describe(event.item.model_dump())
                    if shown:
                        self._send(shown)
                elif kind in ("response.completed", "response.failed", "response.incomplete"):
                    r = event.response
                    error = None
                    if r.status == "failed" and r.error:
                        error = f"{(r.metadata or {}).get('hg_error_code') or r.error.code}: {r.error.message}"
                    self._send({"type": "end", "status": r.status, "error": error})
            print(f"{model}: {rid}", file=sys.stderr)
        except (BrokenPipeError, ConnectionResetError):
            if rid:                                       # the page stopped or closed: stop the turn too
                with contextlib.suppress(Exception):
                    self.client.responses.cancel(rid)
        except Exception as e:                            # refused (unknown model, no provider, ...)
            with contextlib.suppress(Exception):
                self._send({"type": "end", "status": "failed", "error": str(getattr(e, "message", None) or e)})


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HarnessGateway</title>
<style>
:root { --bg:#ffffff; --side:#f9f9f9; --text:#0d0d0d; --muted:#8f8f8f; --soft:#5d5d5d; --line:#ececec; --bubble:#f4f4f4;
        --hover:#ececec; --card:#ffffff; --shadow:0 2px 12px rgba(0,0,0,.06), 0 0 0 1px rgba(0,0,0,.06);
        --accent:#0d0d0d; --err:#d92d20; --codebg:#0d0d0d; --codehead:#2f2f2f; --codetext:#e6e6e6; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#212121; --side:#171717; --text:#ececec; --muted:#8f8f8f; --soft:#b4b4b4; --line:#2f2f2f; --bubble:#303030;
          --hover:#2a2a2a; --card:#2a2a2a; --shadow:0 2px 12px rgba(0,0,0,.3), 0 0 0 1px rgba(255,255,255,.06);
          --accent:#ececec; --err:#f97066; --codebg:#0d0d0d; --codehead:#2a2a2a; --codetext:#e6e6e6; } }
* { box-sizing:border-box; }
html, body { height:100%; margin:0; }
body { display:flex; background:var(--bg); color:var(--text); overflow:hidden; -webkit-font-smoothing:antialiased;
       font:15.5px/1.65 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif; }
button { font:inherit; color:inherit; background:none; border:0; cursor:pointer; padding:0; }
svg { display:block; flex:none; }
::-webkit-scrollbar { width:10px; height:10px; }
::-webkit-scrollbar-thumb { background:var(--line); border-radius:10px; border:3px solid transparent; background-clip:content-box; }
.mono { font-family:ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
.ib { width:34px; height:34px; display:inline-flex; align-items:center; justify-content:center; border-radius:10px; color:var(--soft); }
.ib:hover { background:var(--hover); color:var(--text); }

/* sidebar */
aside { width:260px; flex:none; background:var(--side); display:flex; flex-direction:column; padding:10px 8px; }
.brand { display:flex; align-items:center; justify-content:space-between; padding:4px 6px 10px 10px; }
.brand b { font-weight:600; font-size:15px; letter-spacing:-.01em; }
.label { color:var(--muted); font-size:12px; font-weight:500; padding:14px 10px 6px; }
.chats { flex:1; overflow-y:auto; }
.chat { display:flex; align-items:center; gap:6px; padding:7px 10px; border-radius:10px; cursor:pointer; font-size:14px; }
.chat:hover { background:var(--hover); }
.chat.active { background:var(--hover); }
.chat span { flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.chat button { opacity:0; color:var(--muted); width:22px; height:22px; border-radius:6px; display:flex; align-items:center; justify-content:center; }
.chat:hover button { opacity:1; }
.chat button:hover { color:var(--text); background:var(--bg); }

/* main */
main { flex:1; display:flex; flex-direction:column; min-width:0; position:relative; }
header { display:flex; align-items:center; gap:8px; padding:8px 12px; height:56px; }
.route { display:flex; align-items:center; min-width:0; }
.route .sep { color:var(--muted); padding:0 1px; }
.pick { position:relative; display:flex; align-items:center; border-radius:10px; }
.pick:hover, .pick:focus-within { background:var(--hover); }
.pick select, .pick input { border:0; outline:0; background:transparent; color:var(--text); font-family:inherit; font-size:15px; padding:8px 10px; }
.pick select { appearance:none; -webkit-appearance:none; padding-right:26px; cursor:pointer; font-weight:600; max-width:200px; }
.pick select option { color:#0d0d0d; background:#fff; }
.pick svg { position:absolute; right:6px; top:50%; transform:translateY(-50%); color:var(--muted); pointer-events:none; }
.pick input { width:220px; }
.pick input::placeholder { color:var(--muted); }
.route.solo .provider, .route.solo .model, .route.solo .sep { display:none; }
header .session { margin-left:auto; color:var(--muted); font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
header .ib.on { background:var(--hover); color:var(--text); }
#log { flex:1; overflow-y:auto; }
.col { max-width:768px; margin:0 auto; padding:8px 24px 32px; }
main.blank { justify-content:center; }
main.blank #log { flex:none; overflow:visible; }
main.blank header { position:absolute; top:0; left:0; right:0; }
.hello { text-align:center; padding:0 24px 24px; }
.hello h1 { font-size:28px; font-weight:600; letter-spacing:-.02em; margin:0 0 6px; }
.hello p { color:var(--muted); margin:0; font-size:15px; }
.msg { margin:0 0 28px; animation:rise .25s ease-out; }
@keyframes rise { from { opacity:0; transform:translateY(4px); } to { opacity:1; transform:none; } }
.user { display:flex; flex-direction:column; align-items:flex-end; gap:8px; }
.user .bubble { background:var(--bubble); padding:10px 18px; border-radius:22px; max-width:75%; white-space:pre-wrap; word-wrap:break-word; }
.assistant { color:var(--text); }
.assistant p { margin:0 0 14px; }
.assistant p:last-child { margin-bottom:0; }
.assistant h3 { font-size:17px; font-weight:600; margin:22px 0 8px; }
.assistant ul, .assistant ol { margin:0 0 14px; padding-left:22px; }
.assistant li { margin:4px 0; }
.assistant a { color:var(--text); text-decoration:underline; text-underline-offset:3px; text-decoration-color:var(--muted); }
.assistant code { font:.875em ui-monospace, "SF Mono", Menlo, monospace; background:var(--bubble); padding:2px 6px; border-radius:6px; }
.cb { border-radius:12px; overflow:hidden; margin:0 0 16px; background:var(--codebg); }
.cbh { display:flex; justify-content:space-between; align-items:center; padding:6px 8px 6px 16px; background:var(--codehead);
       color:#b4b4b4; font-size:12px; }
.cbh span { display:flex; gap:2px; }
.cbh button { color:#b4b4b4; font-size:12px; display:inline-flex; align-items:center; gap:5px; padding:3px 8px; border-radius:6px; }
.cbh button:hover { color:#fff; background:#ffffff14; }
.cb pre { margin:0; padding:16px; overflow-x:auto; color:var(--codetext); font:13px/1.6 ui-monospace, "SF Mono", Menlo, monospace; }
.cb pre code { background:none; padding:0; color:inherit; font:inherit; }
details { margin:0 0 12px; }
summary { list-style:none; cursor:pointer; user-select:none; color:var(--muted); font-size:14px; display:inline-flex; align-items:center; gap:6px; }
summary::-webkit-details-marker { display:none; }
summary:hover { color:var(--text); }
summary .chev { transition:transform .15s; }
details[open] > summary .chev { transform:rotate(90deg); }
.shimmer { background:linear-gradient(90deg, var(--muted) 0%, var(--text) 50%, var(--muted) 100%); background-size:200% 100%;
           -webkit-background-clip:text; background-clip:text; color:transparent; animation:shimmer 1.6s linear infinite; }
@keyframes shimmer { from { background-position:100% 0; } to { background-position:-100% 0; } }
details .body { margin:8px 0 4px 6px; padding-left:16px; border-left:1px solid var(--line); color:var(--soft); font-size:14px; white-space:pre-wrap; }
.step { margin:6px 0 10px; white-space:normal; }
.step .label { display:flex; align-items:flex-start; gap:8px; font:13px ui-monospace, "SF Mono", Menlo, monospace; color:var(--text); word-break:break-all; }
.step .label svg { color:var(--muted); margin-top:2px; }
.step pre { margin:6px 0 0; padding:10px 12px; background:var(--bubble); border-radius:10px; max-height:240px; overflow:auto;
            font:12px/1.5 ui-monospace, "SF Mono", Menlo, monospace; white-space:pre-wrap; color:var(--soft); }
.link { cursor:pointer; text-decoration:underline; text-underline-offset:3px; text-decoration-color:var(--muted); }
.link:hover { text-decoration-color:var(--text); }
.tiles { display:flex; flex-wrap:wrap; gap:8px; margin:14px 0 0; }
.tile { display:flex; align-items:center; gap:10px; padding:8px 14px 8px 8px; border-radius:14px; background:var(--card);
        box-shadow:var(--shadow); cursor:pointer; text-align:left; max-width:280px; transition:transform .1s; }
.tile:hover { transform:translateY(-1px); }
.badge { width:36px; height:36px; border-radius:9px; display:flex; align-items:center; justify-content:center; color:#fff;
         font:700 9.5px/1 ui-sans-serif, -apple-system, sans-serif; letter-spacing:.02em; flex:none; }
.tile .nm { font-size:14px; font-weight:500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.tile .sub { color:var(--muted); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.attached { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:6px; }
.status { color:var(--muted); font-size:14px; }
.error { color:var(--err); font-size:14px; margin-top:6px; }
.dot::after { content:""; display:inline-block; width:9px; height:9px; margin-left:2px; border-radius:50%; background:var(--text);
              animation:pulse 1s ease-in-out infinite; vertical-align:middle; }
@keyframes pulse { 50% { transform:scale(.6); opacity:.5; } }

/* composer */
footer { padding:0 24px 18px; }
.composer { max-width:768px; margin:0 auto; border-radius:28px; background:var(--card); box-shadow:var(--shadow); padding:10px 10px 10px 12px; }
.composer:focus-within { box-shadow:0 2px 16px rgba(0,0,0,.08), 0 0 0 1px var(--muted); }
.pending { display:flex; flex-wrap:wrap; gap:8px; padding:4px 4px 10px; }
.pending:empty { display:none; }
.pending .tile { cursor:default; box-shadow:none; background:var(--bubble); padding:6px 8px 6px 6px; }
.pending .tile .badge { width:30px; height:30px; }
.pending .tile button { color:var(--muted); width:22px; height:22px; border-radius:50%; display:flex; align-items:center; justify-content:center; }
.pending .tile button:hover { color:var(--text); background:var(--hover); }
.composer .row { display:flex; align-items:flex-end; gap:6px; }
textarea { flex:1; border:0; outline:0; resize:none; background:transparent; color:var(--text); font:inherit; max-height:220px;
           padding:7px 4px; }
textarea::placeholder { color:var(--muted); }
.send { width:36px; height:36px; border-radius:50%; background:var(--accent); color:var(--bg); display:flex; align-items:center; justify-content:center; }
.send:disabled { opacity:.25; cursor:default; }
.hint { max-width:768px; margin:8px auto 0; text-align:center; color:var(--muted); font-size:12px; }
main.blank .hint { visibility:hidden; }

/* side panel */
#panel { width:46%; min-width:380px; max-width:920px; flex:none; display:flex; flex-direction:column; background:var(--bg);
         border-left:1px solid var(--line); }
#panel[hidden] { display:none; }
.ph { display:flex; align-items:center; gap:6px; padding:8px 8px 8px 12px; height:56px; }
.seg { display:flex; background:var(--bubble); border-radius:10px; padding:3px; }
.seg button { font-size:13px; padding:4px 12px; border-radius:8px; color:var(--soft); }
.seg button.on { background:var(--bg); color:var(--text); box-shadow:0 1px 3px rgba(0,0,0,.08); }
.ph .title { flex:1; min-width:0; padding-left:8px; font-size:13px; color:var(--soft); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.ph a.ib { text-decoration:none; }
#pbody { flex:1; overflow:auto; position:relative; border-top:1px solid var(--line); }
#pbody iframe { position:absolute; inset:0; width:100%; height:100%; border:0; background:#fff; }
#pbody .pad { padding:20px 24px; }
#pbody img { max-width:calc(100% - 48px); display:block; margin:24px auto; border-radius:8px;
             background:repeating-conic-gradient(#8881 0% 25%, transparent 0% 50%) 50% / 16px 16px; }
.files { padding:8px; }
.files .row { display:flex; align-items:center; gap:10px; padding:7px 10px; border-radius:10px; cursor:pointer; font-size:14px; }
.files .row:hover { background:var(--hover); }
.files .row .badge { width:26px; height:26px; border-radius:7px; font-size:8px; }
.files .row .nm { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.files .row .dir { color:var(--muted); }
.files .row .sz { color:var(--muted); font-size:12px; }
.files .row a { color:var(--muted); opacity:0; display:flex; padding:4px; border-radius:6px; }
.files .row:hover a { opacity:1; }
.files .row a:hover { color:var(--text); background:var(--bg); }
.note { padding:48px 24px; text-align:center; color:var(--muted); font-size:14px; }
.code { font:12.5px/1.6 ui-monospace, "SF Mono", Menlo, monospace; margin:0; padding:14px 0; counter-reset:ln; }
.code div { white-space:pre-wrap; word-break:break-all; padding:0 16px 0 60px; position:relative; }
.code div::before { counter-increment:ln; content:counter(ln); position:absolute; left:0; width:44px; text-align:right; color:var(--muted); opacity:.6; }
table.csv { border-collapse:collapse; font-size:13px; margin:16px; }
table.csv td, table.csv th { border-bottom:1px solid var(--line); padding:6px 14px 6px 0; text-align:left; white-space:nowrap; }
table.csv th { font-weight:600; }
@media (max-width:1100px) { aside { display:none; } }
@media (max-width:840px) { #panel { position:fixed; inset:0; width:auto; max-width:none; min-width:0; z-index:5; } .pick input { width:120px; } .pick select { max-width:120px; } }
</style>
</head>
<body>
<aside>
  <div class="brand"><b>HarnessGateway</b><button class="ib" id="new" title="New chat"></button></div>
  <div class="label">Chats</div>
  <div class="chats" id="chats"></div>
</aside>
<main id="main">
  <header>
    <div class="route" id="route">
      <label class="pick" title="Harness"><select id="harness"></select><span class="dd"></span></label>
      <span class="sep">/</span>
      <label class="pick provider" title="Provider"><select id="provider"></select><span class="dd"></span></label>
      <span class="sep">/</span>
      <label class="pick model" title="Model (empty: the harness's default)"><input id="model" list="models" spellcheck="false" autocomplete="off"></label>
      <datalist id="models"></datalist>
    </div>
    <span class="session mono" id="session"></span>
    <button class="ib" id="filesbtn" title="Workspace files"></button>
  </header>
  <div id="log"><div class="col" id="col"></div></div>
  <footer>
    <div class="composer">
      <div class="pending" id="pending"></div>
      <div class="row">
        <button class="ib" id="attach" title="Attach files"></button>
        <input type="file" id="picker" multiple hidden>
        <textarea id="input" rows="1" placeholder="Ask the agent to build something"></textarea>
        <button class="send" id="send" title="Send"></button>
      </div>
    </div>
    <div class="hint">Each chat is one sandbox session. Its files and the agent's memory carry from message to message.</div>
  </footer>
</main>
<section id="panel" hidden>
  <div class="ph">
    <div class="seg"><button id="tabfiles">Files</button><button id="tabview">Preview</button></div>
    <span class="title mono" id="ptitle"></span>
    <a class="ib" id="pnew" target="_blank" rel="noopener" title="Open in a new tab"></a>
    <a class="ib" id="pdl" title="Download"></a>
    <button class="ib" id="prefresh" title="Refresh"></button>
    <button class="ib" id="pclose" title="Close"></button>
  </div>
  <div id="pbody"></div>
</section>
<script>
const DEFAULT_MODEL = __DEFAULT_MODEL__;
const STORE = "hg-chatgui";
const $ = id => document.getElementById(id);
let chats = [], current = null, running = null, pending = [], files = [];
const panel = { open: false, tab: "files", view: null };   // view: {kind: "file", path} | {kind: "snippet", url, lang}

// ── icons ───────────────────────────────────────────────────────────────────────────────────────────────────
const ic = (d, s = 18) => `<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
const I = {
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  clip: '<path d="m21.4 11.1-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5"/>',
  up: '<path d="M12 19V5"/><path d="m5 12 7-7 7 7"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" stroke="none"/>',
  panel: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M14 4v16"/>',
  ext: '<path d="M14 4h6v6"/><path d="M20 4 10 14"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
  down: '<path d="M12 4v11"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  chev: '<path d="m9 6 6 6-6 6"/>',
  down2: '<path d="m6 9 6 6 6-6"/>',
  term: '<path d="m5 8 4 4-4 4"/><path d="M12 17h7"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z"/><path d="M14 3v5h5"/>',
  tool: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17l3 3 5.3-5.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4Z"/>',
  search: '<circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  play: '<path d="M7 5v14l12-7Z"/>',
};

// ── storage ─────────────────────────────────────────────────────────────────────────────────────────────────
function load() { try { chats = JSON.parse(localStorage.getItem(STORE)) || []; } catch { chats = []; } }
function save() { try { localStorage.setItem(STORE, JSON.stringify(chats)); } catch {} }
function chat() { return chats.find(c => c.id === current); }
const newId = () => Date.now().toString(36) + Math.random().toString(36).slice(2);

// ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────────
const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const size = n => n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB";
const ext = p => (p.split("/").pop().includes(".") ? p.split(".").pop().toLowerCase() : "");
const rel = p => String(p || "").replace(/^\/workspace\//, "").replace(/^\.\//, "");
const wsUrl = (sid, path) => `/ws/${sid}/` + path.split("/").map(encodeURIComponent).join("/");
const IMAGES = ["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif", "svg"];
const SHOWN = ["html", "htm", "svg", "md", "markdown", "png", "jpg", "jpeg", "gif", "webp", "pdf"];
const KINDS = [[["html", "htm"], "#e8590c"], [["css", "scss"], "#1c7ed6"], [["js", "mjs", "ts", "tsx", "jsx"], "#e0a800"],
  [["py"], "#3572a5"], [IMAGES, "#7048e8"], [["pdf"], "#e03131"], [["md", "markdown", "txt"], "#868e96"],
  [["csv", "tsv", "json", "xlsx"], "#2b8a3e"], [["sh", "bash", "zsh"], "#495057"]];
function badge(p) {
  const e = ext(p), kind = KINDS.find(([list]) => list.includes(e));
  return `<span class="badge" style="background:${kind ? kind[1] : "#6c757d"}">${esc((e || "file").slice(0, 4).toUpperCase())}</span>`;
}
function copyText(text) {                            // works on plain http too, where navigator.clipboard is absent
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select();
  try { document.execCommand("copy"); } finally { t.remove(); }
}
async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || r.statusText);
  return d;
}

// ── markdown (small and safe: escapes everything, then adds a few constructs) ──────────────────────────────
function inline(s) {
  return esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function blocks(text) {
  let html = "", list = null, para = [];
  const flushPara = () => { if (para.length) { html += "<p>" + para.map(inline).join("<br>") + "</p>"; para = []; } };
  const flushList = () => { if (list) { html += `<${list.tag}>` + list.items.map(i => `<li>${inline(i)}</li>`).join("") + `</${list.tag}>`; list = null; } };
  for (const line of text.split("\n")) {
    const h = line.match(/^(#{1,6})\s+(.*)/), ul = line.match(/^\s*[-*]\s+(.*)/), ol = line.match(/^\s*\d+[.)]\s+(.*)/);
    if (!line.trim()) { flushPara(); flushList(); }
    else if (h) { flushPara(); flushList(); html += `<h3>${inline(h[2])}</h3>`; }
    else if (ul || ol) { flushPara(); const tag = ul ? "ul" : "ol"; if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; } list.items.push((ul || ol)[1]); }
    else { flushList(); para.push(line); }
  }
  flushPara(); flushList();
  return html;
}
function markdown(src) {
  return String(src).split("```").map((part, i) => {
    if (i % 2 === 0) return blocks(part);
    const nl = part.indexOf("\n"), lang = (nl >= 0 ? part.slice(0, nl) : "").trim().toLowerCase();
    const code = (nl >= 0 ? part.slice(nl + 1) : part).replace(/\n$/, "");
    const preview = ["html", "svg", "xml"].includes(lang) || (!lang && /^\s*<(!doctype|html|svg)/i.test(code));
    return `<div class="cb" data-lang="${esc(lang)}"><div class="cbh"><span>${esc(lang || "code")}</span><span>` +
      (preview ? `<button data-act="preview">${ic(I.play, 13)}Preview</button>` : "") + `<button data-act="copy">${ic(I.copy, 13)}<span>Copy</span></button></span></div>` +
      `<pre><code>${esc(code)}</code></pre></div>`;
  }).join("");
}

// ── the conversation ────────────────────────────────────────────────────────────────────────────────────────
const STEP_ICON = { terminal: I.term, file: I.file, tool: I.tool, search: I.search };
function tiles(paths) {
  const known = Object.fromEntries(files.map(f => [f.path, f]));
  return `<div class="tiles">` + paths.map(p => `<button class="tile" data-open="${esc(p)}">${badge(p)}<span style="min-width:0">` +
    `<div class="nm">${esc(p.split("/").pop())}</div><div class="sub">${esc(p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "workspace")}` +
    `${known[p] ? " · " + size(known[p].bytes) : ""}</div></span></button>`).join("") + `</div>`;
}
function stepSummary(steps) {
  const n = k => steps.filter(s => s.icon === k).length, parts = [];
  if (n("terminal")) parts.push(`ran ${n("terminal")} command${n("terminal") > 1 ? "s" : ""}`);
  const edits = steps.filter(s => s.icon === "file");
  if (edits.length) parts.push(`${edits.every(s => s.verb === "Created") ? "created" : "edited"} ${edits.length} file${edits.length > 1 ? "s" : ""}`);
  if (n("tool")) parts.push(`used ${n("tool")} tool${n("tool") > 1 ? "s" : ""}`);
  if (n("search")) parts.push("searched the web");
  const s = parts.join(", ") || `${steps.length} steps`;
  return s[0].toUpperCase() + s.slice(1);
}
function renderAssistant(turn, live, ti) {
  let html = "";
  turn.segments.forEach((seg, si) => {
    const at = `data-t="${ti}" data-s="${si}"`, last = seg === turn.segments.at(-1);
    if (seg.t === "thinking") {
      const thinking = live && last && !seg.ended;
      const secs = seg.started && seg.ended ? Math.max(1, Math.round((seg.ended - seg.started) / 1000)) : 0;
      const label = thinking ? `<span class="shimmer">Thinking</span>` : secs ? `Thought for ${secs}s` : "Thought";
      html += `<details ${at}${seg.open ? " open" : ""}><summary>${label}<span class="chev">${ic(I.chev, 14)}</span></summary><div class="body">${esc(seg.text)}</div></details>`;
    } else if (seg.t === "steps") {
      const working = live && last;
      html += `<details ${at}${seg.open ? " open" : ""}><summary>${working ? `<span class="shimmer">${esc(stepSummary(seg.steps))}</span>` : esc(stepSummary(seg.steps))}<span class="chev">${ic(I.chev, 14)}</span></summary><div class="body">` +
        seg.steps.map(s => {
          const label = s.path ? `${esc(s.verb)} <span class="link" data-open="${esc(rel(s.path))}">${esc(s.path)}</span>` : esc(s.label);
          return `<div class="step"><div class="label">${ic(STEP_ICON[s.icon] || I.tool, 14)}<span>${label}</span></div>` +
            (s.output || s.detail ? `<pre>${esc((s.output || s.detail).slice(0, 20000))}</pre>` : "") +
            (s.exit_code ? `<div class="error">exit ${s.exit_code}</div>` : "") + `</div>`;
        }).join("") + `</div></details>`;
    } else if (seg.t === "text") {
      html += markdown(seg.text);
    }
  });
  if (turn.files && turn.files.length) html += tiles(turn.files);
  if (turn.error) html += `<div class="error">${esc(turn.error)}</div>`;
  else if (turn.status && turn.status !== "completed") html += `<div class="status">${esc(turn.status === "cancelled" ? "Stopped" : turn.status)}</div>`;
  if (live && (!turn.segments.length || turn.segments.at(-1).t === "text")) html += `<span class="dot"></span>`;
  return html;
}
function render() {
  const c = chat(), col = $("col"), blank = !c || !c.turns.length;
  $("main").classList.toggle("blank", blank);
  $("chats").innerHTML = chats.map(x => `<div class="chat${x.id === current ? " active" : ""}" data-id="${x.id}"><span>${esc(x.title)}</span><button data-del="${x.id}" title="Delete">${ic(I.close, 14)}</button></div>`).join("");
  $("session").textContent = c && c.session ? c.session : "";
  if (shownChat !== current) { shownChat = current; if (c) setPick(c.model); }
  if (blank) col.innerHTML = `<div class="hello"><h1>What should we build?</h1><p>The agent works in its own sandbox. What it makes opens on the right.</p></div>`;
  else col.innerHTML = c.turns.map((t, i) => t.role === "user"
    ? `<div class="msg user"><div class="bubble">${esc(t.text)}</div>` + (t.attachments && t.attachments.length ? `<div class="attached">` + t.attachments.map(a => `<span class="tile" style="cursor:default">${badge(a.name)}<span class="nm">${esc(a.name)}</span></span>`).join("") + `</div>` : "") + `</div>`
    : `<div class="msg assistant" id="t${i}">${renderAssistant(t, running && i === c.turns.length - 1, i)}</div>`).join("");
  $("log").scrollTop = $("log").scrollHeight;
  renderPanel();
}
let frame = 0;
function renderLive() {                               // at most once a frame while streaming
  if (frame) return;
  frame = requestAnimationFrame(() => {
    frame = 0;
    const c = chat(); if (!c) return;
    const i = c.turns.length - 1, el = $("t" + i), log = $("log");
    const stick = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    if (el) el.innerHTML = renderAssistant(c.turns[i], !!running, i); else render();
    if (stick) log.scrollTop = log.scrollHeight;
  });
}

// ── the side panel: workspace files and the preview ────────────────────────────────────────────────────────
async function refreshFiles() {
  const c = chat();
  if (!c || !c.session) { files = []; return; }
  try { files = (await (await fetch(`/api/files?session=${encodeURIComponent(c.session)}`)).json()).files || []; }
  catch { files = []; }
}
function openPanel(tab, view) { panel.open = true; panel.tab = tab; if (view) panel.view = view; renderPanel(); }
function renderPanel() {
  const c = chat();
  $("panel").hidden = !panel.open; $("filesbtn").classList.toggle("on", panel.open);
  if (!panel.open) return;
  const showFiles = panel.tab === "files" || !panel.view;
  $("tabfiles").classList.toggle("on", showFiles); $("tabview").classList.toggle("on", !showFiles);
  $("tabview").hidden = !panel.view;
  const body = $("pbody"), sid = c && c.session;
  if (showFiles) {
    $("ptitle").textContent = sid && files.length ? `${files.length} file${files.length === 1 ? "" : "s"}` : "";
    $("pnew").hidden = $("pdl").hidden = true;
    if (!sid) { body.innerHTML = `<div class="note">Start a chat. Its workspace files show here.</div>`; return; }
    if (!files.length) { body.innerHTML = `<div class="note">No files yet. They appear here when a turn finishes.</div>`; return; }
    body.innerHTML = `<div class="files">` + files.map(f => {
      const dir = f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/") + 1) : "";
      return `<div class="row" data-open="${esc(f.path)}">${badge(f.path)}<span class="nm"><span class="dir">${esc(dir)}</span>${esc(f.path.slice(dir.length))}</span>` +
        `<span class="sz">${size(f.bytes)}</span><a href="${wsUrl(sid, f.path)}?download=1" title="Download" data-stop="1">${ic(I.down, 15)}</a></div>`;
    }).join("") + `</div>`;
    return;
  }
  const v = panel.view;
  if (v.kind === "snippet") {
    $("ptitle").textContent = `${v.lang || "code"} preview`;
    $("pnew").hidden = $("pdl").hidden = false; $("pnew").href = v.url; $("pdl").href = v.url + "?download=1";
    body.innerHTML = `<iframe sandbox="allow-scripts allow-forms allow-popups allow-modals" src="${esc(v.url)}"></iframe>`;
    return;
  }
  if (!sid) return;
  const url = wsUrl(sid, v.path), e = ext(v.path), f = files.find(x => x.path === v.path);
  $("ptitle").textContent = v.path; $("pnew").hidden = $("pdl").hidden = false;
  $("pnew").href = url; $("pdl").href = url + "?download=1";
  if (!f) { body.innerHTML = `<div class="note">${esc(v.path)} is not in the workspace yet. Files appear when a turn finishes.</div>`; return; }
  if (["html", "htm"].includes(e)) body.innerHTML = `<iframe sandbox="allow-scripts allow-forms allow-popups allow-modals allow-downloads" src="${url}"></iframe>`;
  else if (e === "pdf") body.innerHTML = `<iframe src="${url}"></iframe>`;
  else if (IMAGES.includes(e)) body.innerHTML = `<img src="${url}" alt="${esc(v.path)}">`;
  else if (f.bytes > 2 * 1048576) body.innerHTML = `<div class="note">${size(f.bytes)} is too large to preview. Download it instead.</div>`;
  else {
    body.innerHTML = `<div class="note">Loading…</div>`;
    fetch(url).then(r => r.text()).then(text => {
      if (panel.view !== v) return;
      if (text.includes("\u0000")) body.innerHTML = `<div class="note">Binary file, ${size(f.bytes)}. Download it instead.</div>`;
      else if (["md", "markdown"].includes(e)) body.innerHTML = `<div class="pad assistant">${markdown(text)}</div>`;
      else if (["csv", "tsv"].includes(e)) body.innerHTML = csvTable(text, e === "tsv" ? "\t" : ",");
      else {
        let shown = text;
        if (e === "json") { try { shown = JSON.stringify(JSON.parse(text), null, 2); } catch {} }
        body.innerHTML = `<div class="code">` + shown.split("\n").map(l => `<div>${esc(l) || " "}</div>`).join("") + `</div>`;
      }
    }).catch(err => { body.innerHTML = `<div class="note error">${esc(err)}</div>`; });
  }
}
function csvTable(text, sep) {
  const rows = [];
  for (const line of text.split(/\r?\n/).slice(0, 501)) {
    if (!line) continue;
    const cells = []; let cur = "", q = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (q) { if (ch === '"' && line[i + 1] === '"') { cur += '"'; i++; } else if (ch === '"') q = false; else cur += ch; }
      else if (ch === '"') q = true; else if (ch === sep) { cells.push(cur); cur = ""; } else cur += ch;
    }
    cells.push(cur); rows.push(cells);
  }
  if (!rows.length) return `<div class="note">Empty.</div>`;
  return `<table class="csv"><tr>${rows[0].map(c => `<th>${esc(c)}</th>`).join("")}</tr>` +
    rows.slice(1).map(r => `<tr>${r.map(c => `<td>${esc(c)}</td>`).join("")}</tr>`).join("") + `</table>`;
}
function openFile(path) { openPanel("view", { kind: "file", path: rel(path) }); }

// ── a turn ──────────────────────────────────────────────────────────────────────────────────────────────────
function apply(turn, ev) {
  let last = turn.segments.at(-1);
  if (last && last.t === "thinking" && !last.ended && ev.type !== "thinking") last.ended = Date.now();
  if (ev.type === "text") { if (last && last.t === "text") last.text += ev.delta; else turn.segments.push({ t: "text", text: ev.delta }); }
  else if (ev.type === "thinking") { if (last && last.t === "thinking") last.text += ev.delta; else turn.segments.push({ t: "thinking", text: ev.delta, started: Date.now() }); }
  else if (ev.type === "tool") {
    const step = { id: ev.id, icon: ev.icon, label: ev.label, verb: ev.verb, path: ev.path, detail: ev.detail || "", output: ev.output || "" };
    if (last && last.t === "steps") last.steps.push(step); else turn.segments.push({ t: "steps", steps: [step] });
  } else if (ev.type === "tool_output") {
    for (const seg of turn.segments) if (seg.t === "steps") for (const s of seg.steps) if (s.id === ev.id) { s.output = ev.text; s.exit_code = ev.exit_code; }
  } else if (ev.type === "end") { turn.status = ev.status; turn.error = ev.error; }
}
async function afterTurn(c, turn, before) {
  // The workspace as the turn left it: tiles for what it made or changed, and a page it built opens in the panel.
  await refreshFiles();
  if (chat() !== c) return;
  const edited = new Set();
  for (const seg of turn.segments) if (seg.t === "steps") for (const s of seg.steps) if (s.path && s.verb !== "Deleted") edited.add(rel(s.path));
  for (const f of files) { const b = before[f.path]; if (!b || b.bytes !== f.bytes || b.at !== f.at) edited.add(f.path); }
  turn.files = files.map(f => f.path).filter(p => edited.has(p)).slice(0, 12);
  const show = turn.files.find(p => ["html", "htm", "svg"].includes(ext(p))) || turn.files.find(p => SHOWN.includes(ext(p)));
  if (show && turn.status === "completed") openFile(show);
  save(); render();
}
async function send() {
  const text = $("input").value.trim();
  if (!text || running || pending.some(p => p.uploading)) return;
  let c = chat();
  const model = pickedModel(), picked = { provider: $("provider").value, model: $("model").value.trim() };
  if (!c || (c.model !== model && c.turns.length)) {
    c = { id: newId(), title: text.slice(0, 60), model, previous: null, session: null, turns: [] };
    chats.unshift(c); current = c.id; files = [];
  }
  c.model = model;
  const attachments = pending.filter(p => p.id).map(p => ({ id: p.id, name: p.name })); pending = []; renderPending();
  c.turns.push({ role: "user", text, attachments });
  const turn = { role: "assistant", segments: [], status: null, error: null };
  c.turns.push(turn);
  const before = Object.fromEntries(files.map(f => [f.path, f]));
  $("input").value = ""; grow();
  running = { controller: new AbortController(), id: null };
  setBusy(true); render(); save();
  try {
    const res = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model, input: text, previous: c.previous, attachments: attachments.map(a => a.id) }), signal: running.controller.signal });
    const reader = res.body.getReader(), decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const frameText = buffer.slice(0, cut); buffer = buffer.slice(cut + 2);
        if (!frameText.startsWith("data: ")) continue;
        const ev = JSON.parse(frameText.slice(6));
        if (ev.type === "start") { running.id = ev.id; c.previous = ev.id; c.session = ev.session || c.session; $("session").textContent = c.session || ""; }
        else apply(turn, ev);
        renderLive();
      }
    }
  } catch (e) {
    if (e.name === "AbortError") turn.status = "cancelled"; else turn.error = String(e);
  }
  for (const seg of turn.segments) if (seg.t === "thinking" && !seg.ended) seg.ended = Date.now();
  running = null; setBusy(false); save(); render();
  if (turn.status === "completed") remember(picked.provider, picked.model);
  if (c.session) await afterTurn(c, turn, before);
}
function stop() {
  if (!running) return;
  if (running.id) post("/api/cancel", { id: running.id }).catch(() => {});
  running.controller.abort();
}
function setBusy(busy) { $("send").innerHTML = ic(busy ? I.stop : I.up, busy ? 18 : 18); $("send").title = busy ? "Stop" : "Send"; }

// ── attachments ─────────────────────────────────────────────────────────────────────────────────────────────
function renderPending() {
  $("pending").innerHTML = pending.map((p, i) => `<span class="tile">${badge(p.name)}<span style="min-width:0"><div class="nm">${esc(p.name)}</div>` +
    `<div class="sub">${p.uploading ? "Uploading…" : p.error ? esc(p.error) : size(p.bytes)}</div></span><button data-unpend="${i}" title="Remove">${ic(I.close, 13)}</button></span>`).join("");
}
function attach(list) {
  for (const file of list) {
    const item = { name: file.name, bytes: file.size, uploading: true };
    pending.push(item); renderPending();
    if (file.size > 20 * 1048576) { item.uploading = false; item.error = "Over 20 MB"; renderPending(); continue; }
    const reader = new FileReader();
    reader.onload = () => {
      post("/api/attach", { name: file.name, data: String(reader.result).split(",")[1] || "" })
        .then(d => { item.id = d.id; item.uploading = false; })
        .catch(e => { item.uploading = false; item.error = e.message; })
        .finally(renderPending);
    };
    reader.readAsDataURL(file);
  }
}

// ── wiring ──────────────────────────────────────────────────────────────────────────────────────────────────
$("new").innerHTML = ic(I.edit); $("attach").innerHTML = ic(I.clip); $("filesbtn").innerHTML = ic(I.panel); document.querySelectorAll(".dd").forEach(e => e.innerHTML = ic(I.down2, 16));
$("pnew").innerHTML = ic(I.ext, 16); $("pdl").innerHTML = ic(I.down, 16); $("prefresh").innerHTML = ic(I.refresh, 16); $("pclose").innerHTML = ic(I.close, 16);
setBusy(false);
function grow() { const t = $("input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 220) + "px"; }
$("input").addEventListener("input", grow);
$("input").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
$("input").addEventListener("paste", e => { if (e.clipboardData && e.clipboardData.files.length) { e.preventDefault(); attach(e.clipboardData.files); } });
$("send").addEventListener("click", () => running ? stop() : send());
$("attach").addEventListener("click", () => $("picker").click());
$("picker").addEventListener("change", e => { attach(e.target.files); e.target.value = ""; });
$("pending").addEventListener("click", e => { const b = e.target.closest("[data-unpend]"); if (b) { pending.splice(+b.dataset.unpend, 1); renderPending(); } });
document.addEventListener("dragover", e => e.preventDefault());
document.addEventListener("drop", e => { e.preventDefault(); if (e.dataTransfer && e.dataTransfer.files.length) attach(e.dataTransfer.files); });
$("col").addEventListener("toggle", e => {             // a section the reader opens or closes stays that way
  const d = e.target, c = chat();
  if (!c || d.tagName !== "DETAILS" || d.dataset.t === undefined) return;
  const seg = c.turns[+d.dataset.t]?.segments[+d.dataset.s];
  if (seg && seg.open !== d.open) { seg.open = d.open; save(); }
}, true);
$("col").addEventListener("click", e => {
  const open = e.target.closest("[data-open]");
  if (open) { e.preventDefault(); openFile(open.dataset.open); return; }
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const block = btn.closest(".cb"), code = block.querySelector("code").textContent;
  if (btn.dataset.act === "copy") { copyText(code); const t = btn.querySelector("span"); t.textContent = "Copied"; setTimeout(() => { t.textContent = "Copy"; }, 1200); }
  if (btn.dataset.act === "preview") post("/api/snippet", { content: code, lang: block.dataset.lang }).then(d => openPanel("view", { kind: "snippet", url: d.url, lang: block.dataset.lang }));
});
$("pbody").addEventListener("click", e => {
  if (e.target.closest("[data-stop]")) return;
  const row = e.target.closest("[data-open]"); if (row) openFile(row.dataset.open);
});
$("filesbtn").addEventListener("click", async () => { if (panel.open) { panel.open = false; renderPanel(); return; } await refreshFiles(); openPanel("files"); });
$("tabfiles").addEventListener("click", async () => { await refreshFiles(); openPanel("files"); });
$("tabview").addEventListener("click", () => openPanel("view"));
$("prefresh").addEventListener("click", async () => { await refreshFiles(); renderPanel(); });
$("pclose").addEventListener("click", () => { panel.open = false; renderPanel(); });
$("new").addEventListener("click", () => { if (running) return; current = null; files = []; panel.view = null; render(); $("input").focus(); });
$("chats").addEventListener("click", async e => {
  if (running) return;
  const del = e.target.closest("[data-del]");
  if (del) { chats = chats.filter(c => c.id !== del.dataset.del); if (current === del.dataset.del) { current = null; files = []; panel.view = null; } save(); render(); return; }
  const row = e.target.closest(".chat"); if (!row) return;
  current = row.dataset.id; panel.view = null; panel.tab = "files"; render(); await refreshFiles(); renderPanel();
});
// ── the picker: harness / provider / model, from what the organization has ─────────────────────────────────────
const MODELS = "hg-chatgui-models";                   // models you've used, per provider, offered again
let catalog = { harnesses: [], providers: [] }, shownChat;
function used() { try { return JSON.parse(localStorage.getItem(MODELS)) || {}; } catch { return {}; } }
function remember(provider, model) {
  if (!provider || !model) return;
  const u = used(); u[provider] = [model, ...(u[provider] || []).filter(m => m !== model)].slice(0, 12);
  try { localStorage.setItem(MODELS, JSON.stringify(u)); } catch {}
}
function harnessOf(name) { return catalog.harnesses.find(h => h.name === name); }
function options(sel, names, value) {
  if (value && !names.includes(value)) names = [value, ...names];   // a chat's model the catalog no longer has
  sel.innerHTML = names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  sel.value = value || names[0] || "";
}
function fillProviders(want, keep) {        // keep: a chat's own provider stays even if the catalog lacks it
  const h = harnessOf($("harness").value), apis = h ? h.provider_apis || [] : null;
  const fits = catalog.providers.filter(p => !apis || apis.includes(p.api)).map(p => p.name);
  $("route").classList.toggle("solo", !!h && !apis.length);      // a harness that calls no model (shell)
  const pick = [want, h && h.default_provider].find(n => n && fits.includes(n)) || (keep && want) || fits[0];
  options($("provider"), fits, pick);
  fillModels();
}
function fillModels() {
  const h = harnessOf($("harness").value), def = h ? h.default_model || "" : "";
  $("model").placeholder = def ? `${def} (default)` : "model";
  const list = [...new Set([def, ...(used()[$("provider").value] || [])].filter(Boolean))];
  $("models").innerHTML = list.map(m => `<option value="${esc(m)}">`).join("");
}
function setPick(id) {
  const [harness = "", provider = "", ...rest] = (id || "").split("/"), model = rest.join("/");   // the model may hold a /
  options($("harness"), catalog.harnesses.map(h => h.name), harness);
  fillProviders(provider, true);
  $("model").value = model;
}
function pickedModel() {
  const h = harnessOf($("harness").value), harness = $("harness").value;
  if (h && !(h.provider_apis || []).length) return harness;
  const provider = $("provider").value, model = $("model").value.trim();
  return [harness, provider, model].filter(Boolean).join("/") || DEFAULT_MODEL;
}
$("harness").addEventListener("change", () => { fillProviders($("provider").value); $("model").value = ""; });
$("provider").addEventListener("change", fillModels);
fetch("/api/catalog").then(r => r.json()).then(d => {
  if (d.harnesses) catalog = d;
  const c = chat(); setPick(c ? c.model : pickedModel());
}).catch(() => {});
load(); current = chats[0] ? chats[0].id : null; setPick(DEFAULT_MODEL); render(); refreshFiles().then(renderPanel); $("input").focus();
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.environ.get("HG_API", "http://localhost:8000"))
    ap.add_argument("--key", default=os.environ.get("HG_KEY", ""))
    ap.add_argument("--model", default=os.environ.get("HG_MODEL", "claude-code/deepseek/deepseek-flash"))
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to reach it from outside this machine (it holds your key)")
    ap.add_argument("--port", type=int, default=8800)
    args = ap.parse_args()
    if not args.key:
        sys.exit("set HG_KEY (or pass --key) to your organization's sk-hg-... key")
    api = args.api.rstrip("/")
    Handler.client = OpenAI(base_url=f"{api}/v1", api_key=args.key, timeout=3600, max_retries=0)
    Handler.http = httpx.Client(base_url=api, headers={"Authorization": f"Bearer {args.key}"}, timeout=120)
    Handler.workspace = Workspace(Handler.http)
    Handler.default_model = args.model
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"HarnessGateway chat on http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
          f" (API {api}, default model {args.model}); Ctrl+C to stop", file=sys.stderr)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
