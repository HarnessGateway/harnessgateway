"""Gemini CLI, one turn: the Responses request in, `gemini -o stream-json` run, Responses stream events out.

    run()      places the request's files in the workspace, writes the turn's ~/.gemini/settings.json (with the turn's
               MCP servers from $HG_MCP) and instruction file, builds the command line and environment, runs the CLI
               in the workspace (prompt on stdin) and feeds every stdout line to a Mapper (each raw line is also kept
               in the turn's log, prefixed `gemini-stream `)
    Mapper     maps Gemini CLI's stream-json onto hgkit.Emitter:
                 init                                     -> the CLI's session id into $HG_STATE/gemini_session (-r)
                 message (assistant, delta)               -> response.output_text.delta
                 tool_use run_shell_command               -> shell_call, its tool_result -> shell_call_output
                 tool_use write_file / replace            -> apply_patch_call + apply_patch_call_output (at the result)
                 tool_use mcp_<server>_<tool>             -> mcp_call (added at the call, done at the result)
                 any other tool_use                       -> function_call, its tool_result -> function_call_output
                 error (severity error)                   -> kept for the failure message
                 result                                   -> usage from its stats; success -> completed, the turn
                                                             limit -> incomplete, anything else -> failed
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

from hgkit import Emitter, Request, State

WORKSPACE = os.environ.get("HG_WORKSPACE", "/workspace")
RAW_PREFIX = "gemini-stream "
INSTRUCTIONS_FILE = "HG_INSTRUCTIONS.md"     # a user-level context file: the request's instructions, rewritten per turn
API_ERROR = "[API Error"
RESUME_FAILED = "Error resuming session"     # the CLI's stderr when -r names no resumable session
# The CLI's helper calls (routing, edit correction, summaries, loop and next-speaker checks, compression, web tools)
# run on model-config aliases that name models of their own (DEFAULT_MODEL_CONFIGS in 0.65.0). Each is redefined with
# the turn's model, keeping its parent alias, so no call asks the provider for a model the request did not name.
HELPER_ALIASES = {
    "gemini-2.5-flash-base": "base", "gemini-3-flash-base": "base", "gemini-3.5-flash-base": "base",
    "classifier": "base", "prompt-completion": "base", "fast-ack-helper": "base", "edit-corrector": "base",
    "summarizer-default": "base", "summarizer-shell": "base", "loop-detection-double-check": "base",
    "chat-compression-3-pro": "", "chat-compression-3-flash": "", "chat-compression-3.1-flash-lite": "",
    "chat-compression-2.5-pro": "", "chat-compression-2.5-flash": "", "chat-compression-2.5-flash-lite": "",
    "chat-compression-default": "", "agent-history-provider-summarizer": "",
}
# Responses reasoning.effort -> the Gemini API's thinkingConfig: Gemini 2.5 takes a token budget, later models a level.
BUDGETS = {"none": 0, "minimal": 512, "low": 2048, "medium": 8192, "high": 24576, "xhigh": 32768}
LEVELS = {"none": "MINIMAL", "minimal": "MINIMAL", "low": "LOW", "medium": "MEDIUM", "high": "HIGH", "xhigh": "HIGH"}
# Request fields the CLI cannot honour, named in metadata.hg_ignored_fields when a request sets them.
IGNORED = ("tool_choice", "parallel_tool_calls", "temperature", "top_p", "top_logprobs", "truncation", "include",
           "prompt_cache_key", "service_tier")
SECRET_ENV = ("GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION",
              "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA", "CLOUD_SHELL")


def _rel(path: str) -> str:
    """A workspace path as the client sees it (relative to the workspace); anything else stays absolute."""
    root = WORKSPACE.rstrip("/") + "/"
    return path[len(root):] if path.startswith(root) else path


def _lines(prefix: str, text: str) -> str:
    body = text[:-1] if text.endswith("\n") else text
    return "\n".join(prefix + line for line in body.split("\n")) if text else ""


def api_error(message: str) -> str:
    """The CLI's `[API Error: <json>]` with the provider's own message unwrapped (the gateway's error body can carry
    Google's as its message): `[API Error: 400 API key not valid. ...]`."""
    if not (message.startswith(API_ERROR + ": ") and message.endswith("]")):
        return message
    code, text = None, message[len(API_ERROR) + 2:-1]
    for _ in range(3):
        try:
            err = json.loads(text)["error"]
            code, text = err.get("code", code), str(err["message"])
        except (ValueError, TypeError, KeyError):
            break
    return f"{API_ERROR}: {code} {text.strip()}]" if code is not None else message


def mcp_name(label: str) -> str:
    """A request's server_label as a Gemini MCP server name: the CLI names its tools mcp_<server>_<tool> and splits
    them at the first underscore, so a server name keeps letters, digits and dashes only."""
    return re.sub(r"[^A-Za-z0-9-]", "-", label)


class Mapper:
    """Gemini CLI stream-json lines in, Responses events out (through the Emitter)."""

    def __init__(self, out: Emitter, state: State | None = None, mcp_labels: list[str] | None = None):
        self.out = out
        self.state = state
        self.labels = {mcp_name(x): x for x in mcp_labels or []}
        self.calls: dict[str, dict[str, Any]] = {}    # tool_id -> {name, args, kind, item, index}
        self.session_id = ""
        self.model = ""
        self.result: dict[str, Any] | None = None
        self.errors: list[str] = []                    # error events of severity "error"

    # ── input ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def line(self, raw: str) -> None:
        s = raw.strip()
        if not s:
            return
        try:
            ev = json.loads(s)
        except ValueError:
            self.out.log(f"gemini: {s}")
            return
        if isinstance(ev, dict):
            try:
                self.handle(ev)
            except Exception as e:  # a CLI event this mapper does not expect must not lose the turn
                self.out.log(f"map.py: could not map a {ev.get('type')} event: {type(e).__name__}: {e}")

    def handle(self, ev: dict[str, Any]) -> None:
        kind = ev.get("type")
        if kind == "init":
            self.model = str(ev.get("model") or "")
            sid = ev.get("session_id")
            if isinstance(sid, str) and sid:
                self.session_id = sid
                if self.state is not None:
                    self.state.set("gemini_session", sid)
        elif kind == "message" and ev.get("role") == "assistant":
            self.out.text(str(ev.get("content") or ""))
        elif kind == "tool_use":
            self._tool_use(ev)
        elif kind == "tool_result":
            self._tool_result(ev)
        elif kind == "error":
            message = str(ev.get("message") or "")
            if ev.get("severity") == "error":
                self.errors.append(message)
            else:
                self.out.log(f"gemini {ev.get('severity')}: {message}")
        elif kind == "result":
            self.result = ev

    # ── tools ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def _mcp(self, name: str) -> tuple[str, str] | None:
        """(server label, tool) of an mcp_<server>_<tool> name; the request's labels are matched first."""
        if not name.startswith("mcp_"):
            return None
        rest = name[4:]
        for server in sorted(self.labels, key=len, reverse=True):
            if rest.startswith(server + "_"):
                return self.labels[server], rest[len(server) + 1:]
        server, _, tool = rest.partition("_")
        return (server, tool) if tool else None

    def _tool_use(self, ev: dict[str, Any]) -> None:
        name = str(ev.get("tool_name") or "")
        args = ev.get("parameters") if isinstance(ev.get("parameters"), dict) else {}
        cid = str(ev.get("tool_id") or f"call_{len(self.calls) + 1}")
        call: dict[str, Any] = {"name": name, "args": args}
        self.calls[cid] = call
        mcp = self._mcp(name)
        if name == "run_shell_command":
            call["kind"] = "shell"
            self.out.item({"type": "shell_call", "call_id": cid, "status": "completed",
                           "action": {"commands": [str(args.get("command", ""))]}})
        elif name in ("write_file", "replace"):
            call["kind"] = "patch"                                   # emitted with its result
        elif mcp:
            self.out.close_text()
            item = {"id": f"mcp_{cid[-24:]}", "type": "mcp_call", "server_label": mcp[0], "name": mcp[1],
                    "arguments": json.dumps(args, ensure_ascii=False), "status": "in_progress"}
            call.update(kind="mcp", item=item, index=self.out.index)
            self.out.emit({"type": "response.output_item.added", "output_index": self.out.index, "item": item})
            self.out.index += 1
        else:
            call["kind"] = "function"
            self.out.item({"type": "function_call", "call_id": cid, "name": name,
                           "arguments": json.dumps(args, ensure_ascii=False), "status": "completed"})

    def _tool_result(self, ev: dict[str, Any]) -> None:
        """A tool's result carries the CLI's display text when it is text (a shell's output; not a file diff) and, on
        failure, the error message. The shell's exit code is not in the stream: a failed run reports 1."""
        cid = str(ev.get("tool_id") or "")
        error = ev.get("status") == "error"
        message = str((ev.get("error") or {}).get("message") or "") if error else ""
        text = str(ev.get("output") or "")
        call = self.calls.pop(cid, {"name": "", "args": {}, "kind": "function"})
        kind, args = call["kind"], call["args"]
        if kind == "shell":
            self.out.item({"type": "shell_call_output", "call_id": cid, "status": "completed",
                           "output": [{"stdout": text, "stderr": message,
                                       "outcome": {"type": "exit", "exit_code": 1 if error else 0}}]})
        elif kind == "patch":
            path = _rel(str(args.get("file_path") or ""))
            if call["name"] == "write_file":
                op = {"type": "create_file", "path": path, "diff": _lines("+", str(args.get("content") or ""))}
            else:
                diff = "@@\n" + _lines("-", str(args.get("old_string") or "")) + "\n" + \
                    _lines("+", str(args.get("new_string") or ""))
                op = {"type": "update_file", "path": path, "diff": diff}
            self.out.item({"type": "apply_patch_call", "call_id": cid, "status": "completed", "operation": op})
            self.out.item({"type": "apply_patch_call_output", "call_id": cid,
                           "status": "failed" if error else "completed", "output": message or text})
        elif kind == "mcp":
            self._mcp_done(call, "failed" if error else "completed", message or text)
        else:
            self.out.item({"type": "function_call_output", "call_id": cid, "output": message or text,
                           "status": "incomplete" if error else "completed"})

    def _mcp_done(self, call: dict[str, Any], status: str, text: str = "") -> None:
        self.out.close_text()
        item = {**call["item"], "status": status}
        if status == "completed":
            item["output"] = text
        elif status == "failed":
            item["error"] = {"type": "mcp_tool_execution_error", "content": [{"type": "text", "text": text}]}
        self.out.emit({"type": "response.output_item.done", "output_index": call["index"], "item": item})
        self.out.output.append(item)

    # ── the end ────────────────────────────────────────────────────────────────────────────────────────────────────
    def response_usage(self) -> dict[str, Any] | None:
        """The run's stats: input_tokens include the cached ones; thinking tokens are what total_tokens holds beyond
        input and output, and count as output (as OpenAI counts reasoning)."""
        s = (self.result or {}).get("stats")
        if not isinstance(s, dict) or not s.get("total_tokens"):
            return None
        inp, out, total = int(s.get("input_tokens") or 0), int(s.get("output_tokens") or 0), int(s["total_tokens"])
        thinking = max(total - inp - out, 0)
        return Emitter.usage(input_tokens=inp, output_tokens=out + thinking, cached=int(s.get("cached") or 0),
                             reasoning=thinking)

    def _end(self, kind: str, **response: Any) -> None:
        """The terminal event (the final output is the items in the order they finished), with the turn's usage."""
        self.out._end(kind, {"usage": self.response_usage(), **response})

    def finish(self, exit_code: int | None = None, stderr: str = "") -> None:
        for call in [self.calls.pop(cid) for cid in list(self.calls)]:    # calls that never got a result
            if call["kind"] == "mcp":
                self._mcp_done(call, "incomplete")
        r = self.result
        meta = {"gemini_session": self.session_id, "gemini_model": self.model}
        if r is None:
            tail = "; ".join(self.errors) or stderr.strip()[-1500:] or "no output"
            self._end("response.failed", error={"code": "cli_error",
                                                "message": f"gemini exited {exit_code} without a result: {tail}"[:4000]})
            return
        if r.get("status") == "success":
            self._end("response.completed", metadata=meta)
            return
        err = r.get("error") if isinstance(r.get("error"), dict) else {}
        message = str(err.get("message") or "; ".join(self.errors) or f"gemini ended with {r.get('status')}").strip()
        if err.get("type") == "FatalTurnLimitedError":     # the request's max_tool_calls ran out: the work so far
            self._end("response.incomplete", incomplete_details={"reason": "max_messages"},
                      metadata={**meta, "gemini_stop": message[:500]})
            return
        message = api_error(message)
        code = "provider_error" if message.startswith(API_ERROR) else str(err.get("type") or "cli_error")
        if re.search(r"\b429\b|RESOURCE_EXHAUSTED", message):
            code = "rate_limit_exceeded"
        self._end("response.failed", error={"code": code, "message": message[:4000]})


# ── running the CLI ────────────────────────────────────────────────────────────────────────────────────────────────
def ignored(req: Request) -> list[str]:
    """The request fields this turn does not act on (only those the request sets)."""
    fields = [f for f in IGNORED if req.get(f) is not None]
    if any(isinstance(t, dict) and t.get("type") != "mcp" for t in req.get("tools") or []):
        fields.append("tools")                         # the CLI runs its own tools; only MCP servers are added
    for parent, child in (("reasoning", "summary"), ("text", "verbosity")):
        if (req.get(parent) or {}).get(child) is not None:
            fields.append(f"{parent}.{child}")
    return fields


def prompt_for(req: Request, files: list[str]) -> str:
    """The prompt, with attached files as `@name` references: the CLI inlines each file's content (images as images)."""
    refs = " ".join("@" + n.replace("\\", "\\\\").replace(" ", "\\ ") for n in files)
    return "\n\n".join(x for x in (req.prompt, f"Attached files (in the working directory): {refs}" if refs else "") if x)


def instructions_for(req: Request) -> str:
    """The request's instructions, plus its text.format (the CLI has no structured-output option)."""
    parts = [req.instructions.strip()]
    fmt = (req.get("text") or {}).get("format") or {}
    if fmt.get("type") == "json_schema":
        parts.append("Your final reply must be only a JSON value (no prose, no code fences) that validates against "
                     f"this JSON schema named {fmt.get('name', 'output')}:\n"
                     + json.dumps(fmt.get("schema") or {}, ensure_ascii=False))
    elif fmt.get("type") == "json_object":
        parts.append("Your final reply must be only a JSON object (no prose, no code fences).")
    return "\n\n".join(p for p in parts if p)


def mcp_settings(servers: list[dict[str, Any]]) -> dict[str, Any]:
    """The turn's MCP servers ($HG_MCP, resolved by the platform) as the CLI's mcpServers. Header values are
    placeholders the door replaces on the way out; a server whose allowed_tools is empty offers nothing and is left out."""
    out: dict[str, Any] = {}
    for s in servers:
        allowed = s.get("allowed_tools")
        if allowed == []:
            continue
        entry: dict[str, Any] = {"url" if s.get("transport") == "sse" else "httpUrl": s["url"], "trust": True}
        if s.get("headers"):
            entry["headers"] = dict(s["headers"])
        if allowed:
            entry["includeTools"] = list(allowed)
        out[mcp_name(s["label"])] = entry
    return out


def gemini_settings(req: Request, model: str, servers: list[dict[str, Any]], seed: dict[str, Any]) -> dict[str, Any]:
    """The package's seeded settings (if any) with what this turn needs on top."""
    doc: dict[str, Any] = json.loads(json.dumps(seed))
    doc.setdefault("security", {}).setdefault("auth", {})["selectedType"] = "gemini-api-key"
    doc.setdefault("general", {}).update(enableAutoUpdate=False, enableAutoUpdateNotification=False)
    doc.setdefault("privacy", {})["usageStatisticsEnabled"] = False
    doc.setdefault("telemetry", {})["enabled"] = False
    doc.setdefault("context", {})["fileName"] = ["GEMINI.md", "AGENTS.md", INSTRUCTIONS_FILE]
    configs = doc.setdefault("modelConfigs", {})
    configs.setdefault("customAliases", {}).update(
        {name: ({"extends": parent} if parent else {}) | {"modelConfig": {"model": model}}
         for name, parent in HELPER_ALIASES.items()})
    generate: dict[str, Any] = {}
    effort = req.reasoning_effort
    if effort in BUDGETS:
        generate["thinkingConfig"] = {"thinkingBudget": BUDGETS[effort]} if model.startswith("gemini-2.") \
            else {"thinkingLevel": LEVELS[effort]}
    mot = req.get("max_output_tokens")
    if isinstance(mot, int) and mot > 0:
        generate["maxOutputTokens"] = mot
    if generate:
        configs.setdefault("customOverrides", []).append({"match": {"model": model},
                                                          "modelConfig": {"generateContentConfig": generate}})
    max_calls = req.get("max_tool_calls")
    if isinstance(max_calls, int) and max_calls > 0:
        doc.setdefault("model", {})["maxSessionTurns"] = max_calls
    mcp = mcp_settings(servers)
    if mcp:
        doc["mcpServers"] = {**doc.get("mcpServers", {}), **mcp}
    return doc


def environment() -> dict[str, str]:
    """The CLI's environment: the Gemini API at the platform's gateway, the per-turn token as its key."""
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env.update(GOOGLE_GEMINI_BASE_URL=env.get("HG_PROVIDER_URL", "").rstrip("/"),
               GEMINI_API_KEY=env.get("HG_PROVIDER_TOKEN", ""), NO_COLOR="1")
    return env


def command(resume: str = "") -> list[str]:
    argv = ["gemini", "-o", "stream-json", "-m", os.environ.get("HG_MODEL", ""), "--approval-mode", "yolo",
            "--skip-trust"]
    return argv + (["-r", resume] if resume else [])


def saved_session(state: State, home: Path) -> str:
    """The CLI session to resume: the id kept in $HG_STATE, if its transcript is still under the persisted
    ~/.gemini/tmp (the CLI names it session-<time>-<first 8 characters of the id>.jsonl)."""
    sid = state.get("gemini_session")
    return sid if sid and any((home / ".gemini" / "tmp").glob(f"*/chats/session-*-{sid[:8]}.json*")) else ""


def _descendants(root: int) -> list[int]:
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


_seen: set[int] = set()        # processes seen below this one while the turn ran: an orphaned tool is still stopped


def _alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def _watch_tree() -> None:
    while True:
        _seen.intersection_update([p for p in _seen if _alive(p)])
        _seen.update(_descendants(os.getpid()))
        time.sleep(0.5)


def _stop(*_: Any) -> None:
    """Cancel/timeout: the CLI exits on SIGTERM but leaves its shell commands running (each in a session of its own,
    orphaned once the CLI is gone), so every process seen below this one is signalled, then killed."""
    pids = sorted(set(_descendants(os.getpid())) | _seen)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, sig)
        deadline = time.monotonic() + 2
        while sig == signal.SIGTERM and time.monotonic() < deadline and any(map(_alive, pids)):
            time.sleep(0.1)
        pids = [p for p in pids if _alive(p)]
    os._exit(143)


