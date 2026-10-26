"""A ChatGPT-style page for talking to a harness: conversations in a sidebar, replies streamed with markdown, the
agent's thinking and commands folded under each reply. The page talks to this small server; this server holds your
key and talks to HarnessGateway, so the key never reaches the browser.

    HG_KEY=sk-hg-... python chatgui.py [--api http://localhost:8000] [--host 127.0.0.1] [--port 8800]

Then open http://localhost:8800 (from a Mac with the platform in an OrbStack VM: run with --host 0.0.0.0 and open
http://hg.orb.local:8800). Each conversation is one session: its workspace, files and the agent's memory carry from
message to message. Conversations are kept in the browser. Needs: pip install openai.
"""
# ruff: noqa: E501  (the page below is HTML and JavaScript, not Python)
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from openai import OpenAI

BODY_MAX = 4 * 1024 * 1024


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
        return {"type": "tool", "id": cid, "icon": "file", "label": f"{verb} {op.get('path', '')}",
                "detail": op.get("diff") or ""}
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


class Handler(BaseHTTPRequestHandler):
    client: OpenAI
    default_model: str
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:       # quiet: one line per chat turn is printed instead
        pass

    def _json(self, status: int, doc: object) -> None:
        body = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= BODY_MAX:
            raise ValueError("a JSON body is required")
        doc = json.loads(self.rfile.read(size))
        if not isinstance(doc, dict):
            raise ValueError("the body is a JSON object")
        return doc

    def do_GET(self) -> None:
        if self.path == "/":
            body = PAGE.replace("__DEFAULT_MODEL__", json.dumps(self.default_model)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/models":
            try:
                self._json(200, {"models": sorted(m.id for m in self.client.models.list())})
            except Exception as e:                       # the platform is down or the key is wrong
                self._json(502, {"error": str(getattr(e, "message", e))})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            doc = self._body()
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return
        if self.path == "/api/cancel":
            with contextlib.suppress(Exception):
                self.client.responses.cancel(str(doc.get("id", "")))
            self._json(200, {"ok": True})
        elif self.path == "/api/chat":
            self._chat(doc)
        else:
            self._json(404, {"error": "not found"})

    def _send(self, event: dict) -> None:
        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
        self.wfile.flush()

    def _chat(self, doc: dict) -> None:
        """One turn, relayed to the page as server-sent events. If the page goes away, the turn is cancelled."""
        model, text, previous = str(doc.get("model") or self.default_model), str(doc.get("input") or ""), doc.get("previous")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        rid = None
        try:
            kwargs = {"previous_response_id": previous} if previous else {}
            for event in self.client.responses.create(model=model, input=text, stream=True, **kwargs):
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
        --accent:#10a37f; --code:#f6f8fa; --err:#b42318; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#212121; --side:#171717; --text:#ececec; --muted:#9b9b9b; --line:#333; --bubble:#2f2f2f;
          --accent:#19c37d; --code:#1a1a1a; --err:#f97066; } }
* { box-sizing:border-box; }
html, body { height:100%; margin:0; }
body { display:flex; background:var(--bg); color:var(--text); font:15px/1.6 -apple-system, BlinkMacSystemFont,
       "Segoe UI", Roboto, sans-serif; }
aside { width:260px; background:var(--side); border-right:1px solid var(--line); display:flex; flex-direction:column; }
aside .new { margin:12px; padding:10px 12px; border:1px solid var(--line); border-radius:10px; background:transparent;
             color:var(--text); font:inherit; text-align:left; cursor:pointer; }
aside .new:hover, .chat:hover { background:var(--bubble); }
.chats { flex:1; overflow-y:auto; padding:0 8px 12px; }
.chat { display:flex; align-items:center; gap:6px; padding:8px 10px; border-radius:8px; cursor:pointer; }
.chat.active { background:var(--bubble); }
.chat span { flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.chat button { visibility:hidden; border:0; background:none; color:var(--muted); cursor:pointer; font-size:16px; }
.chat:hover button { visibility:visible; }
main { flex:1; display:flex; flex-direction:column; min-width:0; }
header { display:flex; align-items:center; gap:10px; padding:10px 16px; border-bottom:1px solid var(--line); }
header strong { font-weight:600; }
header input { flex:0 1 360px; padding:6px 10px; border:1px solid var(--line); border-radius:8px; background:var(--bg);
               color:var(--text); font:13px ui-monospace, Menlo, monospace; }
header .session { margin-left:auto; color:var(--muted); font:12px ui-monospace, Menlo, monospace; }
#log { flex:1; overflow-y:auto; }
.col { max-width:780px; margin:0 auto; padding:24px 16px; }
.empty { margin-top:20vh; text-align:center; color:var(--muted); }
.empty h1 { color:var(--text); font-weight:600; font-size:28px; margin:0 0 8px; }
.msg { margin:0 0 24px; }
.user { display:flex; justify-content:flex-end; }
.user div { background:var(--bubble); padding:10px 16px; border-radius:18px; max-width:80%; white-space:pre-wrap;
            word-wrap:break-word; }
.assistant p { margin:0 0 12px; }
.assistant p:last-child { margin-bottom:0; }
.assistant pre { background:var(--code); border:1px solid var(--line); border-radius:10px; padding:12px; overflow-x:auto;
                 font:13px/1.5 ui-monospace, Menlo, monospace; }
.assistant code { font:0.9em ui-monospace, Menlo, monospace; background:var(--code); padding:1px 5px; border-radius:5px; }
.assistant pre code { background:none; padding:0; }
.assistant a { color:var(--accent); }
.assistant h1, .assistant h2, .assistant h3 { font-size:1.1em; margin:16px 0 8px; }
.assistant ul, .assistant ol { margin:0 0 12px; padding-left:24px; }
details { margin:0 0 10px; color:var(--muted); font-size:14px; }
summary { cursor:pointer; user-select:none; }
details .body { margin:6px 0 0 4px; padding-left:12px; border-left:2px solid var(--line); white-space:pre-wrap; }
.step { margin:4px 0; }
.step .label { font:13px ui-monospace, Menlo, monospace; color:var(--text); }
.step pre { margin:4px 0 0; padding:8px; background:var(--code); border-radius:8px; max-height:220px; overflow:auto;
            font:12px/1.45 ui-monospace, Menlo, monospace; white-space:pre-wrap; }
.status { color:var(--muted); font-size:13px; }
.error { color:var(--err); font-size:14px; }
.cursor::after { content:"▍"; animation:blink 1s steps(1) infinite; color:var(--muted); }
@keyframes blink { 50% { opacity:0; } }
footer { padding:12px 16px 20px; }
.composer { max-width:780px; margin:0 auto; display:flex; align-items:flex-end; gap:8px; border:1px solid var(--line);
            border-radius:24px; padding:8px 8px 8px 18px; background:var(--bg); }
textarea { flex:1; border:0; outline:0; resize:none; background:transparent; color:var(--text); font:inherit;
           max-height:200px; padding:6px 0; }
.send { width:36px; height:36px; border-radius:50%; border:0; background:var(--text); color:var(--bg); cursor:pointer;
        font-size:16px; }
.send:disabled { opacity:.3; cursor:default; }
.hint { max-width:780px; margin:6px auto 0; text-align:center; color:var(--muted); font-size:12px; }
@media (max-width:720px) { aside { display:none; } }
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
  </header>
  <div id="log"><div class="col" id="col"></div></div>
  <footer>
    <div class="composer">
      <textarea id="input" rows="1" placeholder="Message the agent"></textarea>
      <button class="send" id="send" title="Send">↑</button>
    </div>
    <div class="hint">Each conversation is one sandbox session: its files and the agent's memory carry over.</div>
  </footer>
</main>
<script>
const DEFAULT_MODEL = __DEFAULT_MODEL__;
const STORE = "hg-chatgui";
const $ = id => document.getElementById(id);
let chats = [], current = null, running = null;

// ── storage ─────────────────────────────────────────────────────────────────────────────────────────────────
function load() { try { chats = JSON.parse(localStorage.getItem(STORE)) || []; } catch { chats = []; } }
function save() { try { localStorage.setItem(STORE, JSON.stringify(chats)); } catch {} }
function chat() { return chats.find(c => c.id === current); }

// ── markdown (small and safe: escapes everything, then adds a few constructs) ──────────────────────────────
const esc = s => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
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
  return src.split("```").map((part, i) => {
    if (i % 2 === 0) return blocks(part);
    const nl = part.indexOf("\n");
    return `<pre><code>${esc((nl >= 0 ? part.slice(nl + 1) : part).replace(/\n$/, ""))}</code></pre>`;
  }).join("");
}

// ── rendering ───────────────────────────────────────────────────────────────────────────────────────────────
const ICONS = { terminal: "›_", file: "✎", tool: "⚙", search: "⌕" };
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
        seg.steps.map(s => `<div class="step"><span class="label">${ICONS[s.icon] || "•"} ${esc(s.label)}</span>` +
          (s.output || s.detail ? `<pre>${esc((s.output || s.detail).slice(0, 20000))}</pre>` : "") +
          (s.exit_code ? `<div class="error">exit ${s.exit_code}</div>` : "") + `</div>`).join("") + `</div></details>`;
    } else if (seg.t === "text") {
      html += `<div class="${live && seg === turn.segments.at(-1) ? "cursor" : ""}">${markdown(seg.text)}</div>`;
    }
  });
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
  if (!c || !c.turns.length) { col.innerHTML = `<div class="empty"><h1>What should the agent do?</h1><div>It works in its own sandbox and keeps the files between messages.</div></div>`; return; }
  col.innerHTML = c.turns.map((t, i) => t.role === "user"
    ? `<div class="msg user"><div>${esc(t.text)}</div></div>`
    : `<div class="msg assistant" id="t${i}">${renderAssistant(t, running && i === c.turns.length - 1, i)}</div>`).join("");
  $("log").scrollTop = $("log").scrollHeight;
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

