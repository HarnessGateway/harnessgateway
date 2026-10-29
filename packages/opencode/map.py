"""OpenCode, one turn: the Responses request in, `opencode serve` driven over its HTTP API, Responses events out.

    run()      writes the turn's OpenCode config (one provider, `hg`, at $HG_PROVIDER_URL/v1 with the per-turn token;
               the model; the turn's MCP servers from $HG_MCP), places the request's files in the workspace, starts
               `opencode serve` on a loopback port in the workspace, subscribes to GET /event, opens the session kept
               in $HG_STATE (or a new one), sends the prompt with POST /session/<id>/prompt_async and feeds every
               event to a Mapper until the session is idle again. Permission asks are approved (the sandbox is the
               boundary), questions rejected (nobody is there to answer); SIGTERM aborts the session and stops the
               server
    Mapper     maps the turn's own session's events onto hgkit.Emitter (child sessions of the `task` tool add their
               usage, nothing else):
                 message.updated (assistant)            -> which message ids are the assistant's (user prompts come
                                                           back as parts too); info.error is the turn's error
                 message.part.delta field=text          -> output_text deltas, token by token, for text parts
                 message.part.updated text (time.end)   -> the message closes (any text the deltas missed first)
                 message.part.updated reasoning (end)   -> a reasoning item
                 message.part.updated tool              -> by tool, when it runs and when it ends:
                       bash                                shell_call (running) + shell_call_output (exit code, timeout)
                       write / edit / apply_patch          apply_patch_call + apply_patch_call_output (at the end)
                       <mcp server>_<tool>                 mcp_call (at the end, output or error)
                       anything else                       function_call (running) + function_call_output
                 message.part.updated step-finish       -> usage, summed over the turn (sub-agent sessions included)
                 session.error                          -> response.failed (APIError 429 -> rate_limit_exceeded), or
                                                           response.incomplete for MessageOutputLengthError
                 session.status retry                   -> a log line

Runs on the sandbox's python3; hgkit is on PYTHONPATH there.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from hgkit import Emitter, Request, State

PROVIDER = "hg"
AGENT = "build"                      # OpenCode's default primary agent
STATE_KEY = "opencode_session"
CONFIG = Path("/tmp/hg-opencode/opencode.json")
NOISE = {"plugin.added", "server.heartbeat", "catalog.updated", "reference.updated", "integration.updated",
         "file.watcher.updated", "lsp.updated", "lsp.client.diagnostics"}
ENV = {
    "OPENCODE_DISABLE_MODELS_FETCH": "1",   # no models.dev catalog download: the provider is defined here
    "OPENCODE_DISABLE_AUTOUPDATE": "1",     # a pinned CLI never replaces itself
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",   # no language-server downloads (no egress anyway)
    "NO_COLOR": "1",
}
# Anthropic's variants (thinking budgets) are high and max; OpenAI-shaped models get every effort as a variant.
ANTHROPIC_VARIANTS = {"high": "high", "xhigh": "max", "max": "max"}
ANTHROPIC_THINKING_OUTPUT = 32000    # max_tokens when thinking is asked for without max_output_tokens
OPENAI_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh"]
SHELL_TOOLS = {"bash", "shell"}
PATCH_TOOLS = {"write", "edit", "apply_patch"}
_SHELL_META = re.compile(r"\n*<shell_metadata>\n(.*?)\n</shell_metadata>\s*$", re.S)
_V4A_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$")
_V4A_OPS = {"Add": "create_file", "Update": "update_file", "Delete": "delete_file"}
IGNORED = ("tool_choice", "parallel_tool_calls", "truncation", "prompt_cache_key", "service_tier", "top_logprobs",
           "include", "prompt")


def _stderr(text: str) -> None:
    with contextlib.suppress(OSError, ValueError):
        sys.stderr.write(text)
        sys.stderr.flush()


def _plus(text: str) -> str:
    """A new file's content as an apply_patch (V4A) create_file diff: every line added."""
    return "".join(f"+{line}\n" for line in text.splitlines())