def run(out: Emitter) -> None:
    req, state = Request.load(), State()
    out.ignored(ignored(req))
    try:
        files = req.place_inputs()
    except OSError as e:
        out.failed("invalid_input_file", f"an input file could not be saved into the workspace: {e}")
        return
    prompt = prompt_for(req, files)
    if not prompt.strip():
        out.failed("invalid_prompt", "the request has no input text or files")
        return
    if not os.environ.get("HG_PROVIDER_URL"):
        out.failed("harness_error", "Gemini CLI needs a provider: call it as gemini-cli/<provider>/<model>")
        return
    home = Path(os.environ.get("HOME", "/home/hg"))
    gdir = home / ".gemini"
    gdir.mkdir(parents=True, exist_ok=True)
    seed_file = Path(os.environ.get("HG_PACKAGE_DIR", "/opt/hg/package"), "seeds/home/.gemini/settings.json")
    seed = json.loads(seed_file.read_text()) if seed_file.is_file() else {}
    env = environment()
    servers = Request.mcp_servers()
    settings_file = gdir / "settings.json"
    settings_file.write_text(json.dumps(gemini_settings(req, env.get("HG_MODEL", ""), servers, seed), indent=2))
    settings_file.chmod(0o600)
    instructions = instructions_for(req)
    if instructions:
        (gdir / INSTRUCTIONS_FILE).write_text(instructions + "\n")
    else:
        (gdir / INSTRUCTIONS_FILE).unlink(missing_ok=True)
    resume = saved_session(state, home)
    out.log(f"gemini-cli: model={env.get('HG_MODEL', '')} resume={resume or '-'}")
    signal.signal(signal.SIGTERM, _stop)
    threading.Thread(target=_watch_tree, daemon=True).start()
    labels = [str(s["label"]) for s in servers]
    mapper = Mapper(out, state, labels)
    code, stderr = _gemini(command(resume), env, prompt, mapper)
    if resume and mapper.result is None and RESUME_FAILED in stderr:
        # a session whose turns never got a model reply has nothing the CLI can resume
        out.log(f"gemini-cli: session {resume} cannot be resumed; starting a new one")
        state.set("gemini_session", "")
        mapper = Mapper(out, state, labels)
        code, stderr = _gemini(command(), env, prompt, mapper)
    mapper.finish(code, stderr)


def _gemini(argv: list[str], env: dict[str, str], prompt: str, mapper: Mapper) -> tuple[int, str]:
    """One run of the CLI (prompt on stdin), its stdout fed to the mapper; its exit code and the tail of its stderr."""
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
    with contextlib.suppress(BrokenPipeError):
        proc.stdin.write(prompt)  # type: ignore[union-attr]
        proc.stdin.close()  # type: ignore[union-attr]
    for raw in proc.stdout:  # type: ignore[union-attr]
        line = raw.rstrip("\n")
        if line.strip():
            mapper.out.log(RAW_PREFIX + line)
            mapper.line(line)
    code = proc.wait()
    t.join(5)
    return code, "".join(tail)

def main() -> None:
    out = Emitter()
    try:
        run(out)
    except Exception as e:
        import traceback
        traceback.print_exc()
        if not out.ended:
            out.failed("harness_error", f"the gemini-cli package failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