// ── a turn ──────────────────────────────────────────────────────────────────────────────────────────────────
function apply(turn, ev) {
  const last = turn.segments.at(-1);
  if (ev.type === "text") { if (last && last.t === "text") last.text += ev.delta; else turn.segments.push({ t: "text", text: ev.delta }); }
  else if (ev.type === "thinking") { if (last && last.t === "thinking") last.text += ev.delta; else turn.segments.push({ t: "thinking", text: ev.delta }); }
  else if (ev.type === "tool") {
    const step = { id: ev.id, icon: ev.icon, label: ev.label, detail: ev.detail || "", output: ev.output || "" };
    if (last && last.t === "steps") last.steps.push(step); else turn.segments.push({ t: "steps", steps: [step] });
  } else if (ev.type === "tool_output") {
    for (const seg of turn.segments) if (seg.t === "steps") for (const s of seg.steps) if (s.id === ev.id) { s.output = ev.text; s.exit_code = ev.exit_code; }
  } else if (ev.type === "end") { turn.status = ev.status; turn.error = ev.error; }
}
async function send() {
  const text = $("input").value.trim();
  if (!text || running) return;
  let c = chat();
  const model = $("model").value.trim() || DEFAULT_MODEL;
  if (!c || (c.model !== model && c.turns.length)) {
    c = { id: Date.now().toString(36) + Math.random().toString(36).slice(2), title: text.slice(0, 60), model, previous: null, session: null, turns: [] };
    chats.unshift(c); current = c.id;
  }
  c.model = model;
  c.turns.push({ role: "user", text });
  const turn = { role: "assistant", segments: [], status: null, error: null };
  c.turns.push(turn);
  $("input").value = ""; grow();
  running = { controller: new AbortController(), id: null };
  setBusy(true); render(); save();
  try {
    const res = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model, input: text, previous: c.previous }), signal: running.controller.signal });
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
}
function stop() {
  if (!running) return;
  if (running.id) fetch("/api/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: running.id }) });
  running.controller.abort();
}
function setBusy(busy) { $("send").textContent = busy ? "■" : "↑"; $("send").title = busy ? "Stop" : "Send"; }

// ── wiring ──────────────────────────────────────────────────────────────────────────────────────────────────
function grow() { const t = $("input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 200) + "px"; }
$("input").addEventListener("input", grow);
$("col").addEventListener("toggle", e => {             // a section the reader opens or closes stays that way
  const d = e.target, c = chat();
  if (!c || d.tagName !== "DETAILS" || d.dataset.t === undefined) return;
  const seg = c.turns[+d.dataset.t]?.segments[+d.dataset.s];
  if (seg && seg.open !== d.open) { seg.open = d.open; save(); }
}, true);
$("input").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
$("send").addEventListener("click", () => running ? stop() : send());
$("new").addEventListener("click", () => { if (running) return; current = null; render(); $("input").focus(); });
$("chats").addEventListener("click", e => {
  if (running) return;
  const del = e.target.dataset.del;
  if (del) { chats = chats.filter(c => c.id !== del); if (current === del) current = null; save(); render(); return; }
  const row = e.target.closest(".chat"); if (row) { current = row.dataset.id; render(); }
});
fetch("/api/models").then(r => r.json()).then(d => {
  $("models").innerHTML = (d.models || []).map(m => `<option value="${esc(m)}">`).join("");
}).catch(() => {});
load(); current = chats[0] ? chats[0].id : null; render(); $("input").focus();
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
    Handler.client = OpenAI(base_url=f"{args.api.rstrip('/')}/v1", api_key=args.key, timeout=3600, max_retries=0)
    Handler.default_model = args.model
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"HarnessGateway chat on http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
          f" (API {args.api}, default model {args.model}); Ctrl+C to stop", file=sys.stderr)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