def mcp_key(label: str) -> str:
    """An MCP server_label as an OpenCode server key (OpenCode names the server's tools <key>_<tool>)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", label)


def v4a_files(patch: str) -> list[tuple[str, str, str]]:
    """An apply_patch patchText split per file: [(operation, path, that file's diff lines)]."""
    files: list[list[Any]] = []
    for line in patch.splitlines():
        m = _V4A_FILE.match(line)
        if m:
            files.append([_V4A_OPS[m.group(1)], m.group(2).strip(), []])
        elif files and not line.startswith("*** End Patch"):
            files[-1][2].append(line)
    return [(op, path, "".join(f"{x}\n" for x in body)) for op, path, body in files]


def error_text(err: Any) -> tuple[str, str]:
    """An OpenCode error object ({name, data: {message, statusCode, responseBody}}) as (name, message)."""
    if not isinstance(err, dict):
        return "UnknownError", str(err or "")
    data = err.get("data") if isinstance(err.get("data"), dict) else {}
    name = str(err.get("name") or "UnknownError")
    message = str(data.get("message") or err.get("message") or name)
    body = str(data.get("responseBody") or "").strip()
    if body and body[:200] not in message:
        message = f"{message}: {body[:1000]}"
    if data.get("statusCode") and str(data["statusCode"]) not in message:
        message = f"{message} (HTTP {data['statusCode']})"
    return name, message


class Mapper:
    """OpenCode bus events in (one decoded JSON object at a time), Responses events out (through the Emitter)."""

    def __init__(self, out: Emitter, session_id: str, mcp_servers: dict[str, str] | None = None):
        self.out = out
        self.session = session_id
        self.children: set[str] = set()
        self.labels = mcp_servers or {}                     # OpenCode server key -> the turn's MCP server label
        self.mcp = sorted(self.labels, key=len, reverse=True)   # longest first: "a_b" before "a"
        self.assistant: set[str] = set()                    # assistant message ids of this session
        self.parts: dict[str, str] = {}                     # part id -> part type
        self.streamed: dict[str, str] = {}                  # text part id -> text already emitted
        self.text_part: str | None = None                   # the text part open in the Emitter
        self.done: set[str] = set()                         # text/reasoning/tool parts already finished
        self.called: set[str] = set()                       # tool parts whose call item is out
        self.tokens = {"input": 0, "output": 0, "reasoning": 0, "read": 0, "write": 0}
        self.error: tuple[str, str] | None = None
        self.structured: Any = None

    # ── events ─────────────────────────────────────────────────────────────────────────────────────────────────────
    def feed(self, ev: dict[str, Any]) -> None:
        kind = ev.get("type")
        p = ev.get("properties") if isinstance(ev.get("properties"), dict) else {}
        if kind in ("session.created", "session.updated"):
            info = p.get("info") or {}
            if info.get("parentID") and (info["parentID"] == self.session or info["parentID"] in self.children):
                self.children.add(str(info.get("id")))
        elif kind == "message.updated":
            self._message(p.get("info") or {})
        elif kind == "message.part.updated":
            part = p.get("part") or {}
            if part.get("sessionID") == self.session:
                self._part(part)
            elif part.get("sessionID") in self.children and part.get("type") == "step-finish":
                self._usage(part.get("tokens"))
        elif kind == "message.part.delta":
            if p.get("sessionID") == self.session and p.get("field") == "text":
                self._delta(str(p.get("partID") or ""), str(p.get("delta") or ""))
        elif kind == "session.error":
            if p.get("sessionID") in (None, self.session) and p.get("error"):
                self.error = error_text(p["error"])
        elif kind == "session.status" and p.get("sessionID") == self.session:
            st = p.get("status") or {}
            if st.get("type") == "retry":
                self.out.log(f"opencode: retry {st.get('attempt')}: {st.get('message', '')}")

    def _message(self, info: dict[str, Any]) -> None:
        if info.get("sessionID") != self.session or info.get("role") != "assistant":
            return
        self.assistant.add(str(info.get("id")))
        if info.get("error"):
            self.error = error_text(info["error"])
        if info.get("structured") is not None:
            self.structured = info["structured"]

    def _delta(self, part_id: str, delta: str) -> None:
        if self.parts.get(part_id) != "text" or part_id in self.done or not delta:
            return
        if self.text_part != part_id:
            self.out.close_text()
            self.text_part = part_id
        self.streamed[part_id] = self.streamed.get(part_id, "") + delta
        self.out.text(delta)

    def _part(self, part: dict[str, Any]) -> None:
        pid, kind = str(part.get("id") or ""), part.get("type")
        if str(part.get("messageID")) not in self.assistant or pid in self.done:
            return
        self.parts[pid] = str(kind)
        ended = bool((part.get("time") or {}).get("end"))
        if kind == "text" and ended:
            if not part.get("synthetic") and not part.get("ignored"):
                text, sent = str(part.get("text") or ""), self.streamed.get(pid, "")
                rest = text[len(sent):] if text.startswith(sent) else ""
                self._delta(pid, rest)
            self.done.add(pid)
            if self.text_part == pid:
                self.out.close_text()
                self.text_part = None
        elif kind == "reasoning" and ended:
            self.done.add(pid)
            self.out.reasoning(str(part.get("text") or ""))
        elif kind == "tool":
            self._tool(pid, part)
        elif kind == "step-finish":
            self.done.add(pid)
            self._usage(part.get("tokens"))

    def _usage(self, t: Any) -> None:
        if not isinstance(t, dict):
            return
        cache = t.get("cache") if isinstance(t.get("cache"), dict) else {}
        for key, v in (("input", t.get("input")), ("output", t.get("output")), ("reasoning", t.get("reasoning")),
                       ("read", cache.get("read")), ("write", cache.get("write"))):
            if isinstance(v, (int, float)):
                self.tokens[key] += int(v)

    # ── tools ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def _mcp_server(self, tool: str) -> str | None:
        return next((s for s in self.mcp if tool.startswith(f"{s}_")), None)

    def _tool(self, pid: str, part: dict[str, Any]) -> None:
        st = part.get("state") or {}
        status = st.get("status")
        if status not in ("running", "completed", "error"):
            return
        name, cid = str(part.get("tool") or ""), str(part.get("callID") or pid)
        args = st.get("input") if isinstance(st.get("input"), dict) else {}
        deferred = name in PATCH_TOOLS or self._mcp_server(name) is not None
        if pid not in self.called and not deferred:
            self.called.add(pid)
            if name in SHELL_TOOLS:
                action: dict[str, Any] = {"commands": [str(args.get("command", ""))]}
                if isinstance(args.get("timeout"), (int, float)):
                    action["timeout_ms"] = int(args["timeout"])
                self.out.item({"type": "shell_call", "call_id": cid, "status": "completed", "action": action})
            else:
                self.out.item({"type": "function_call", "call_id": cid, "name": name,
                               "arguments": json.dumps(args, ensure_ascii=False), "status": "completed"})
        if status == "running":
            return
        self.done.add(pid)
        failed = status == "error"
        output = str(st.get("error") if failed else st.get("output") or "")
        meta = st.get("metadata") if isinstance(st.get("metadata"), dict) else {}
        if name in SHELL_TOOLS:
            self._shell_output(cid, output, meta, failed)
        elif name in PATCH_TOOLS:
            self._patch(cid, name, args, meta, output, failed)
        elif deferred:
            server = self._mcp_server(name) or ""
            self.out.mcp(self.labels.get(server, server), name[len(server) + 1:], args, output=None if failed else output,
                         error=output if failed else None)
        else:
            self.out.item({"type": "function_call_output", "call_id": cid, "output": output,
                           "status": "incomplete" if failed else "completed"})

    def _shell_output(self, cid: str, output: str, meta: dict[str, Any], failed: bool) -> None:
        m = _SHELL_META.search(output)
        notes = m.group(1) if m else ""
        stdout = output[:m.start()] if m else output
        code = meta.get("exit")
        if "exceeding timeout" in notes:
            outcome: dict[str, Any] = {"type": "timeout"}
        else:
            outcome = {"type": "exit", "exit_code": code if isinstance(code, int) else (1 if failed or notes else 0)}
        self.out.item({"type": "shell_call_output", "call_id": cid, "status": "completed",
                       "output": [{"stdout": "" if failed else stdout, "stderr": "\n".join(x for x in (
                           output if failed else "", notes) if x), "outcome": outcome}]})

    def _patch(self, cid: str, name: str, args: dict[str, Any], meta: dict[str, Any], output: str,
               failed: bool) -> None:
        if name == "apply_patch":
            ops = v4a_files(str(args.get("patchText") or args.get("patch") or "")) or [("update_file", "", "")]
        elif name == "write":
            op = "update_file" if meta.get("exists") else "create_file"
            ops = [(op, str(args.get("filePath") or meta.get("filepath") or ""), _plus(str(args.get("content", ""))))]
        else:
            diff = str(meta.get("diff") or "") or (
                "".join(f"-{x}\n" for x in str(args.get("oldString", "")).splitlines()) +
                "".join(f"+{x}\n" for x in str(args.get("newString", "")).splitlines()))
            ops = [("update_file", str(args.get("filePath") or ""), diff)]
        for i, (op, path, diff) in enumerate(ops):
            self.out.patch(op, path, diff, ok=not failed, output=output, call_id=cid if i == 0 else f"{cid}_{i}")

    # ── the end ────────────────────────────────────────────────────────────────────────────────────────────────────
    def usage(self) -> dict[str, Any] | None:
        t = self.tokens
        if not any(t.values()):
            return None
        return Emitter.usage(input_tokens=t["input"] + t["read"] + t["write"], output_tokens=t["output"] + t["reasoning"],
                             cached=t["read"], cache_write=t["write"], reasoning=t["reasoning"])

    def finish(self, failure: str = "") -> None:
        """Exactly one terminal event: the session's error, else `failure` (the server died or never answered), else
        completed. With text.format, the structured result is the answer when the model put it nowhere else."""
        if self.structured is not None and not self.error and not self.streamed:
            self.out.message(json.dumps(self.structured, ensure_ascii=False))
        usage = self.usage()
        if self.error:
            name, message = self.error
            low = message.lower()
            if name == "MessageOutputLengthError":
                self.out.incomplete("max_output_tokens", usage=usage)
            elif name == "ContextOverflowError":
                self.out.failed("context_length_exceeded", message[:4000], usage=usage)
            else:
                code = "rate_limit_exceeded" if ("429" in low or "rate limit" in low) else "provider_error"
                self.out.failed(code, message[:4000], usage=usage)
        elif failure:
            self.out.failed("harness_error", failure[-4000:], usage=usage)
        else:
            self.out.completed(usage=usage, metadata={"opencode_session": self.session})


# ── the request -> OpenCode ────────────────────────────────────────────────────────────────────────────────────────
def variant(req: Request) -> str:
    effort = req.reasoning_effort
    if os.environ.get("HG_PROVIDER_API") == "anthropic":
        return ANTHROPIC_VARIANTS.get(effort, "")
    return effort if effort in OPENAI_EFFORTS else ("xhigh" if effort == "max" else "")


def provider_block(req: Request) -> dict[str, Any]:
    model, shape = os.environ.get("HG_MODEL", ""), os.environ.get("HG_PROVIDER_API", "anthropic")
    if shape == "anthropic":
        npm = "@ai-sdk/anthropic"
    else:
        wire = os.environ.get("OPENCODE_OPENAI_API", "").strip() or (
            "responses" if re.match(r"(gpt-|o\d|codex)", model) else "chat")
        npm = "@ai-sdk/openai" if wire == "responses" else "@ai-sdk/openai-compatible"
    spec: dict[str, Any] = {"name": model, "tool_call": True, "reasoning": True, "attachment": True,
                            "modalities": {"input": ["text", "image", "pdf"], "output": ["text"]}}
    if shape != "anthropic" and req.reasoning_effort:
        spec["reasoning_options"] = [{"type": "effort", "values": OPENAI_EFFORTS}]
    mot = req.get("max_output_tokens")
    if isinstance(mot, int) and mot > 0:
        spec["limit"] = {"context": 0, "output": mot}
    elif shape == "anthropic" and variant(req):
        # A thinking budget is derived from the output limit (unknown, 0, for a custom model: budget -1).
        spec["limit"] = {"context": 0, "output": ANTHROPIC_THINKING_OUTPUT}
    return {"npm": npm, "name": "HarnessGateway", "models": {model: spec},
            # Every AI-SDK package appends its own path (/messages, /responses, /chat/completions) to a base with /v1.
            "options": {"baseURL": os.environ.get("HG_PROVIDER_URL", "").rstrip("/") + "/v1",
                        "apiKey": "{env:HG_PROVIDER_TOKEN}"}}


def mcp_block() -> tuple[dict[str, Any], dict[str, bool], dict[str, str]]:
    """The turn's MCP servers ($HG_MCP) as remote servers keyed by mcp_key(label), the tool rules their allowed_tools
    make (every other tool of that server off), and key -> label. Header values are placeholders the door replaces on
    the way out, so they may be written as they are."""
    servers: dict[str, Any] = {}
    rules: dict[str, bool] = {}
    labels: dict[str, str] = {}
    for s in Request.mcp_servers():
        key = mcp_key(str(s["label"]))
        labels[key] = str(s["label"])
        entry: dict[str, Any] = {"type": "remote", "url": s["url"], "enabled": True, "oauth": False}
        if s.get("headers"):
            entry["headers"] = dict(s["headers"])
        servers[key] = entry
        if s.get("allowed_tools") is not None:
            rules[f"{key}_*"] = False
            rules.update({f"{key}_{n}": True for n in s["allowed_tools"]})
    return servers, rules, labels


def write_config(req: Request, path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    model = os.environ.get("HG_MODEL", "")
    agent: dict[str, Any] = {}
    for field, key in (("max_tool_calls", "steps"), ("temperature", "temperature"), ("top_p", "top_p")):
        v = req.get(field)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            agent[key] = v
    cfg: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {PROVIDER: provider_block(req)},
        "enabled_providers": [PROVIDER],          # no built-in provider (and none of their network calls)
        "model": f"{PROVIDER}/{model}",
        "small_model": f"{PROVIDER}/{model}",     # titles and summaries on the same model, never another provider
        "autoupdate": False,
        "share": "disabled",
        "snapshot": False,                        # the undo history is a git copy of the workspace: not needed here
        "permission": {"question": "deny"},       # nobody can answer a question mid-turn
    }
    servers, rules, labels = mcp_block()
    if servers:
        cfg["mcp"] = servers
    if rules:
        cfg["tools"] = rules
    if agent:
        cfg["agent"] = {AGENT: agent}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2))
    path.chmod(0o600)
    return cfg, labels


