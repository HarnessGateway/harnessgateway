"""Claude Code, one turn: the Responses request in, `claude -p --output-format stream-json` run, Responses events out.

    run()      builds the CLI's command line and environment from the request, runs it in the workspace and feeds
               every stdout line to a Mapper (each raw line is also kept in the turn's log, prefixed `claude-stream `)
    Mapper     maps Claude Code's stream-json onto hgkit.Emitter:
                 system init / result                    -> the CLI's session id into $HG_STATE/session (--resume)
                 stream_event content_block_delta text   -> response.output_text.delta (token level)
                 thinking (deltas, then the whole block) -> a reasoning item, its summary text streamed as it comes
                 tool_use Bash                           -> shell_call, its tool_result -> shell_call_output
                 tool_use Write / Edit / MultiEdit       -> apply_patch_call + apply_patch_call_output (at the result)
                 tool_use mcp__<server>__<tool>          -> mcp_call (added at the call, done at the result)
                 any other tool_use                      -> function_call, its tool_result -> function_call_output
                 result                                  -> usage on the terminal event; success -> completed,
                                                            error_max_turns -> incomplete, anything else -> failed

Runs on the sandbox's python3 (3.11); hgkit is on PYTHONPATH there.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from hgkit import Emitter, Request, State, input_files

CLI_VERSION = "2.1.310"
WORKSPACE = os.environ.get("HG_WORKSPACE", "/workspace")
RAW_PREFIX = "claude-stream "
# Responses reasoning.effort -> Claude Code --effort (low, medium, high, xhigh, max); "none" turns thinking off.
EFFORT = {"minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"}
# Every variable Claude Code resolves a model alias or a background model from: all pinned to the request's model,
# so the CLI never asks the provider for a model the request did not name.
MODEL_ENV = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
             "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
             "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL", "CLAUDE_CODE_AUTO_MODE_MODEL",
             "CLAUDE_CODE_BG_CLASSIFIER_MODEL")
PATCH_TOOLS = ("Write", "Edit", "MultiEdit")


def _rel(path: str) -> str:
    """A workspace path as the client sees it (relative to the workspace); anything else stays absolute."""
    p = str(path or "")
    root = WORKSPACE.rstrip("/") + "/"
    return p[len(root):] if p.startswith(root) else p


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    out.append(str(c.get("text", "")))
                elif c.get("type") == "image":
                    out.append("[image]")
                elif c.get("type") == "tool_reference":
                    out.append(f"[tool loaded: {c.get('tool_name', '')}]")
        return "\n".join(out)
    return "" if content is None else json.dumps(content, ensure_ascii=False)


def _plus(content: str) -> str:
    """A new file's content as an apply_patch create diff: every line added."""
    body = content[:-1] if content.endswith("\n") else content
    return "\n".join("+" + line for line in body.split("\n")) if content else ""


def _hunks(patch: Any) -> str:
    """Claude Code's structuredPatch (a list of unified-diff hunks) as diff text."""
    out = []
    for h in patch if isinstance(patch, list) else []:
        if isinstance(h, dict):
            out.append(f"@@ -{h.get('oldStart', 0)},{h.get('oldLines', 0)} +{h.get('newStart', 0)},{h.get('newLines', 0)} @@")
            out.extend(str(x) for x in h.get("lines") or [])
    return "\n".join(out)


