"""A ChatGPT-style page for talking to a harness, with artifacts: conversations in a sidebar, replies streamed with
markdown, the agent's thinking and steps folded under each reply, and a side panel with the session's workspace files
(browse, preview, download) that opens what the agent builds: HTML pages run live, images, PDFs, markdown, CSV and
code render in place. Files you attach go to the agent with your message.

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
            elif url.path == "/api/models":
                self._json(200, {"models": sorted(m.id for m in self.client.models.list())})
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
<title>HarnessGateway Chat</title>
<style>
:root { --bg:#ffffff; --side:#f7f7f8; --text:#1f2328; --muted:#6b7280; --line:#e5e7eb; --bubble:#f1f3f5;
        --accent:#10a37f; --code:#f6f8fa; --err:#b42318; --hover:#ececf1; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#212121; --side:#171717; --text:#ececec; --muted:#9b9b9b; --line:#333; --bubble:#2f2f2f;
          --accent:#19c37d; --code:#1a1a1a; --err:#f97066; --hover:#2a2a2a; } }
* { box-sizing:border-box; }
html, body { height:100%; margin:0; }
body { display:flex; background:var(--bg); color:var(--text); font:15px/1.6 -apple-system, BlinkMacSystemFont,
       "Segoe UI", Roboto, sans-serif; overflow:hidden; }
button { font:inherit; color:inherit; }
aside { width:260px; flex:none; background:var(--side); border-right:1px solid var(--line); display:flex; flex-direction:column; }
aside .new { margin:12px; padding:10px 12px; border:1px solid var(--line); border-radius:10px; background:transparent;
             text-align:left; cursor:pointer; }
aside .new:hover, .chat:hover { background:var(--hover); }
.chats { flex:1; overflow-y:auto; padding:0 8px 12px; }
.chat { display:flex; align-items:center; gap:6px; padding:8px 10px; border-radius:8px; cursor:pointer; }
.chat.active { background:var(--bubble); }
.chat span { flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.chat button { visibility:hidden; border:0; background:none; color:var(--muted); cursor:pointer; font-size:16px; }
.chat:hover button { visibility:visible; }
main { flex:1; display:flex; flex-direction:column; min-width:0; }
header { display:flex; align-items:center; gap:10px; padding:10px 16px; border-bottom:1px solid var(--line); }
header strong { font-weight:600; white-space:nowrap; }
header input { flex:0 1 340px; min-width:120px; padding:6px 10px; border:1px solid var(--line); border-radius:8px;
               background:var(--bg); color:var(--text); font:13px ui-monospace, Menlo, monospace; }
header .session { margin-left:auto; color:var(--muted); font:12px ui-monospace, Menlo, monospace; white-space:nowrap;
                  overflow:hidden; text-overflow:ellipsis; }
.tbtn { border:1px solid var(--line); background:transparent; border-radius:8px; padding:5px 10px; cursor:pointer;
        white-space:nowrap; font-size:13px; }
.tbtn:hover, .tbtn.on { background:var(--hover); }
#log { flex:1; overflow-y:auto; }
.col { max-width:780px; margin:0 auto; padding:24px 16px; }
.empty { margin-top:20vh; text-align:center; color:var(--muted); }
.empty h1 { color:var(--text); font-weight:600; font-size:28px; margin:0 0 8px; }
.msg { margin:0 0 24px; }
.user { display:flex; flex-direction:column; align-items:flex-end; gap:6px; }
.user .bubble { background:var(--bubble); padding:10px 16px; border-radius:18px; max-width:80%; white-space:pre-wrap;
                word-wrap:break-word; }
.assistant p { margin:0 0 12px; }
.assistant p:last-child { margin-bottom:0; }
.assistant code { font:0.9em ui-monospace, Menlo, monospace; background:var(--code); padding:1px 5px; border-radius:5px; }
.assistant a { color:var(--accent); }
.assistant h1, .assistant h2, .assistant h3 { font-size:1.1em; margin:16px 0 8px; }
.assistant ul, .assistant ol { margin:0 0 12px; padding-left:24px; }
.cb { border:1px solid var(--line); border-radius:10px; margin:0 0 12px; overflow:hidden; }
.cbh { display:flex; justify-content:space-between; align-items:center; padding:4px 8px 4px 12px; background:var(--bubble);
       color:var(--muted); font-size:12px; }
.cbh button { border:0; background:none; color:var(--muted); cursor:pointer; font-size:12px; padding:2px 6px; border-radius:6px; }
.cbh button:hover { background:var(--hover); color:var(--text); }
.cb pre { margin:0; padding:12px; background:var(--code); overflow-x:auto; font:13px/1.5 ui-monospace, Menlo, monospace; }
.cb pre code { background:none; padding:0; }
details { margin:0 0 10px; color:var(--muted); font-size:14px; }
summary { cursor:pointer; user-select:none; }
details .body { margin:6px 0 0 4px; padding-left:12px; border-left:2px solid var(--line); white-space:pre-wrap; }
.step { margin:4px 0; }
.step .label { font:13px ui-monospace, Menlo, monospace; color:var(--text); }
.step pre { margin:4px 0 0; padding:8px; background:var(--code); border-radius:8px; max-height:220px; overflow:auto;
            font:12px/1.45 ui-monospace, Menlo, monospace; white-space:pre-wrap; }
.link { color:var(--accent); cursor:pointer; text-decoration:underline; text-underline-offset:2px; }
.chips { display:flex; flex-wrap:wrap; gap:8px; margin:8px 0 0; }
.chip { display:inline-flex; align-items:center; gap:8px; border:1px solid var(--line); border-radius:12px; padding:8px 12px;
        background:var(--bg); cursor:pointer; font-size:13px; max-width:100%; }
.chip:hover { background:var(--hover); }
.chip .ico { font-size:16px; }
.chip .nm { font-weight:500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.chip .sub { color:var(--muted); font-size:12px; }
.status { color:var(--muted); font-size:13px; }
.error { color:var(--err); font-size:14px; }
.cursor::after { content:"▍"; animation:blink 1s steps(1) infinite; color:var(--muted); }
@keyframes blink { 50% { opacity:0; } }
footer { padding:12px 16px 20px; }
.composer { max-width:780px; margin:0 auto; border:1px solid var(--line); border-radius:24px; padding:8px; background:var(--bg); }
.composer .row { display:flex; align-items:flex-end; gap:6px; }
.pending { display:flex; flex-wrap:wrap; gap:6px; padding:2px 6px 6px; }
.pending:empty { display:none; }
.pending .chip { padding:4px 10px; cursor:default; }
.pending .chip button { border:0; background:none; cursor:pointer; color:var(--muted); padding:0; }
textarea { flex:1; border:0; outline:0; resize:none; background:transparent; color:var(--text); font:inherit;
           max-height:200px; padding:6px 4px; }
.icon { width:36px; height:36px; border-radius:50%; border:0; background:transparent; cursor:pointer; font-size:18px; color:var(--muted); }
.icon:hover { background:var(--hover); }
.send { width:36px; height:36px; border-radius:50%; border:0; background:var(--text); color:var(--bg); cursor:pointer; font-size:16px; }
.hint { max-width:780px; margin:6px auto 0; text-align:center; color:var(--muted); font-size:12px; }
#panel { width:46%; min-width:360px; max-width:900px; flex:none; border-left:1px solid var(--line); display:flex;
         flex-direction:column; background:var(--bg); }
#panel[hidden] { display:none; }
.ph { display:flex; align-items:center; gap:8px; padding:8px 10px; border-bottom:1px solid var(--line); }
.ph .title { flex:1; min-width:0; font:13px ui-monospace, Menlo, monospace; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.ph a, .ph button { border:0; background:none; cursor:pointer; color:var(--muted); text-decoration:none; font-size:15px;
                    padding:4px 8px; border-radius:6px; }
.ph a:hover, .ph button:hover { background:var(--hover); color:var(--text); }
.tabs button { font-size:13px; }
.tabs button.on { background:var(--bubble); color:var(--text); }
#pbody { flex:1; overflow:auto; position:relative; }
#pbody iframe { position:absolute; inset:0; width:100%; height:100%; border:0; background:#fff; }
#pbody .pad { padding:16px; }
#pbody img { max-width:100%; display:block; margin:16px auto; background:repeating-conic-gradient(#8881 0% 25%, transparent 0% 50%) 50% / 16px 16px; }
.files .row { display:flex; align-items:center; gap:8px; padding:6px 12px; cursor:pointer; font-size:14px; }
.files .row:hover { background:var(--hover); }
.files .row .nm { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.files .row .dir { color:var(--muted); }
.files .row .sz { color:var(--muted); font-size:12px; }
.files .row a { color:var(--muted); text-decoration:none; padding:0 4px; }
.files .note { padding:16px; color:var(--muted); font-size:14px; }
.code { font:12.5px/1.55 ui-monospace, Menlo, monospace; margin:0; padding:12px 0; counter-reset:ln; }
.code div { white-space:pre-wrap; word-break:break-all; padding:0 12px 0 56px; position:relative; }
.code div::before { counter-increment:ln; content:counter(ln); position:absolute; left:0; width:44px; text-align:right;
                    color:var(--muted); opacity:.6; }
table.csv { border-collapse:collapse; font-size:13px; margin:12px; }
table.csv td, table.csv th { border:1px solid var(--line); padding:4px 8px; text-align:left; white-space:nowrap; }
table.csv th { background:var(--bubble); }
@media (max-width:1100px) { aside { display:none; } }
@media (max-width:820px) { #panel { position:fixed; inset:0; width:auto; max-width:none; min-width:0; z-index:5; } }
</style>
</head>
<body>
<aside>
  <button class="new" id="new">✎ New chat</button>
  <div class="chats" id="chats"></div>
</aside>
<main>
  <header>
    <strong>HarnessGateway</strong>
    <input id="model" list="models" spellcheck="false" title="harness/provider/model">
    <datalist id="models"></datalist>
    <span class="session" id="session"></span>
    <button class="tbtn" id="filesbtn" title="Workspace files">🗂 Files</button>
  </header>
  <div id="log"><div class="col" id="col"></div></div>
  <footer>
    <div class="composer">
      <div class="pending" id="pending"></div>
      <div class="row">
        <button class="icon" id="attach" title="Attach files">📎</button>
        <input type="file" id="picker" multiple hidden>
        <textarea id="input" rows="1" placeholder="Message the agent"></textarea>
        <button class="send" id="send" title="Send">↑</button>
      </div>
    </div>
    <div class="hint">Each conversation is one sandbox session: its files and the agent's memory carry over.</div>
  </footer>
</main>
<section id="panel" hidden>
  <div class="ph">
    <span class="tabs"><button id="tabfiles">Files</button><button id="tabview">Preview</button></span>
    <span class="title" id="ptitle"></span>
    <a id="pnew" target="_blank" rel="noopener" title="Open in a new tab">↗</a>
    <a id="pdl" title="Download">⤓</a>
    <button id="prefresh" title="Refresh">↻</button>
    <button id="pclose" title="Close">×</button>
  </div>
  <div id="pbody"></div>
</section>
<script>
const DEFAULT_MODEL = __DEFAULT_MODEL__;
const STORE = "hg-chatgui";
const $ = id => document.getElementById(id);
let chats = [], current = null, running = null, pending = [], files = [], filesFor = null;
const panel = { open: false, tab: "files", view: null };   // view: {kind: "file", path} | {kind: "snippet", url, lang}

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
const SHOWN = ["html", "htm", "svg", "md", "markdown", "png", "jpg", "jpeg", "gif", "webp", "pdf"];   // opened on their own
function fileIcon(p) { const e = ext(p); return ["html", "htm"].includes(e) ? "🌐" : IMAGES.includes(e) ? "🖼" : e === "pdf" ? "📕" : ["md", "markdown", "txt"].includes(e) ? "📝" : ["csv", "tsv", "json"].includes(e) ? "📊" : "📄"; }
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
      (preview ? `<button data-act="preview">Preview</button>` : "") + `<button data-act="copy">Copy</button></span></div>` +
      `<pre><code>${esc(code)}</code></pre></div>`;
  }).join("");
}

// ── the conversation ────────────────────────────────────────────────────────────────────────────────────────
const ICONS = { terminal: "›_", file: "✎", tool: "⚙", search: "⌕" };
function fileChips(paths) {
  const known = Object.fromEntries(files.map(f => [f.path, f]));
  return `<div class="chips">` + paths.map(p => `<button class="chip" data-open="${esc(p)}"><span class="ico">${fileIcon(p)}</span>` +
    `<span><div class="nm">${esc(p.split("/").pop())}</div><div class="sub">${esc(p.includes("/") ? p : "workspace")}` +
    `${known[p] ? " · " + size(known[p].bytes) : ""}</div></span></button>`).join("") + `</div>`;
}
function renderAssistant(turn, live, ti) {
  let html = "";
  turn.segments.forEach((seg, si) => {
    const at = `data-t="${ti}" data-s="${si}"`;
    if (seg.t === "thinking") {
      const open = seg.open ?? (live && seg === turn.segments.at(-1));
      html += `<details ${at}${open ? " open" : ""}><summary>Thinking</summary><div class="body">${esc(seg.text)}</div></details>`;
    } else if (seg.t === "steps") {
      const n = seg.steps.length;
      html += `<details ${at}${seg.open ? " open" : ""}><summary>${n} step${n > 1 ? "s" : ""}: ${esc(seg.steps.at(-1).label)}</summary><div class="body">` +
        seg.steps.map(s => {
          const label = s.path ? `${esc(s.verb)} <span class="link" data-open="${esc(rel(s.path))}">${esc(s.path)}</span>` : esc(s.label);
          return `<div class="step"><span class="label">${ICONS[s.icon] || "•"} ${label}</span>` +
            (s.output || s.detail ? `<pre>${esc((s.output || s.detail).slice(0, 20000))}</pre>` : "") +
            (s.exit_code ? `<div class="error">exit ${s.exit_code}</div>` : "") + `</div>`;
        }).join("") + `</div></details>`;
    } else if (seg.t === "text") {
      html += `<div class="${live && seg === turn.segments.at(-1) ? "cursor" : ""}">${markdown(seg.text)}</div>`;
    }
  });
  if (turn.files && turn.files.length) html += fileChips(turn.files);
  if (turn.error) html += `<div class="error">${esc(turn.error)}</div>`;
  else if (turn.status && turn.status !== "completed") html += `<div class="status">(${esc(turn.status)})</div>`;
  if (live && !turn.segments.length) html += `<div class="status cursor">Working</div>`;
  return html;
}
function render() {
  const c = chat(), col = $("col");
  $("chats").innerHTML = chats.map(x => `<div class="chat${x.id === current ? " active" : ""}" data-id="${x.id}"><span>${esc(x.title)}</span><button data-del="${x.id}" title="Delete">×</button></div>`).join("");
  $("session").textContent = c && c.session ? c.session : "";
  $("model").value = c ? c.model : ($("model").value || DEFAULT_MODEL);
  if (!c || !c.turns.length) { col.innerHTML = `<div class="empty"><h1>What should the agent build?</h1><div>It works in its own sandbox. Files it makes appear in the side panel, and pages it builds run there.</div></div>`; }
  else col.innerHTML = c.turns.map((t, i) => t.role === "user"
    ? `<div class="msg user"><div class="bubble">${esc(t.text)}</div>` + (t.attachments && t.attachments.length ? `<div class="chips">` + t.attachments.map(a => `<span class="chip"><span class="ico">📎</span><span class="nm">${esc(a.name)}</span></span>`).join("") + `</div>` : "") + `</div>`
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
  if (!c || !c.session) { files = []; filesFor = null; return; }
  try { files = (await (await fetch(`/api/files?session=${encodeURIComponent(c.session)}`)).json()).files || []; filesFor = c.session; }
  catch { files = []; }
}
function openPanel(tab, view) { panel.open = true; panel.tab = tab; if (view) panel.view = view; renderPanel(); }
function renderPanel() {
  const c = chat(), p = $("panel");
  p.hidden = !panel.open; $("filesbtn").classList.toggle("on", panel.open);
  if (!panel.open) return;
  $("tabfiles").classList.toggle("on", panel.tab === "files"); $("tabview").classList.toggle("on", panel.tab === "view");
  $("tabview").hidden = !panel.view;
  const body = $("pbody"), sid = c && c.session;
  if (panel.tab === "files" || !panel.view) {
    $("ptitle").textContent = sid ? `${files.length} file${files.length === 1 ? "" : "s"} in /workspace` : "";
    $("pnew").hidden = $("pdl").hidden = true;
    if (!sid) { body.innerHTML = `<div class="files"><div class="note">Start a conversation: its workspace files show here.</div></div>`; return; }
    if (!files.length) { body.innerHTML = `<div class="files"><div class="note">No files yet. Files appear here when a turn finishes.</div></div>`; return; }
    body.innerHTML = `<div class="files">` + files.map(f => {
      const dir = f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/") + 1) : "";
      return `<div class="row" data-open="${esc(f.path)}"><span>${fileIcon(f.path)}</span><span class="nm"><span class="dir">${esc(dir)}</span>${esc(f.path.slice(dir.length))}</span>` +
        `<span class="sz">${size(f.bytes)}</span><a href="${wsUrl(sid, f.path)}?download=1" title="Download" data-stop="1">⤓</a></div>`;
    }).join("") + `</div>`;
    return;
  }
  const v = panel.view;
  if (v.kind === "snippet") {
    $("ptitle").textContent = `Preview of a ${v.lang || "code"} block`;
    $("pnew").hidden = false; $("pnew").href = v.url; $("pdl").hidden = false; $("pdl").href = v.url + "?download=1";
    body.innerHTML = `<iframe sandbox="allow-scripts allow-forms allow-popups allow-modals" src="${esc(v.url)}"></iframe>`;
    return;
  }
  if (!sid) return;
  const url = wsUrl(sid, v.path), e = ext(v.path), f = files.find(x => x.path === v.path);
  $("ptitle").textContent = v.path; $("pnew").hidden = $("pdl").hidden = false;
  $("pnew").href = url; $("pdl").href = url + "?download=1";
  if (!f) { body.innerHTML = `<div class="pad status">${esc(v.path)} is not in the workspace yet: files appear when a turn finishes.</div>`; return; }
  if (["html", "htm"].includes(e)) body.innerHTML = `<iframe sandbox="allow-scripts allow-forms allow-popups allow-modals allow-downloads" src="${url}"></iframe>`;
  else if (e === "pdf") body.innerHTML = `<iframe src="${url}"></iframe>`;
  else if (IMAGES.includes(e)) body.innerHTML = `<img src="${url}" alt="${esc(v.path)}">`;
  else if (f.bytes > 2 * 1048576) body.innerHTML = `<div class="pad status">${size(f.bytes)}: too large to preview. Download it with ⤓.</div>`;
  else {
    body.innerHTML = `<div class="pad status">Loading…</div>`;
    fetch(url).then(r => r.text()).then(text => {
      if (panel.view !== v) return;
      if (text.includes("\u0000")) body.innerHTML = `<div class="pad status">Binary file, ${size(f.bytes)}. Download it with ⤓.</div>`;
      else if (["md", "markdown"].includes(e)) body.innerHTML = `<div class="pad assistant">${markdown(text)}</div>`;
      else if (["csv", "tsv"].includes(e)) body.innerHTML = csvTable(text, e === "tsv" ? "\t" : ",");
      else {
        let shown = text;
        if (e === "json") { try { shown = JSON.stringify(JSON.parse(text), null, 2); } catch {} }
        body.innerHTML = `<div class="code">` + shown.split("\n").map(l => `<div>${esc(l) || " "}</div>`).join("") + `</div>`;
      }
    }).catch(err => { body.innerHTML = `<div class="pad error">${esc(err)}</div>`; });
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
  if (!rows.length) return `<div class="pad status">Empty.</div>`;
  return `<table class="csv"><tr>${rows[0].map(c => `<th>${esc(c)}</th>`).join("")}</tr>` +
    rows.slice(1).map(r => `<tr>${r.map(c => `<td>${esc(c)}</td>`).join("")}</tr>`).join("") + `</table>`;
}
function openFile(path) { openPanel("view", { kind: "file", path: rel(path) }); }

// ── a turn ──────────────────────────────────────────────────────────────────────────────────────────────────
function apply(turn, ev) {
  const last = turn.segments.at(-1);
  if (ev.type === "text") { if (last && last.t === "text") last.text += ev.delta; else turn.segments.push({ t: "text", text: ev.delta }); }
  else if (ev.type === "thinking") { if (last && last.t === "thinking") last.text += ev.delta; else turn.segments.push({ t: "thinking", text: ev.delta }); }
  else if (ev.type === "tool") {
    const step = { id: ev.id, icon: ev.icon, label: ev.label, verb: ev.verb, path: ev.path, detail: ev.detail || "", output: ev.output || "" };
    if (last && last.t === "steps") last.steps.push(step); else turn.segments.push({ t: "steps", steps: [step] });
  } else if (ev.type === "tool_output") {
    for (const seg of turn.segments) if (seg.t === "steps") for (const s of seg.steps) if (s.id === ev.id) { s.output = ev.text; s.exit_code = ev.exit_code; }
  } else if (ev.type === "end") { turn.status = ev.status; turn.error = ev.error; }
}
async function afterTurn(c, turn, before) {
  // The workspace as the turn left it: chips for what it made or changed, and a page it built opens in the panel.
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
  const model = $("model").value.trim() || DEFAULT_MODEL;
  if (!c || (c.model !== model && c.turns.length)) {
    c = { id: newId(), title: text.slice(0, 60), model, previous: null, session: null, turns: [] };
    chats.unshift(c); current = c.id; files = [];
  }
  c.model = model;
  const attachments = pending.map(p => ({ id: p.id, name: p.name })); pending = []; renderPending();
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
  running = null; setBusy(false); save(); render();
  if (c.session) await afterTurn(c, turn, before);
}
function stop() {
  if (!running) return;
  if (running.id) post("/api/cancel", { id: running.id }).catch(() => {});
  running.controller.abort();
}
function setBusy(busy) { $("send").textContent = busy ? "■" : "↑"; $("send").title = busy ? "Stop" : "Send"; }

// ── attachments ─────────────────────────────────────────────────────────────────────────────────────────────
function renderPending() {
  $("pending").innerHTML = pending.map((p, i) => `<span class="chip"><span>📎</span><span class="nm">${esc(p.name)}</span>` +
    `<span class="sub">${p.uploading ? "uploading…" : p.error ? esc(p.error) : size(p.bytes)}</span><button data-unpend="${i}" title="Remove">×</button></span>`).join("");
}
function attach(list) {
  for (const file of list) {
    const item = { name: file.name, bytes: file.size, uploading: true };
    pending.push(item); renderPending();
    if (file.size > 20 * 1048576) { item.uploading = false; item.error = "over 20 MB"; renderPending(); continue; }
    const reader = new FileReader();
    reader.onload = () => {
      post("/api/attach", { name: file.name, data: String(reader.result).split(",")[1] || "" })
        .then(d => { item.id = d.id; item.uploading = false; })
        .catch(e => { item.uploading = false; item.error = e.message; })
        .finally(() => { pending = pending.filter(p => !p.error || p === item); renderPending(); });
    };
    reader.readAsDataURL(file);
  }
}

// ── wiring ──────────────────────────────────────────────────────────────────────────────────────────────────
function grow() { const t = $("input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 200) + "px"; }
$("input").addEventListener("input", grow);
$("input").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
$("input").addEventListener("paste", e => { if (e.clipboardData && e.clipboardData.files.length) { e.preventDefault(); attach(e.clipboardData.files); } });
$("send").addEventListener("click", () => running ? stop() : send());
$("attach").addEventListener("click", () => $("picker").click());
$("picker").addEventListener("change", e => { attach(e.target.files); e.target.value = ""; });
$("pending").addEventListener("click", e => { const i = e.target.dataset.unpend; if (i !== undefined) { pending.splice(+i, 1); renderPending(); } });
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
  const act = e.target.dataset.act;
  if (!act) return;
  const block = e.target.closest(".cb"), code = block.querySelector("code").textContent;
  if (act === "copy") { copyText(code); e.target.textContent = "Copied"; setTimeout(() => { e.target.textContent = "Copy"; }, 1200); }
  if (act === "preview") post("/api/snippet", { content: code, lang: block.dataset.lang }).then(d => openPanel("view", { kind: "snippet", url: d.url, lang: block.dataset.lang }));
});
$("pbody").addEventListener("click", e => {
  if (e.target.dataset.stop) return;
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
  const del = e.target.dataset.del;
  if (del) { chats = chats.filter(c => c.id !== del); if (current === del) { current = null; files = []; panel.view = null; } save(); render(); return; }
  const row = e.target.closest(".chat"); if (!row) return;
  current = row.dataset.id; panel.view = null; panel.tab = "files"; render(); await refreshFiles(); renderPanel();
});
fetch("/api/models").then(r => r.json()).then(d => {
  $("models").innerHTML = (d.models || []).map(m => `<option value="${esc(m)}">`).join("");
}).catch(() => {});
load(); current = chats[0] ? chats[0].id : null; render(); refreshFiles().then(renderPanel); $("input").focus();
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