def prompt_parts(req: Request, names: list[str]) -> list[dict[str, Any]]:
    """The files placed in the workspace (`names`) as OpenCode file parts, then the prompt text."""
    workspace = Path(os.environ.get("HG_WORKSPACE", "/workspace"))
    parts: list[dict[str, Any]] = [{"type": "file", "mime": mimetypes.guess_type(n)[0] or "text/plain", "filename": n,
                                    "url": (workspace / n).as_uri()} for n in names]
    if req.prompt:
        parts.append({"type": "text", "text": req.prompt})
    return parts


def prompt_body(req: Request, parts: list[dict[str, Any]]) -> dict[str, Any]:
    body: dict[str, Any] = {"parts": parts, "model": {"providerID": PROVIDER, "modelID": os.environ.get("HG_MODEL", "")}}
    if req.instructions:
        body["system"] = req.instructions
    v = variant(req)
    if v:
        body["variant"] = v
    fmt = req.text_format or {}
    if fmt.get("type") == "json_schema" and isinstance(fmt.get("schema"), dict):
        body["format"] = {"type": "json_schema", "schema": fmt["schema"]}
    return body


# ── running the server ─────────────────────────────────────────────────────────────────────────────────────────────
class Server:
    """`opencode serve` on a free loopback port, in the workspace, in a process group of its own."""

    def __init__(self, env: dict[str, str]):
        self.proc = subprocess.Popen(["opencode", "serve", "--hostname", "127.0.0.1", "--port", "0"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=env, cwd=os.environ.get("HG_WORKSPACE", "/workspace"),
                                     start_new_session=True)
        self.tail = bytearray()
        self.port = 0
        threading.Thread(target=self._pump, args=(self.proc.stderr,), daemon=True).start()
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace")
            self._keep(raw)
            m = re.search(r"listening on http://[^\s:]+:(\d+)", line)
            if m:
                self.port = int(m.group(1))
                break
        threading.Thread(target=self._pump, args=(self.proc.stdout,), daemon=True).start()

    def _keep(self, chunk: bytes) -> None:
        _stderr(chunk.decode("utf-8", "replace"))
        self.tail.extend(chunk)
        del self.tail[:-8192]

    def _pump(self, stream: Any) -> None:
        for chunk in iter(lambda: stream.read1(65536), b""):
            self._keep(chunk)

    def call(self, method: str, path: str, body: Any = None, timeout: float = 30) -> tuple[int, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(method, path, json.dumps(body) if body is not None else None,
                         {"content-type": "application/json"})
            r = conn.getresponse()
            data = r.read()
            return r.status, (json.loads(data) if data.strip() else None)
        finally:
            conn.close()

    def events(self) -> Iterator[dict[str, Any]]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=None)
        conn.request("GET", "/event", headers={"accept": "text/event-stream"})
        resp = conn.getresponse()
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("data:"):
                with contextlib.suppress(ValueError):
                    yield json.loads(line[5:])

    def stop(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.proc.pid, signal.SIGTERM)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)

    def stderr_tail(self) -> str:
        return self.tail.decode("utf-8", "replace").strip()