def _mcp_name(server: str) -> str:
    """How Claude Code normalizes an MCP server name into its tool names (mcp__<name>__<tool>)."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", server)


class Mapper:
    """Claude Code stream-json lines in, Responses events out (through the Emitter)."""

    def __init__(self, out: Emitter, state: State | None = None, mcp_labels: list[str] | None = None):
        self.out = out
        self.state = state
        self.labels = {_mcp_name(x): x for x in mcp_labels or []}
        self.calls: dict[str, dict[str, Any]] = {}    # tool_use id -> {name, input, kind, item, index}
        self.streamed: set[str] = set()                # assistant message ids whose text arrived as deltas
        self.current = ""                              # the message id the stream_events belong to
        self.session_id = ""
        self.result: dict[str, Any] | None = None
        self.api_error = ""                            # the text of a synthetic API-error message, if any
        self.init_seen = False
        self.thought: set[str] = set()                 # assistant message ids whose thinking was streamed
        self.rs: dict[str, Any] | None = None          # the reasoning item being streamed

    # ── input ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def line(self, raw: str) -> None:
        s = raw.strip()
        if not s:
            return
        try:
            ev = json.loads(s)
        except ValueError:
            self.out.log(f"claude: {s}")
            return
        if isinstance(ev, dict):
            try:
                self.handle(ev)
            except Exception as e:  # a CLI event this mapper does not expect must not lose the turn
                self.out.log(f"map.py: could not map a {ev.get('type')} event: {type(e).__name__}: {e}")

    def handle(self, ev: dict[str, Any]) -> None:
        kind = ev.get("type")
        sub = ev.get("parent_tool_use_id")             # set on a subagent's messages
        if kind in ("user", "result") or (kind == "assistant" and not self._thinking_only(ev)):
            self.close_reasoning()
        if kind == "system" and ev.get("subtype") == "init":
            self.init_seen = True
            self._remember(ev.get("session_id"))
        elif kind == "stream_event" and not sub:
            self._stream(ev.get("event") or {})
        elif kind == "assistant":
            self._assistant(ev, bool(sub))
        elif kind == "user":
            content = (ev.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self._tool_result(block, ev.get("tool_use_result"))
        elif kind == "result":
            self.result = ev
            self._remember(ev.get("session_id"))

    def _remember(self, sid: Any) -> None:
        if isinstance(sid, str) and sid:
            self.session_id = sid
            if self.state is not None:
                self.state.set("session", sid)

    def _stream(self, e: dict[str, Any]) -> None:
        t = e.get("type")
        if t == "message_start":
            self.current = str((e.get("message") or {}).get("id") or "")
        elif t == "content_block_delta":
            d = e.get("delta") or {}
            if d.get("type") == "text_delta" and d.get("text"):
                self.close_reasoning()
                self.streamed.add(self.current)
                self.out.text(d["text"])
            elif d.get("type") == "thinking_delta" and d.get("thinking"):
                self._think(d["thinking"])
        elif t == "content_block_stop":
            self.close_reasoning()

    # ── reasoning, streamed as a summary ───────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _thinking_only(ev: dict[str, Any]) -> bool:
        blocks = (ev.get("message") or {}).get("content")
        return isinstance(blocks, list) and bool(blocks) and \
            all(isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking") for b in blocks)

    def _think(self, delta: str) -> None:
        if self.rs is None:
            self.out.close_text()
            self.rs = {"id": f"rs_{os.urandom(12).hex()}", "index": self.out.index, "text": ""}
            self.out.index += 1
            base = {"item_id": self.rs["id"], "output_index": self.rs["index"]}
            self.out.emit({"type": "response.output_item.added", "output_index": self.rs["index"],
                           "item": {"id": self.rs["id"], "type": "reasoning", "summary": []}})
            self.out.emit({"type": "response.reasoning_summary_part.added", **base, "summary_index": 0,
                           "part": {"type": "summary_text", "text": ""}})
        self.thought.add(self.current)
        self.rs["text"] += delta
        self.out.emit({"type": "response.reasoning_summary_text.delta", "item_id": self.rs["id"],
                       "output_index": self.rs["index"], "summary_index": 0, "delta": delta})

    def close_reasoning(self, text: str | None = None) -> None:
        rs, self.rs = self.rs, None
        if rs is None:
            return
        text = rs["text"] if text is None else text
        base = {"item_id": rs["id"], "output_index": rs["index"], "summary_index": 0}
        part = {"type": "summary_text", "text": text}
        self.out.emit({"type": "response.reasoning_summary_text.done", **base, "text": text})
        self.out.emit({"type": "response.reasoning_summary_part.done", **base, "part": part})
        item = {"id": rs["id"], "type": "reasoning", "summary": [part]}
        self.out.emit({"type": "response.output_item.done", "output_index": rs["index"], "item": item})
        self.out.output.append(item)

    def _assistant(self, ev: dict[str, Any], subagent: bool) -> None:
        msg = ev.get("message") or {}
        blocks = msg.get("content") if isinstance(msg.get("content"), list) else []
        if ev.get("error") or msg.get("model") == "<synthetic>":
            # The CLI's own message (an API error, "Prompt is too long", ...): kept for the failure message, not
            # streamed as if the model had said it.
            self.api_error = "\n".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict)).strip()
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text" and not subagent:
                if msg.get("id") not in self.streamed:          # not already streamed as deltas
                    self.out.text(str(block.get("text") or ""))
            elif bt == "thinking" and not subagent:
                if self.rs is not None:                          # streamed: close it with the whole block
                    self.close_reasoning(str(block.get("thinking") or self.rs["text"]))
                elif msg.get("id") not in self.thought:
                    self.out.reasoning(str(block.get("thinking") or ""))
            elif bt == "tool_use":
                self._tool_use(block)

    # ── tools ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def _tool_use(self, block: dict[str, Any]) -> None:
        name = str(block.get("name") or "")
        args = block.get("input") if isinstance(block.get("input"), dict) else {}
        cid = str(block.get("id") or f"call_{len(self.calls) + 1}")
        call: dict[str, Any] = {"name": name, "input": args}
        self.calls[cid] = call
        if name == "Bash":
            action: dict[str, Any] = {"commands": [str(args.get("command", ""))]}
            if isinstance(args.get("timeout"), int):
                action["timeout_ms"] = args["timeout"]
            call["kind"] = "shell"
            self.out.item({"type": "shell_call", "call_id": cid, "status": "completed", "action": action})
        elif name in PATCH_TOOLS:
            call["kind"] = "patch"                                   # emitted with its result (create vs update)
        elif name == "StructuredOutput":
            call["kind"] = "structured"                              # becomes the final message (text.format)
        elif name.startswith("mcp__"):
            _, server, tool = (name.split("__", 2) + ["", ""])[:3]
            self.out.close_text()
            item = {"id": f"mcp_{cid[-24:]}", "type": "mcp_call", "server_label": self.labels.get(server, server),
                    "name": tool, "arguments": json.dumps(args, ensure_ascii=False), "status": "in_progress"}
            call.update(kind="mcp", item=item, index=self.out.index)
            self.out.emit({"type": "response.output_item.added", "output_index": self.out.index, "item": item})
            self.out.index += 1
        else:
            call["kind"] = "function"
            self.out.item({"type": "function_call", "call_id": cid, "name": name,
                           "arguments": json.dumps(args, ensure_ascii=False), "status": "completed"})

    def _tool_result(self, block: dict[str, Any], tur: Any) -> None:
        cid = str(block.get("tool_use_id") or "")
        text = _text(block.get("content"))
        error = bool(block.get("is_error"))
        call = self.calls.pop(cid, {"name": "", "input": {}, "kind": "function"})
        kind, args = call["kind"], call["input"]
        tur = tur if isinstance(tur, dict) else {}
        if kind == "shell":
            if "stdout" in tur or "stderr" in tur:
                stdout, stderr = str(tur.get("stdout") or ""), str(tur.get("stderr") or "")
            else:
                stdout, stderr = ("", text) if error else (text, "")
            m = re.match(r"Exit code (\d+)", text)
            code = int(m.group(1)) if m else (1 if error else 0)
            outcome = {"type": "timeout"} if tur.get("interrupted") else {"type": "exit", "exit_code": code}
            self.out.item({"type": "shell_call_output", "call_id": cid, "status": "completed",
                           "output": [{"stdout": stdout, "stderr": stderr, "outcome": outcome}]})
        elif kind == "patch":
            path = _rel(str(args.get("file_path") or tur.get("filePath") or ""))
            if call["name"] == "Write" and tur.get("type") != "update":
                op = {"type": "create_file", "path": path, "diff": _plus(str(args.get("content") or ""))}
            else:
                diff = _hunks(tur.get("structuredPatch"))
                if not diff:
                    edits = args.get("edits") if isinstance(args.get("edits"), list) else [args]
                    diff = "\n".join(
                        "@@\n" + "\n".join("-" + x for x in str(e.get("old_string", "")).split("\n")) + "\n" +
                        "\n".join("+" + x for x in str(e.get("new_string", e.get("content", ""))).split("\n"))
                        for e in edits if isinstance(e, dict))
                op = {"type": "update_file", "path": path, "diff": diff}
            self.out.item({"type": "apply_patch_call", "call_id": cid, "status": "completed", "operation": op})
            self.out.item({"type": "apply_patch_call_output", "call_id": cid,
                           "status": "failed" if error else "completed", "output": text})
        elif kind == "mcp":
            self.out.close_text()
            item = {**call["item"], "status": "failed" if error else "completed",
                    "output": None if error else text,
                    "error": {"type": "mcp_tool_execution_error", "content": text} if error else None}
            self.out.emit({"type": "response.output_item.done", "output_index": call["index"], "item": item})
            self.out.output.append(item)
        elif kind == "structured":
            pass
        else:
            self.out.item({"type": "function_call_output", "call_id": cid, "output": text,
                           "status": "incomplete" if error else "completed"})

    # ── the end ────────────────────────────────────────────────────────────────────────────────────────────────────
    def usage(self) -> dict[str, Any] | None:
        u = (self.result or {}).get("usage")
        if not isinstance(u, dict):
            return None
        cached = int(u.get("cache_read_input_tokens") or 0)
        written = int(u.get("cache_creation_input_tokens") or 0)
        # Anthropic's input_tokens leaves out cache reads and writes; OpenAI's includes them.
        return Emitter.usage(input_tokens=int(u.get("input_tokens") or 0) + cached + written,
                             output_tokens=int(u.get("output_tokens") or 0), cached=cached, cache_write=written)

    def finish(self, exit_code: int | None = None, stderr: str = "") -> None:
        """The terminal event, from the CLI's result (or its absence)."""
        self.close_reasoning()
        for cid in list(self.calls):                    # calls that never got a result (interrupted)
            call = self.calls.pop(cid)
            if call["kind"] == "mcp":
                self.out.close_text()
                item = {**call["item"], "status": "incomplete"}
                self.out.emit({"type": "response.output_item.done", "output_index": call["index"], "item": item})
                self.out.output.append(item)
        r = self.result
        if r is None:
            tail = (self.api_error or stderr.strip() or "no output")[-1500:]
            self.out.failed("cli_error", f"claude exited {exit_code} without a result: {tail}")
            return
        if r.get("structured_output") is not None:
            self.out.message(json.dumps(r["structured_output"], ensure_ascii=False))
        usage = self.usage()
        meta = {"claude_session_id": self.session_id, "claude_num_turns": str(r.get("num_turns", "")),
                "claude_version": CLI_VERSION}
        subtype = str(r.get("subtype") or "")
        if subtype == "success" and not r.get("is_error"):
            self.out.completed(usage=usage, metadata=meta)
        elif subtype == "error_max_turns":
            # the CLI stopped at --max-turns (the request's max_tool_calls): what it did so far, marked incomplete
            self.out._end("response.incomplete", {"incomplete_details": {"reason": "max_messages"}, "usage": usage,
                                                  "metadata": {**meta, "claude_stop": subtype}})
        else:
            errors = r.get("errors") if isinstance(r.get("errors"), list) else []
            message = str(r.get("result") or "").strip() or "; ".join(str(x) for x in errors) or self.api_error \
                or f"claude ended with {subtype or 'an error'}"
            code = "provider_error" if message.startswith("API Error") else (subtype or "cli_error")
            self.out.failed(code, message[:4000], usage=usage)


