"""One turn of Hermes Agent: drive `hermes acp` (Agent Client Protocol, JSON-RPC 2.0 over stdio) and map it with map.py.

    1. $HERMES_HOME/config.yaml is written for this turn: the provider (the gateway, anthropic_messages or
       chat_completions), the model, reasoning effort, max turns, MCP servers; nothing that phones home or nudges.
    2. Continuing a session: its row in Hermes's state.db is pointed at this turn's provider and model (Hermes
       restores a session's provider from it), and its stored system prompt is dropped if the instructions changed.
    3. initialize -> session/load (the id kept in $HG_STATE) or session/new -> session/set_mode dont_ask ->
       session/prompt. Permission requests are answered "allow" (the sandbox is the boundary; approvals are off too).
    4. session/update notifications, Hermes's stderr log and new state.db transcript rows go to the mapper, in order;
       each is also printed as a `hermes-raw` log line (never shown to the client) so a turn can be replayed.
    5. SIGTERM (cancel): session/cancel, a short wait for Hermes to stop and save, then the terminal event.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contextlib

from hgkit import Emitter, Request, State, input_files  # noqa: E402
from map import API_CALL_OK, ERROR_LINE, TURN_ENDED, Mapper  # noqa: E402

PROVIDER = "harnessgateway"
HERMES = os.path.join(os.environ.get("HG_TOOLS", "/opt/hg/tools"), "bin", "hermes")
WORKSPACE = os.environ.get("HG_WORKSPACE", "/workspace")
HERMES_HOME = os.path.join(os.environ.get("HOME", "/home/hg"), ".hermes")
DB_PATH = os.path.join(HERMES_HOME, "state.db")
RAW_MAX = 200_000
CANCEL_WAIT_S = 6
HINT = ("You are running in a HarnessGateway sandbox on behalf of an API client. The working directory {ws} is the "
        "user's workspace; it and $HOME persist across the turns of this conversation. Work autonomously: nobody "
        "can answer questions or approve actions mid-turn. Your turn ends when you reply, and nothing reports back to "
        "you after that: run commands in the foreground and wait for them, unless asked to leave a service running.")

class _Stdout:
    """stdout that outlives its reader: if the node stops reading, the turn still stops Hermes before exiting."""

    def write(self, text: str) -> None:
        with contextlib.suppress(BrokenPipeError, OSError, ValueError):
            sys.stdout.write(text)

    def flush(self) -> None:
        with contextlib.suppress(BrokenPipeError, OSError, ValueError):
            sys.stdout.flush()


out = Emitter(stream=_Stdout())
req = Request.load()
state = State()


def _stderr(text: str) -> None:
    try:
        sys.stderr.write(text)
        sys.stderr.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass


def raw(kind: str, value: Any) -> None:
    line = json.dumps([kind, value], ensure_ascii=False, separators=(",", ":"))
    out.log("hermes-raw " + (line if len(line) <= RAW_MAX else json.dumps([kind, {"truncated": len(line)}])))


# ── the request ────────────────────────────────────────────────────────────────────────────────────────────────
def prompt_blocks() -> list[dict[str, Any]]:
    text = req.prompt
    notes: list[str] = []
    images: list[dict[str, Any]] = []
    index = input_files()
    for p in req._parts():
        kind = p.get("type")
        if kind == "input_image":
            data, mime = None, None
            if p.get("file_id") in index:
                f = index[p["file_id"]]
                data = base64.b64encode(Path(f["path"]).read_bytes()).decode()
                mime = mimetypes.guess_type(f["filename"])[0]
            elif str(p.get("image_url", "")).startswith("data:"):
                head, data = p["image_url"].split(",", 1)
                mime = head[5:].split(";")[0]
            elif p.get("image_url"):
                notes.append(f"Image URL: {p['image_url']}")
            if data:
                images.append({"type": "image", "data": data, "mimeType": mime or "image/png"})
        elif kind == "input_file":
            name = None
            if p.get("file_id") in index:
                f = index[p["file_id"]]
                name = os.path.basename(f["filename"]) or p["file_id"]
                shutil.copyfile(f["path"], os.path.join(WORKSPACE, name))
            elif p.get("file_data"):
                name = os.path.basename(p.get("filename") or "") or f"input-{len(notes) + 1}"
                data = p["file_data"]
                Path(WORKSPACE, name).write_bytes(base64.b64decode(data.split(",", 1)[1]) if data.startswith("data:")
                                                  else data.encode())
            elif p.get("file_url"):
                notes.append(f"File URL: {p['file_url']}")
            if name:
                notes.append(f"Attached file (in the workspace): {name}")
    fmt = (req.get("text") or {}).get("format") or {}
    if fmt.get("type") == "json_schema":
        notes.append("Reply with only a JSON value (no prose, no code fences) that matches this JSON Schema:\n"
                     + json.dumps(fmt.get("schema") or {}, ensure_ascii=False))
    elif fmt.get("type") == "json_object":
        notes.append("Reply with only a JSON object (no prose, no code fences).")
    full = "\n\n".join(x for x in [text, *notes] if x)
    return ([{"type": "text", "text": full}] if full else []) + images


def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def mcp_config() -> tuple[dict[str, Any], dict[str, str]]:
    servers, names = {}, {}
    for t in req.mcp_servers:
        label, url = t.get("server_label"), t.get("server_url")
        if not label or not url:
            continue
        headers = dict(t.get("headers") or {})
        if t.get("authorization"):
            headers.setdefault("Authorization", f"Bearer {t['authorization']}")
        cfg: dict[str, Any] = {"url": url, "headers": headers}
        allowed = t.get("allowed_tools")
        if isinstance(allowed, dict):
            allowed = allowed.get("tool_names")
        if isinstance(allowed, list) and allowed:
            cfg["tools"] = {"include": allowed}
        servers[label] = cfg
        names[sanitize(label)] = label
    return servers, names


def write_config(model: str, api: str, base_url: str, mcp: dict[str, Any]) -> None:
    """config.yaml, as JSON (YAML's superset), rewritten every turn so it always matches the request."""
    effort = req.reasoning_effort
    agent: dict[str, Any] = {"verify_on_stop": False, "max_verify_nudges": 0}
    if effort:
        agent["reasoning_effort"] = {"minimal": "minimal", "none": "none"}.get(effort, effort)
    if isinstance(req.get("max_tool_calls"), int) and req.get("max_tool_calls") > 0:
        agent["max_turns"] = req.get("max_tool_calls")
    cfg = {
        "_config_version": 33,
        "model": {"provider": PROVIDER, "default": model, "context_length": 128000},
        "providers": {PROVIDER: {"name": PROVIDER, "base_url": base_url, "key_env": "HG_PROVIDER_TOKEN",
                                 "transport": api, "default_model": model}},
        "fallback_providers": [],
        "agent": agent,
        "approvals": {"mode": "off", "mcp_reload_confirm": False, "destructive_slash_confirm": False},
        "hooks_auto_accept": True,
        "security": {"tirith_enabled": False, "allow_lazy_installs": False},
        "auxiliary": {"title_generation": {"enabled": False}},
        "curator": {"enabled": False},
        "model_catalog": {"enabled": False},
        "lsp": {"enabled": False},
        "memory": {"nudge_interval": 0},
        "skills": {"creation_nudge_interval": 0},
        "checkpoints": {"enabled": False},
        "sessions": {"auto_prune": False},
        "onboarding": {"profile_build": "skip"},
        "display": {"credits_notices": False, "turn_completion_explainer": False},
        "mcp_servers": mcp,
    }
    os.makedirs(HERMES_HOME, exist_ok=True)
    Path(HERMES_HOME, "config.yaml").write_text(json.dumps(cfg, indent=1))


# ── Hermes's session store ─────────────────────────────────────────────────────────────────────────────────────
LINEAGE = ("WITH RECURSIVE lin(id) AS (SELECT ? UNION SELECT s.id FROM sessions s JOIN lin ON s.parent_session_id = "
           "lin.id) ")


def db() -> sqlite3.Connection | None:
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def prepare_session(sid: str, model: str, api: str, base_url: str, hint_changed: bool) -> bool:
    """Point a stored ACP session at this turn's provider and model. False when there is no such session."""
    try:
        conn = db()
        if conn is None:
            return False
        with conn:
            row = conn.execute("SELECT model_config FROM sessions WHERE id = ? AND source = 'acp'", (sid,)).fetchone()
            if row is None:
                return False
            meta = json.loads(row["model_config"] or "{}") if (row["model_config"] or "").startswith("{") else {}
            meta.update(cwd=WORKSPACE, provider=PROVIDER, base_url=base_url, api_mode=api)
            conn.execute("UPDATE sessions SET model = ?, model_config = ? WHERE id = ?", (model, json.dumps(meta), sid))
            if hint_changed:                               # the system prompt is frozen per session; rebuild it
                conn.execute(LINEAGE + "UPDATE sessions SET system_prompt = NULL WHERE id IN lin", (sid,))
        conn.close()
        return True
    except sqlite3.Error as e:
        out.log(f"hermes: could not prepare session {sid}: {e}")
        return False


def _json_list(text: Any) -> list[dict[str, Any]]:
    try:
        value = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return []
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


class Transcript:
    """New rows of the session's transcript (its compression children included), each read once."""

    def __init__(self) -> None:
        self.sid = ""
        self.ok = True
        self.last = 0
        self.cache_write0 = 0
        self.known = set()
        try:
            conn = db()
            if conn is not None:
                self.last = conn.execute("SELECT coalesce(max(id), 0) FROM messages").fetchone()[0]
                conn.close()
        except sqlite3.Error as e:
            self.ok = False
            out.log(f"hermes: state.db unreadable: {e}")

    def bind(self, sid: str) -> None:
        """Hermes may rewrite a resumed session's whole transcript (new row ids) when it saves, so tool calls the
        session already had are remembered and never reported again."""
        self.sid = sid
        self.cache_write0 = self.cache_write()
        self.known: set[str] = set()
        try:
            conn = db()
            if conn is not None:
                for r in conn.execute(LINEAGE + "SELECT tool_call_id, tool_calls FROM messages WHERE session_id IN lin "
                                      "AND (tool_call_id IS NOT NULL OR tool_calls IS NOT NULL)", (sid,)):
                    if r["tool_call_id"]:
                        self.known.add(r["tool_call_id"])
                    for tc in _json_list(r["tool_calls"]):
                        self.known.add(tc.get("id") or tc.get("call_id") or "")
                conn.close()
        except sqlite3.Error as e:
            out.log(f"hermes: state.db unreadable: {e}")

    def cache_write(self) -> int:
        try:
            conn = db()
            if conn is None:
                return 0
            n = conn.execute(LINEAGE + "SELECT coalesce(sum(cache_write_tokens), 0) FROM sessions WHERE id IN lin",
                             (self.sid,)).fetchone()[0]
            conn.close()
            return int(n or 0)
        except sqlite3.Error:
            return 0

    def new_rows(self) -> list[dict[str, Any]]:
        if not self.sid or not self.ok:
            return []
        try:
            conn = db()
            if conn is None:
                return []
            rows = conn.execute(LINEAGE + "SELECT id, role, content, tool_call_id, tool_calls, tool_name FROM messages "
                                "WHERE id > ? AND session_id IN lin ORDER BY id", (self.sid, self.last)).fetchall()
            conn.close()
        except sqlite3.Error as e:
            self.ok = False
            out.log(f"hermes: state.db unreadable: {e}")
            return []
        if rows:
            self.last = rows[-1]["id"]
        fresh = []
        for r in map(dict, rows):
            if r["tool_call_id"] in self.known:
                continue
            if r["tool_calls"]:
                calls = [tc for tc in _json_list(r["tool_calls"]) if (tc.get("id") or tc.get("call_id")) not in self.known]
                if not calls:
                    continue
                r["tool_calls"] = json.dumps(calls)
            if r["role"] in ("assistant", "tool"):
                fresh.append({k: v for k, v in r.items() if v is not None})
        return fresh


# ── the ACP connection ─────────────────────────────────────────────────────────────────────────────────────────
class Hermes:
    def __init__(self, env: dict[str, str]):
        self.proc = subprocess.Popen([HERMES, "acp", "--accept-hooks"], cwd=WORKSPACE, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.sel = selectors.DefaultSelector()
        self.bufs = {"out": b"", "err": b""}
        self.sel.register(self.proc.stdout, selectors.EVENT_READ, "out")
        self.sel.register(self.proc.stderr, selectors.EVENT_READ, "err")
        self.next_id = 0
        self.eof = False
        self.stderr_tail: list[str] = []

    def send(self, msg: dict[str, Any]) -> None:
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass

    def request(self, method: str, params: dict[str, Any]) -> int:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        return self.next_id

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def poll(self, timeout: float) -> list[tuple[str, Any]]:
        """("msg", json) and ("err", line) as they arrive, stderr first when both are ready (Hermes logs a failure
        before it reports it)."""
        if self.eof:
            time.sleep(min(timeout, 0.2))
            return []
        events = self.sel.select(timeout)
        got: list[tuple[str, Any]] = []
        for key, _ in sorted(events, key=lambda e: e[0].data != "err"):
            chunk = os.read(key.fileobj.fileno(), 65536)
            if not chunk:
                self.sel.unregister(key.fileobj)
                if key.data == "out":
                    self.eof = True
                continue
            self.bufs[key.data] += chunk
            *lines, self.bufs[key.data] = self.bufs[key.data].split(b"\n")
            for line in lines:
                text = line.decode("utf-8", "replace")
                if key.data == "err":
                    _stderr(text + "\n")
                    self.stderr_tail = (self.stderr_tail + [text])[-20:]
                    got.append(("err", text))
                elif text.strip():
                    try:
                        got.append(("msg", json.loads(text)))
                    except ValueError:
                        out.log("hermes stdout: " + text[:2000])
        return got

    def close(self, wait: float = 8) -> None:
        """Hermes runs in a session of its own (so a cancel reaches it as session/cancel, letting it stop its tools
        and save); it exits when its stdin closes, or is signalled."""
        with contextlib.suppress(OSError):
            self.proc.stdin.close()
        for sig, grace in ((None, wait), (signal.SIGTERM, 3), (signal.SIGKILL, 2)):
            if sig is not None:
                with contextlib.suppress(OSError):
                    os.killpg(self.proc.pid, sig)
            try:
                self.proc.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue


def allow(params: dict[str, Any]) -> dict[str, Any]:
    options = params.get("options") or []
    for kind in ("allow_always", "allow_once"):
        for o in options:
            if o.get("kind") == kind:
                return {"outcome": {"outcome": "selected", "optionId": o["optionId"]}}
    if options:
        return {"outcome": {"outcome": "selected", "optionId": options[0]["optionId"]}}
    return {"outcome": {"outcome": "cancelled"}}


# ── the turn ───────────────────────────────────────────────────────────────────────────────────────────────────
def main() -> None:
    stop = {"at": None}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(at=stop["at"] or time.monotonic()))
    signal.signal(signal.SIGINT, lambda *_: stop.update(at=stop["at"] or time.monotonic()))

    api = os.environ.get("HG_PROVIDER_API", "")
    url = os.environ.get("HG_PROVIDER_URL", "")
    model = os.environ.get("HG_MODEL", "")
    if api not in ("anthropic", "openai") or not url or not model:
        out.failed("no_provider", "Hermes needs a provider (anthropic or openai shape) and a model")
        return
    transport = "anthropic_messages" if api == "anthropic" else "chat_completions"
    base_url = url if api == "anthropic" else url.rstrip("/") + "/v1"

    blocks = prompt_blocks()
    if not blocks:
        out.failed("invalid_prompt", "the request has no input text or image")
        return
    mcp, mcp_names = mcp_config()
    write_config(model, transport, base_url, mcp)
    hint = HINT.format(ws=WORKSPACE)
    if req.instructions.strip():
        hint += "\n\n## Instructions for this conversation (from the user; follow them)\n\n" + req.instructions.strip()
    hint_sha = hashlib.sha256(hint.encode()).hexdigest()

    sid = state.get("hermes_session")
    resuming = bool(sid) and prepare_session(sid, model, transport, base_url, state.get("hermes_hint") != hint_sha)
    if sid and not resuming:
        out.log(f"hermes: session {sid} is not in state.db; starting a new one")
    transcript = Transcript()

    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONDONTWRITEBYTECODE")}
    token = os.environ.get("HG_PROVIDER_TOKEN", "")
    env.update(HERMES_HOME=HERMES_HOME, HERMES_YOLO_MODE="1", HERMES_ACCEPT_HOOKS="1", HERMES_ENVIRONMENT_HINT=hint,
               NO_COLOR="1", TERM="dumb", ANTHROPIC_API_KEY=token, OPENAI_API_KEY=token)
    for k in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "HERMES_INFERENCE_PROVIDER", "HERMES_INFERENCE_MODEL"):
        env.pop(k, None)

    mapper = Mapper(out, mcp_names)
    hermes = Hermes(env)
    pending: dict[int, Any] = {}
    prompting = {"on": False, "tool_seen": False}

    def feed(kind: str, value: Any) -> None:
        raw(kind, value)
        mapper.record(kind, value)

    def sync() -> None:
        rows = transcript.new_rows()
        if rows:
            feed("db", rows)

    def handle(item: tuple[str, Any]) -> None:
        kind, value = item
        if kind == "err":
            if prompting["on"] and (ERROR_LINE.search(value) or TURN_ENDED.search(value) or API_CALL_OK.search(value)):
                feed("log", value)
            return
        msg = value
        if "id" in msg and "method" in msg:              # a request from Hermes
            if msg["method"] == "session/request_permission":
                hermes.send({"jsonrpc": "2.0", "id": msg["id"], "result": allow(msg.get("params") or {})})
            else:
                hermes.send({"jsonrpc": "2.0", "id": msg["id"],
                             "error": {"code": -32601, "message": f"{msg['method']} is not offered by this client"}})
            return
        if "id" in msg:                                  # a response
            pending[msg["id"]] = msg
            return
        if msg.get("method") == "session/update" and prompting["on"]:
            update = (msg.get("params") or {}).get("update") or {}
            kind = update.get("sessionUpdate")
            if kind in ("available_commands_update", "usage_update", "current_mode_update"):
                return
            if kind in ("tool_call", "tool_call_update"):
                sync()
                prompting["tool_seen"] = True
            elif kind in ("agent_message_chunk", "agent_thought_chunk") and prompting["tool_seen"]:
                # the model is answering again: the tools it called have finished and are in the transcript
                sync()
                prompting["tool_seen"] = False
            feed("acp", msg)

    def call(method: str, params: dict[str, Any], deadline: float | None = None) -> dict[str, Any] | None:
        rid = hermes.request(method, params)
        cancel_sent = False
        while rid not in pending:
            if stop["at"] is not None:
                if method == "session/prompt" and not cancel_sent:
                    hermes.notify("session/cancel", {"sessionId": params["sessionId"]})
                    cancel_sent = True
                if time.monotonic() - stop["at"] > CANCEL_WAIT_S:
                    return None
            if hermes.eof and not hermes.bufs["out"]:
                return None
            if deadline is not None and time.monotonic() > deadline:
                return None
            for item in hermes.poll(0.25):
                handle(item)
        return pending.pop(rid)

    def finish(info: dict[str, Any]) -> None:
        sync()
        info.setdefault("transcript", transcript.ok)
        info["cache_write"] = transcript.cache_write() - transcript.cache_write0 if transcript.sid else 0
        raw("end", info)
        mapper.end(info)

    def crashed(what: str) -> dict[str, Any]:
        tail = " | ".join(x for x in hermes.stderr_tail[-5:] if x.strip())
        if stop["at"] is not None:
            return {"code": "cancelled", "message": "the turn was cancelled"}
        return {"code": "harness_error", "message": f"Hermes {what}" + (f": {tail[-1500:]}" if tail else "")}

    try:
        init = call("initialize", {"protocolVersion": 1, "clientInfo": {"name": "harnessgateway", "version": "1"},
                                   "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                                          "terminal": False}}, time.monotonic() + 120)
        if not init or "error" in init:
            finish({"failure": crashed("did not start (ACP initialize failed)")})
            return
        session = None
        if resuming:
            loaded = call("session/load", {"sessionId": sid, "cwd": WORKSPACE, "mcpServers": []},
                          time.monotonic() + 120)
            if loaded and "error" not in loaded and loaded.get("result") is not None:
                session = sid
            else:
                out.log(f"hermes: session/load {sid} failed ({json.dumps(loaded)[:500]}); starting a new session")
        if session is None:
            created = call("session/new", {"cwd": WORKSPACE, "mcpServers": []}, time.monotonic() + 120)
            if not created or "error" in created:
                finish({"failure": crashed("could not open a session") if not created else
                        {"code": "harness_error", "message": json.dumps(created.get("error"))[:2000]}})
                return
            session = created["result"]["sessionId"]
        state.set("hermes_session", session)
        state.set("hermes_hint", hint_sha)
        transcript.bind(session)
        call("session/set_mode", {"sessionId": session, "modeId": "dont_ask"}, time.monotonic() + 30)
        out.log(f"hermes: session {session} ({'resumed' if session == sid else 'new'}), model {model} via {transport}")

        prompting["on"] = True
        result = call("session/prompt", {"sessionId": session, "prompt": blocks})
        if result is None:
            finish({"failure": crashed("exited before the turn ended")})
        elif "error" in result and stop["at"] is not None:   # 0.19.0's cancel path can fail after stopping cleanly
            finish({"stopReason": "cancelled"})
        elif "error" in result:
            finish({"failure": {"code": "harness_error", "message": json.dumps(result["error"])[:2000]}})
        else:
            r = result.get("result") or {}
            finish({"stopReason": r.get("stopReason"), "usage": r.get("usage")})
    finally:
        if not out.ended:
            out.failed("harness_error", "the turn ended unexpectedly")
        hermes.close(2 if stop["at"] is not None else 8)


if __name__ == "__main__":
    main()