def open_session(server: Server, req: Request, state: State, out: Emitter) -> str:
    """The session kept in $HG_STATE if OpenCode still has it (its database persists with the session), else new."""
    saved = state.get(STATE_KEY) if req.get("store", True) is not False else ""
    if saved:
        status, _ = server.call("GET", f"/session/{saved}")
        if status == 200:
            return saved
        out.log(f"opencode: session {saved} not found ({status}); starting a new one")
    # A title of our own: OpenCode would otherwise spend a model call naming the session.
    status, info = server.call("POST", "/session", {"title": f"HarnessGateway {os.environ.get('HG_SESSION', '')}"})
    if status != 200 or not isinstance(info, dict) or not info.get("id"):
        raise RuntimeError(f"could not create an OpenCode session: HTTP {status} {info}")
    if req.get("store", True) is not False:
        state.set(STATE_KEY, info["id"])
    return str(info["id"])


def ignored(req: Request) -> list[str]:
    """Request fields OpenCode cannot honour, when present."""
    fields = [f for f in IGNORED if req.get(f) is not None]
    fields += [f"{k}.{sub}" for k, sub in (("text", "verbosity"), ("reasoning", "summary"))
               if isinstance(req.get(k), dict) and req.get(k).get(sub) is not None]
    if any(isinstance(t, dict) and t.get("type") == "function" for t in req.get("tools") or []):
        fields.append("tools.function")
    return fields