# ── running the CLI ────────────────────────────────────────────────────────────────────────────────────────────────
def stage_files(req: Request) -> list[str]:
    """input_file / input_image parts, by file_id or inline, saved into the workspace; their workspace names."""
    names: list[str] = []
    for meta in input_files().values():
        name = os.path.basename(str(meta.get("filename") or "")) or Path(meta["path"]).name
        Path(WORKSPACE, name).write_bytes(Path(meta["path"]).read_bytes())
        names.append(name)
    for name, data in req.inline_files:
        Path(WORKSPACE, name).write_bytes(data)
        names.append(name)
    return names


def mcp_config(req: Request) -> dict[str, Any] | None:
    servers: dict[str, Any] = {}
    for t in req.mcp_servers:
        label, url = t.get("server_label"), t.get("server_url")
        if not label or not url:
            continue
        headers = dict(t.get("headers") or {})
        if t.get("authorization"):
            headers["Authorization"] = f"Bearer {t['authorization']}"
        entry: dict[str, Any] = {"type": "http", "url": url}
        if headers:
            entry["headers"] = headers
        servers[label] = entry
    return {"mcpServers": servers} if servers else None


def command(req: Request, prompt_note: str = "", resume: str = "", tmp: str = "/tmp/hg-claude") -> list[str]:  # noqa: S108 (the sandbox's own tmpfs)
    model = os.environ.get("HG_MODEL", "")
    argv = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--dangerously-skip-permissions", "--system-prompt-snapshot", "off"]
    if model:
        argv += ["--model", model]
    if resume:
        argv += ["--resume", resume]
    Path(tmp).mkdir(mode=0o700, parents=True, exist_ok=True)
    if req.instructions.strip():
        f = Path(tmp, "instructions.md")
        f.write_text(req.instructions)
        argv += ["--append-system-prompt-file", str(f)]
    effort = EFFORT.get(req.reasoning_effort)
    if effort:
        argv += ["--effort", effort]
    fmt = (req.get("text") or {}).get("format") or {}
    if fmt.get("type") == "json_schema" and isinstance(fmt.get("schema"), dict):
        argv += ["--json-schema", json.dumps(fmt["schema"], separators=(",", ":"))]
    mcp = mcp_config(req)
    if mcp:
        f = Path(tmp, "mcp.json")
        f.touch(mode=0o600)
        f.write_text(json.dumps(mcp))
        argv += ["--mcp-config", str(f), "--strict-mcp-config"]
    max_calls = req.get("max_tool_calls")
    if isinstance(max_calls, int) and max_calls > 0:
        argv += ["--max-turns", str(max_calls)]
    return argv


