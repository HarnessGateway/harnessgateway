"""A ChatGPT-style page for talking to a harness, built on the platform's public API, with artifacts: conversations
in a sidebar, replies streamed with markdown (tables, math, highlighted code, diagrams), the agent's thinking and steps
folded under each reply, and a side panel with the session's workspace files that renders what the agent builds:
HTML pages run live; images, SVG, PDF, audio and video play; markdown, notebooks, CSV, JSON, code and text render in
place at any size; Word documents, spreadsheets and Mermaid diagrams render in a sandbox. Files you attach go to the
agent with your message. The header picks the harness and the provider from what your organization has installed and
connected; type the model, or leave it empty for the harness's default. A chat keeps its harness; its provider and
model can change from one message to the next.

Turns run in the background on the platform: move to another chat or start a new one while the agent works, and come
back to it (a running chat pulses in the sidebar); a reload picks running turns up again. Stop ends a turn; sending a
message while the agent works interrupts it, and the agent continues from where it stopped, with its work and memory.

    HG_KEY=hg_... python console/chat/server.py [--api http://localhost:8000] [--host 127.0.0.1] [--port 8800]

Then open http://localhost:8800 (to reach it from another machine, run with --host 0.0.0.0). This server holds your
key; the page never sees it. Each conversation is one session: its
workspace, files and the agent's memory carry from message to message. Workspace files are read from the session's
last save, so they appear once a turn's state is saved. What the agent made runs sandboxed, in an origin of its own.
Conversations are kept in the browser. Needs: pip install httpx.
"""
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
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