def run(out: Emitter) -> None:
    req = Request.load()
    out.ignored(ignored(req))
    parts = prompt_parts(req, req.place_inputs())
    if not parts:
        out.failed("invalid_prompt", "the request has no input text or files for OpenCode")
        return
    if not os.environ.get("HG_PROVIDER_URL"):
        out.failed("harness_error", "OpenCode needs a provider: call it as opencode/<provider>/<model>")
        return
    env = {**os.environ, **ENV, "OPENCODE_CONFIG": str(CONFIG)}
    _, labels = write_config(req, CONFIG)
    raw = os.environ.get("OPENCODE_RAW_EVENTS") == "1"

    server = Server(env)
    if not server.port:
        server.proc.wait()
        out.failed("harness_error", server.stderr_tail()[-2000:] or "opencode serve did not start")
        return
    session = ""

    def stop(*_: Any) -> None:                         # cancel: abort the session, stop the server, leave
        if session:
            with contextlib.suppress(Exception):
                server.call("POST", f"/session/{session}/abort", timeout=3)
        server.stop()
        os._exit(143)

    signal.signal(signal.SIGTERM, stop)
    failure = ""
    try:
        stream = server.events()
        next(stream)                                   # server.connected: subscribed before anything happens
        session = open_session(server, req, State(), out)
        mapper = Mapper(out, session, labels)
        status, err = server.call("POST", f"/session/{session}/prompt_async", prompt_body(req, parts))
        if status >= 300:
            raise RuntimeError(f"OpenCode refused the prompt: HTTP {status} {json.dumps(err)[:2000]}")
        for ev in stream:
            kind = ev.get("type")
            if kind in NOISE:
                continue
            if raw:
                _stderr("OPENCODE_RAW " + json.dumps(ev, ensure_ascii=False) + "\n")
            p = ev.get("properties") or {}
            if kind == "permission.asked" and p.get("sessionID") in {session, *mapper.children}:
                server.call("POST", f"/permission/{p['id']}/reply", {"reply": "once"})
            elif kind == "question.asked":
                server.call("POST", f"/question/{p['id']}/reject", {})
            mapper.feed(ev)
            if kind == "session.status" and p.get("sessionID") == session and (p.get("status") or {}).get("type") == "idle":
                break
        else:
            failure = "OpenCode's event stream ended before the turn did\n" + server.stderr_tail()[-2000:]
    except (OSError, RuntimeError, StopIteration, http.client.HTTPException) as e:
        failure = f"{e}\n{server.stderr_tail()[-2000:]}"
        if not session:
            out.failed("harness_error", failure.strip()[-4000:])
            server.stop()
            return
    mapper.finish(failure.strip())
    server.stop()


def main() -> None:
    out = Emitter()
    try:
        run(out)
    except Exception as e:
        import traceback
        traceback.print_exc()
        if not out.ended:
            out.failed("harness_error", f"the opencode package failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