def environment(req: Request) -> dict[str, str]:
    env = dict(os.environ)
    model = env.get("HG_MODEL", "")
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env.update({"ANTHROPIC_BASE_URL": env.get("HG_PROVIDER_URL", ""), "ANTHROPIC_API_KEY": env.get("HG_PROVIDER_TOKEN", ""),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
                "DISABLE_ERROR_REPORTING": "1", "DISABLE_BUG_COMMAND": "1", "IS_SANDBOX": "1",
                "CLAUDE_CODE_MAX_RETRIES": env.get("CLAUDE_CODE_MAX_RETRIES", "3")})
    if model:
        env.update({k: model for k in MODEL_ENV})
    if req.reasoning_effort == "none":
        env["CLAUDE_CODE_DISABLE_THINKING"] = "1"
    for k in ("NO_PROXY", "no_proxy"):                 # the provider gateway is reached directly, never via the proxy
        env[k] = ",".join(x for x in (env.get(k, ""), "hg-egress") if x)
    return env


def _descendants(root: int) -> list[int]:
    """Every process below `root`, from /proc. Claude Code runs each tool command in a process group of its own, so a
    signal to the turn's group does not reach them; stopping the turn walks the tree instead."""
    children: dict[int, list[int]] = {}
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                ppid = int((d / "stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
            children.setdefault(ppid, []).append(int(d.name))
    out, todo = [], [root]
    while todo:
        for c in children.get(todo.pop(), []):
            out.append(c)
            todo.append(c)
    return out


_seen: set[int] = set()            # descendants seen while the turn ran: an orphaned tool process is still stopped


def _watch_tree() -> None:
    while True:
        _seen.intersection_update(p for p in _seen if _alive(p))
        _seen.update(_descendants(os.getpid()))
        time.sleep(0.5)


def _alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def _stop_tree(signum: int, _frame: Any) -> None:
    """SIGTERM (cancel, timeout): stop the CLI and everything it started, then exit."""
    pids = sorted(set(_descendants(os.getpid())) | _seen)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, sig)
        deadline = time.monotonic() + 3
        while sig == signal.SIGTERM and time.monotonic() < deadline and any(map(_alive, pids)):
            time.sleep(0.1)
        pids = [p for p in pids if _alive(p)]
        if not pids:
            break
    os._exit(128 + signum)


def _run(argv: list[str], env: dict[str, str], prompt: str, mapper: Mapper) -> tuple[int, str]:
    proc = subprocess.Popen(argv, cwd=WORKSPACE, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
    tail: list[str] = []

    def drain() -> None:
        for chunk in proc.stderr:  # type: ignore[union-attr]
            sys.stderr.write(chunk)
            sys.stderr.flush()
            tail.append(chunk)
            del tail[:-50]

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    try:
        proc.stdin.write(prompt)  # type: ignore[union-attr]
        proc.stdin.close()  # type: ignore[union-attr]
    except BrokenPipeError:
        pass
    for raw in proc.stdout:  # type: ignore[union-attr]
        line = raw.rstrip("\n")
        if line.strip():
            mapper.out.log(RAW_PREFIX + line)
            mapper.line(line)
    code = proc.wait()
    t.join(5)
    return code, "".join(tail)


def run(out: Emitter) -> None:
    signal.signal(signal.SIGTERM, _stop_tree)
    threading.Thread(target=_watch_tree, daemon=True).start()
    req, state = Request.load(), State()
    try:
        files = stage_files(req)
    except OSError as e:
        out.failed("invalid_input_file", f"an input file could not be saved into the workspace: {e}")
        return
    prompt = req.prompt
    if files:
        prompt = (prompt + "\n\n" if prompt else "") + "Attached files (saved in the working directory): " + \
            ", ".join(files)
    if not prompt.strip():
        out.failed("invalid_prompt", "the request has no input text or files")
        return
    labels = [str(t.get("server_label")) for t in req.mcp_servers if t.get("server_label")]
    env = environment(req)
    resume = state.get("session")
    mapper = Mapper(out, state, labels)
    code, stderr = _run(command(req, resume=resume), env, prompt, mapper)
    said = stderr + mapper.api_error + json.dumps((mapper.result or {}).get("errors") or [])
    if resume and not mapper.init_seen and "No conversation found" in said:
        out.log(f"the CLI session {resume} is gone; starting a new one")
        state.set("session", "")
        mapper = Mapper(out, state, labels)
        code, stderr = _run(command(req), env, prompt, mapper)
    mapper.finish(code, stderr)


def main() -> None:
    out = Emitter()
    try:
        run(out)
    except Exception as e:
        import traceback
        traceback.print_exc()
        if not out.ended:
            out.failed("harness_error", f"the claude-code package failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