STATIC = Path(__file__).resolve().parent / "static"
BODY_MAX = 32 * 1024 * 1024                    # an attachment (20 MB) as base64, with room to spare
ATTACH_MAX = 20 * 1024 * 1024
CACHE_BYTES = 256 * 1024 * 1024                # workspace files kept in memory, so a video seeks without refetching
SESSION = re.compile(r"cntr_[0-9a-f]{8,64}")
RESPONSE = re.compile(r"resp_[0-9a-f]{8,64}")
RANGE = re.compile(r"bytes=(\d*)-(\d*)")
CDN = "https://cdn.jsdelivr.net"               # highlighting and math, pinned and checked by integrity hashes
# The page: its own scripts, and the CDN's. Nothing the agent wrote can add markup to it (see static/markdown.js).
PAGE_CSP = (f"default-src 'self'; script-src 'self' {CDN}; style-src 'self' 'unsafe-inline' {CDN}; font-src {CDN}; "
            "img-src 'self' data: blob:; media-src 'self' blob:; frame-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
# What the agent made, and the viewer that renders documents and diagrams, run in an origin of their own: they can
# neither read this page nor call this server as the page.
SANDBOX = ("sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads; "
           "default-src * data: blob: 'unsafe-inline' 'unsafe-eval'")
TEXT_TYPES = ("application/json", "application/javascript", "text/javascript", "image/svg+xml")
for ext, kind in ((".md", "text/markdown"), (".markdown", "text/markdown"), (".mmd", "text/plain"),
                  (".ts", "text/plain"), (".tsx", "text/plain"), (".jsx", "text/plain"), (".py", "text/plain"),
                  (".sh", "text/plain"), (".yaml", "text/plain"), (".yml", "text/plain"), (".toml", "text/plain"),
                  (".go", "text/plain"), (".rs", "text/plain"), (".ipynb", "application/json"),
                  (".js", "text/javascript"), (".mjs", "text/javascript"), (".webp", "image/webp"),
                  (".avif", "image/avif"), (".m4a", "audio/mp4"), (".opus", "audio/ogg"), (".mov", "video/quicktime")):
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


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The one span a Range header asks for, first and last byte inclusive; None for the whole file (no header, or one
    this server does not serve: several spans, another unit). ValueError: the span lies outside the file."""
    m = RANGE.fullmatch((header or "").strip())
    if not m or not (m[1] or m[2]) or size == 0:
        return None
    if m[1]:
        first, last = int(m[1]), min(int(m[2]), size - 1) if m[2] else size - 1
    else:                                                  # the last n bytes
        first, last = max(size - int(m[2]), 0), size - 1
        if int(m[2]) == 0:
            raise ValueError("an empty suffix")
    if first >= size or first > last:
        raise ValueError(f"bytes {first}-{last} of {size}")
    return first, last


class Workspace:
    """A session's files as the platform keeps them (its last save). The listing is cached for a few seconds, so that a
    page and the files it links to resolve quickly; contents are cached by version, up to CACHE_BYTES in all."""

    def __init__(self, http: httpx.Client):
        self.http = http
        self._listings: dict[str, tuple[float, dict[str, dict]]] = {}
        self._contents: OrderedDict[tuple, bytes] = OrderedDict()
        self._held = 0
        self._lock = threading.Lock()

    def state(self, sid: str) -> str:
        """running, saving (the listing is not yet what the last turn left) or idle."""
        r = self.http.get(f"/v1/containers/{sid}")
        r.raise_for_status()
        return (r.json().get("metadata") or {}).get("state") or "idle"

    def files(self, sid: str, fresh: bool = False) -> dict[str, dict]:
        with self._lock:
            hit = self._listings.get(sid)
        if hit and not fresh and time.monotonic() - hit[0] < 5:
            return hit[1]
        out: dict[str, dict] = {}
        after = None
        for _ in range(200):                             # 200 pages of 100: the platform lists at most 20,000
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
            self._listings[sid] = (time.monotonic(), out)
        return out

    def find(self, sid: str, rel: str) -> dict | None:
        return self.files(sid).get(rel) or self.files(sid, fresh=True).get(rel)

    def content(self, sid: str, f: dict) -> bytes:
        key = (sid, f["id"], f["bytes"], f["at"])
        with self._lock:
            if key in self._contents:
                self._contents.move_to_end(key)
                return self._contents[key]
        r = self.http.get(f"/v1/containers/{sid}/files/{f['id']}/content", timeout=httpx.Timeout(30, read=600))
        r.raise_for_status()
        data = r.content
        if len(data) <= CACHE_BYTES // 4:
            with self._lock:
                if key not in self._contents:
                    self._contents[key] = data
                    self._held += len(data)
                while self._held > CACHE_BYTES:
                    self._held -= len(self._contents.popitem(last=False)[1])
        return data


class Handler(BaseHTTPRequestHandler):
    http: httpx.Client
    workspace: Workspace
    default_model: str
    static: dict[str, Path]
    snippets: OrderedDict[str, tuple[str, bytes]] = OrderedDict()   # code from replies, previewed in the panel
    snippets_lock = threading.Lock()
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

    def _text(self, status: int, message: str) -> None:
        self._reply(status, message.encode(), "text/plain; charset=utf-8")

    @contextlib.contextmanager
    def _platform_errors(self) -> Iterator[None]:
        """A platform error goes to the page as it came; a browser that went away needs no answer."""
        try:
            yield
        except (BrokenPipeError, ConnectionResetError):
            pass
        except httpx.HTTPStatusError as e:
            self._json(e.response.status_code, {"error": e.response.text[:500]})
        except httpx.HTTPError as e:
            self._json(502, {"error": f"the platform is unreachable: {e}"})

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

    def _static(self, name: str, csp: str | None = None) -> None:
        """One of the page's own files, by name: the directory is listed at start, so no path reaches outside it."""
        path = self.static.get(name)
        if not path:
            self._text(404, "not found")
            return
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        headers = {"Cache-Control": "no-cache"}
        if csp:
            headers["Content-Security-Policy"] = csp
        self._reply(200, path.read_bytes(), f"{kind}; charset=utf-8", headers)

    def _file(self, name: str, data: bytes, download: bool) -> None:
        """A file the agent made, sandboxed, in whole or the span a Range header asks for (media players seek)."""
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind in TEXT_TYPES:
            kind += "; charset=utf-8"
        headers = {"Cache-Control": "no-store", "Accept-Ranges": "bytes"}
        if not kind.startswith("application/pdf"):         # a browser shows no PDF in a sandboxed document
            headers["Content-Security-Policy"] = SANDBOX
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{name.rsplit("/", 1)[-1].replace(chr(34), "")}"'
        try:
            span = byte_range(self.headers.get("Range"), len(data))
        except ValueError:
            self._reply(416, b"", kind, {"Content-Range": f"bytes */{len(data)}"})
            return
        if span is None:
            self._reply(200, data, kind, headers)
            return
        first, last = span
        headers["Content-Range"] = f"bytes {first}-{last}/{len(data)}"
        self._reply(206, data[first:last + 1], kind, headers)

    def catalog(self) -> dict:
        """What the picker offers: the organization's harnesses and its providers (no keys: the API never returns them)."""
        def listing(path: str) -> list[dict]:
            r = self.http.get(path)
            r.raise_for_status()
            return r.json()["data"]
        try:
            harnesses = [{k: h.get(k) for k in ("name", "description", "provider_apis", "default_provider", "default_model")}
                         for h in listing("/v1/harnesses")]
            providers = [{"name": p["name"], "api": p["api"]} for p in listing("/v1/providers")]
        except httpx.HTTPStatusError as e:              # a run-scoped key: the same picture from /v1/models
            if e.response.status_code != 403:
                raise
            harnesses, providers, seen = [], [], {}
            for m in listing("/v1/models"):
                parts = m["id"].split("/")
                h = seen.setdefault(parts[0], {"name": parts[0], "description": "", "provider_apis": [], "default_provider": "",
                                               "default_model": ""})
                if len(parts) > 1:
                    h["provider_apis"] = ["*"]
                    h["default_provider"] = h["default_provider"] or parts[1]
                    h["default_model"] = h["default_model"] or (parts[2] if len(parts) > 2 else "")
                    if parts[1] not in [p["name"] for p in providers]:
                        providers.append({"name": parts[1], "api": "*"})
            harnesses = list(seen.values())
        return {"harnesses": harnesses, "providers": providers, "default_model": self.default_model}

    # ── GET ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        arg = lambda name: (query.get(name) or [""])[0]    # noqa: E731
        with self._platform_errors():
            if url.path == "/":
                self._static("index.html", PAGE_CSP)
            elif url.path == "/viewer":
                self._static("viewer.html", SANDBOX)
            elif url.path.startswith("/static/"):
                self._static(url.path.removeprefix("/static/"))
            elif url.path == "/api/catalog":
                self._json(200, self.catalog())
            elif url.path == "/api/follow":
                if not RESPONSE.fullmatch(arg("id")):
                    self._json(400, {"error": "a response id (resp_...)"})
                    return
                self._relay("GET", f"/v1/responses/{arg('id')}", params={"stream": "true", "starting_after": "-1"})
            elif url.path == "/api/container":
                sid = arg("session")
                if not SESSION.fullmatch(sid):
                    self._json(400, {"error": "a session id (cntr_...)"})
                    return
                r = self.http.get(f"/v1/containers/{sid}")
                r.raise_for_status()
                self._json(200, r.json())
            elif url.path in ("/api/files", "/api/session"):
                sid = arg("session")
                if not SESSION.fullmatch(sid):
                    self._json(400, {"error": "a session id (cntr_...)"})
                elif url.path == "/api/session":
                    self._json(200, {"state": self.workspace.state(sid)})
                else:
                    files = self.workspace.files(sid, fresh=True)
                    self._json(200, {"files": sorted(files.values(), key=lambda f: f["path"])})
            elif url.path.startswith("/ws/"):
                self._workspace_file(url.path.removeprefix("/ws/"), "download" in query)
            elif url.path.startswith("/snippet/"):
                with self.snippets_lock:
                    hit = self.snippets.get(url.path.removeprefix("/snippet/"))
                if hit:
                    self._file(hit[0], hit[1], "download" in query)
                else:
                    self._text(404, "not found")
            else:
                self._text(404, "not found")

    def _workspace_file(self, path: str, download: bool) -> None:
        sid, _, rel = path.partition("/")
        rel = unquote(rel)
        if not SESSION.fullmatch(sid) or not rel:
            self._text(404, "not found")
            return
        found = self.workspace.find(sid, rel)
        if not found:
            self._text(404, f"{rel} is not in the workspace (yet): files appear once a turn's state is saved")
            return
        self._file(rel, self.workspace.content(sid, found), download)

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
        with self._platform_errors():
            if self.path == "/api/chat":
                self._chat(doc)
            elif self.path == "/api/cancel":
                rid = str(doc.get("id", ""))
                if RESPONSE.fullmatch(rid):
                    with contextlib.suppress(httpx.HTTPError):
                        self.http.post(f"/v1/responses/{rid}/cancel")
                self._json(200, {"ok": True})
            elif self.path == "/api/attach":
                self._attach(doc)
            elif self.path == "/api/snippet":
                name = "snippet.svg" if doc.get("lang") == "svg" else "snippet.html"
                sid = uuid.uuid4().hex
                with self.snippets_lock:
                    self.snippets[sid] = (name, str(doc.get("content", "")).encode()[:BODY_MAX])
                    while len(self.snippets) > 100:
                        self.snippets.popitem(last=False)
                self._json(200, {"id": sid, "url": f"snippet/{sid}"})
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
        """One turn, started in the background (it runs whether or not a page watches it) and relayed to the page."""
        model, text, previous = str(doc.get("model") or self.default_model), str(doc.get("input") or ""), doc.get("previous")
        files = [f for f in doc.get("attachments") or [] if isinstance(f, str) and f.startswith("file")]
        request_input: object = text
        if files:
            request_input = [{"role": "user", "content": [{"type": "input_text", "text": text},
                                                          *({"type": "input_file", "file_id": f} for f in files)]}]
        body = {"model": model, "input": request_input, "stream": True, "background": True}
        if isinstance(previous, str) and RESPONSE.fullmatch(previous):
            body["previous_response_id"] = previous
        print(f"{model}: {'continues ' + previous if previous else 'new session'}", file=sys.stderr)
        self._relay("POST", "/v1/responses", json=body)

    def _relay(self, method: str, path: str, **kwargs) -> None:
        """The platform's event stream for one turn, as the page's events (start, text, thinking, tool, tool_output,
        end). Leaving early (the page closed or switched away) stops only the relay: the turn keeps running and the page
        can follow it again from the start with /api/follow."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            with self.http.stream(method, path, timeout=httpx.Timeout(60, read=300), **kwargs) as r:
                if r.status_code >= 400:
                    r.read()
                    try:
                        message = r.json()["error"]["message"]
                    except (ValueError, KeyError, TypeError):
                        message = r.text[:500]
                    self._send({"type": "end", "status": "failed", "error": message})
                    return
                for line in r.iter_lines():
                    if line.startswith("data: "):
                        self._event(json.loads(line[6:]))
        except (BrokenPipeError, ConnectionResetError):
            pass                                          # the page went away; the turn goes on
        except Exception as e:                            # the platform went away mid-turn
            with contextlib.suppress(Exception):
                self._send({"type": "end", "status": "failed", "error": f"lost the platform: {e}"})

    def _event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "response.created":
            r = event["response"]
            self._send({"type": "start", "id": r["id"], "session": (r.get("metadata") or {}).get("session_id")})
        elif kind == "response.output_text.delta":
            self._send({"type": "text", "delta": event.get("delta", "")})
        elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            self._send({"type": "thinking", "delta": event.get("delta", "")})
        elif kind == "response.output_item.done":
            shown = describe(event.get("item") or {})
            if shown:
                self._send(shown)
        elif kind in ("response.completed", "response.failed", "response.incomplete"):
            r = event["response"]
            error = None
            if r.get("status") == "failed" and r.get("error"):
                code = (r.get("metadata") or {}).get("hg_error_code") or r["error"].get("code")
                error = f"{code}: {r['error'].get('message', '')}"
            self._send({"type": "end", "status": r.get("status"), "error": error})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        """A browser that drops a connection (a closed tab, a keep-alive the network reset) is not an error."""
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.environ.get("HG_API", "http://localhost:8000"))
    ap.add_argument("--key", default=os.environ.get("HG_KEY", ""))
    ap.add_argument("--model", default=os.environ.get("HG_MODEL", ""), help="the picker's first choice; empty: the first harness")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to reach it from outside this machine (it holds your key)")
    ap.add_argument("--port", type=int, default=8800)
    args = ap.parse_args()
    if not args.key:
        sys.exit("set HG_KEY (or pass --key) to your organization's hg_... key")
    api = args.api.rstrip("/")
    Handler.http = httpx.Client(base_url=api, headers={"Authorization": f"Bearer {args.key}"}, timeout=120)
    Handler.workspace = Workspace(Handler.http)
    Handler.default_model = args.model
    Handler.static = {p.name: p for p in STATIC.iterdir() if p.is_file()}
    server = Server((args.host, args.port), Handler)
    print(f"HarnessGateway chat on http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
          f" (API {api}, default model {args.model}); Ctrl+C to stop", file=sys.stderr)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
