"""Hermes Agent, one turn: the Responses request in, `hermes chat --format stream-json` run, Responses events out.

    run()      places the request's files in the workspace, writes Hermes's config.yaml for this turn (the provider,
               the model, the turn's MCP servers from $HG_MCP), starts Hermes's gateway (`hermes gateway run`: in 0.22 every
               chat runs on it) and the chat client (`hermes chat --query-file F --format stream-json`, resuming the
               session kept in $HG_STATE), feeds every client stdout line to a Mapper (each raw line is also kept in
               the turn's log, prefixed `hermes-stream `), then stops the gateway so its session store is complete
    Mapper     maps Hermes's stream-json onto hgkit.Emitter:
                 text                                    -> response.output_text.delta (token level), one assistant
                                                            message per stretch of text between tool calls
                 tool_use terminal                       -> shell_call, its tool_result -> shell_call_output
                 tool_use write_file / patch             -> apply_patch_call + apply_patch_call_output (at the result)
                 tool_use mcp__<server>__<tool>          -> mcp_call (added at the call, done at the result)
                 any other tool_use                      -> function_call, its tool_result -> function_call_output
                 result                                  -> the session id into $HG_STATE/hermes_session (--resume);
                                                            its text ends the reply; tokens -> usage; exit_code 0 ->
                                                            completed, 1 with no error (stopped at max_tool_calls) ->
                                                            incomplete, 130 -> failed (cancelled), an error -> failed
                                                            (provider_error for the provider's HTTP errors)

Runs on the sandbox's python3; hgkit is on PYTHONPATH there.
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
from pathlib import Path
from typing import Any

from hgkit import Emitter, Request, State

CLI_VERSION = "0.22.5"
WORKSPACE = os.environ.get("HG_WORKSPACE", "/workspace")
HERMES_HOME = os.path.join(os.environ.get("HOME", "/home/hg"), ".hermes")
# Where install.sh ran the official installer (its HOME): the checkout, the managed Python and the install record.
INSTALL_ROOT = os.path.join(os.environ.get("HG_TOOLS", "/opt/hg/tools"), "home", ".hermes")
PROVIDER = "harnessgateway"
RAW_PREFIX = "hermes-stream "
STOP_WAIT_S = 10
HINT = ("You are running in a HarnessGateway sandbox on behalf of an API client. The working directory {ws} is the "
        "user's workspace; it persists across the turns of this conversation. Work autonomously: nobody can answer "
        "questions or approve actions mid-turn. Your turn ends when you reply, and nothing reports back to you after "
        "that: run commands in the foreground and wait for them, unless asked to leave a service running.")
SHELL_TOOLS = ("terminal",)
PATCH_TOOLS = ("write_file", "patch")
# Request fields Hermes cannot honour, named in metadata.hg_ignored_fields when a request sets them.
IGNORED = ("tool_choice", "parallel_tool_calls", "temperature", "top_p", "top_logprobs", "max_output_tokens",
           "truncation", "include", "prompt_cache_key", "service_tier")


def _loads(text: Any) -> Any:
    try:
        return json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None


def _rel(path: str) -> str:
    """A workspace path as the client sees it (relative to the workspace); anything else stays absolute."""
    root = WORKSPACE.rstrip("/") + "/"
    return path[len(root):] if path.startswith(root) else path


def _plus(content: str) -> str:
    """A whole file's content as an apply_patch create diff: every line added."""
    return "".join(f"+{line}\n" for line in content.splitlines())


def _split_diff(diff: str) -> dict[str, str]:
    """A unified diff with `--- a/x` / `+++ b/x` headers, as {path: its hunks}."""
    out: dict[str, list[str]] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[4:].strip()
            current = current[2:] if current.startswith("b/") else current
            out.setdefault(current, [])
        elif line.startswith(("--- ", "diff --git")):
            continue
        elif current is not None:
            out[current].append(line)
    return {k: "\n".join(v) + "\n" for k, v in out.items()}


def _asked(args: dict[str, Any]) -> str:
    """What a patch call asked for, as diff text: its patch, or its old_string -> new_string replacement."""
    if args.get("patch"):
        return str(args["patch"])
    return "".join(f"-{x}\n" for x in str(args.get("old_string") or "").splitlines()) + \
        "".join(f"+{x}\n" for x in str(args.get("new_string") or "").splitlines())


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _mcp_name(server: str) -> str:
    """How Hermes turns an MCP server name into its tool names (mcp__<name>__<tool>)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", server)


class Mapper:
    """Hermes stream-json lines in, Responses events out (through the Emitter)."""

    def __init__(self, out: Emitter, state: State | None = None, mcp_labels: list[str] | None = None):
        self.out = out
        self.state = state
        self.labels = {_mcp_name(x): x for x in mcp_labels or []}
        self.calls: dict[str, dict[str, Any]] = {}     # tool_call_id -> {name, input, kind, item, index}
        self.result: dict[str, Any] | None = None

    # ── input ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def line(self, raw: str) -> None:
        s = raw.strip()
        if not s:
            return
        ev = _loads(s)
        if not isinstance(ev, dict):
            self.out.log(f"hermes: {s}")
            return
        try:
            self.handle(ev)
        except Exception as e:  # an event this mapper does not expect must not lose the turn
            self.out.log(f"map.py: could not map a {ev.get('type')} event: {type(e).__name__}: {e}")

    def handle(self, ev: dict[str, Any]) -> None:
        kind = ev.get("type")
        if kind == "text":
            self._text(str(ev.get("text") or ""))
        elif kind == "tool_use":
            self._tool_use(ev)
        elif kind == "tool_result":
            self._tool_result(ev)
        elif kind == "result":
            self.result = ev
            sid = ev.get("session_id")
            if isinstance(sid, str) and sid and self.state is not None:
                self.state.set("hermes_session", sid)

    def _text(self, delta: str) -> None:
        if self.out._msg is None:            # Hermes starts the text after a tool call with blank lines
            delta = delta.lstrip("\n")
        self.out.text(delta)

    # ── tools ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def _tool_use(self, ev: dict[str, Any]) -> None:
        name = str(ev.get("name") or "")
        given = ev.get("input")
        args: dict[str, Any] = given if isinstance(given, dict) else {}
        cid = str(ev.get("tool_call_id") or f"call_{len(self.calls) + 1}")
        call: dict[str, Any] = {"name": name, "input": args}
        self.calls[cid] = call
        server = self._mcp_server(name)
        if name in SHELL_TOOLS and isinstance(args.get("command"), str):
            call["kind"] = "shell"
            self.out.item({"type": "shell_call", "call_id": cid, "status": "completed",
                           "action": {"commands": [args["command"]]}})
        elif name in PATCH_TOOLS:
            call["kind"] = "patch"                                   # emitted with its result (it carries the diff)
        elif server is not None:
            self.out.close_text()
            item = {"id": f"mcp_{cid[-24:]}", "type": "mcp_call", "server_label": self.labels[server],
                    "name": name[len(f"mcp__{server}__"):], "arguments": json.dumps(args, ensure_ascii=False),
                    "status": "in_progress"}
            call.update(kind="mcp", item=item, index=self.out.index)
            self.out.emit({"type": "response.output_item.added", "output_index": self.out.index, "item": item})
            self.out.index += 1
        else:
            call["kind"] = "function"
            self.out.item({"type": "function_call", "call_id": cid, "name": name,
                           "arguments": json.dumps(args, ensure_ascii=False), "status": "completed"})

    def _mcp_server(self, name: str) -> str | None:
        """The sanitized server name of an MCP tool (the longest that matches), or None for Hermes's own tools."""
        return next((s for s in sorted(self.labels, key=len, reverse=True) if name.startswith(f"mcp__{s}__")), None)

    def _tool_result(self, ev: dict[str, Any]) -> None:
        cid = str(ev.get("tool_call_id") or "")
        name = str(ev.get("name") or "")
        call = self.calls.pop(cid, None) or {"name": name, "input": {}, "kind": "function"}
        text = str(ev.get("output") or "")
        data = _loads(text)
        data = data if isinstance(data, dict) else {}
        error = bool(ev.get("is_error"))
        kind, args = call["kind"], call["input"]
        if kind == "shell":
            code = data.get("exit_code")
            stdout = str(data.get("output") or "") if data else text
            self.out.item({"type": "shell_call_output", "call_id": cid, "status": "completed",
                           "output": [{"stdout": stdout, "stderr": str(data.get("error") or ""),
                                       "outcome": {"type": "exit",
                                                   "exit_code": code if isinstance(code, int) else int(error)}}]})
        elif kind == "patch":
            self._patch(cid, call["name"], args, data, text, error)
        elif kind == "mcp":
            result = data.get("result") if isinstance(data.get("result"), str) else text
            self._mcp_done(call, None if error else result, str(data.get("error") or text) if error else None)
        else:
            self.out.item({"type": "function_call_output", "call_id": cid, "output": text,
                           "status": "incomplete" if error else "completed"})

    def _patch(self, cid: str, name: str, args: dict[str, Any], data: dict[str, Any], text: str, error: bool) -> None:
        failed = error or bool(data.get("error")) or data.get("success") is False
        output = str(data.get("error") or "") if failed else ""
        ops: list[tuple[str, str, str]] = []
        if name == "write_file":
            # Hermes reports no create-or-overwrite distinction: a whole-file write is a create_file of its content
            ops = [("create_file", str(args.get("path") or ""), _plus(str(args.get("content") or "")))]
        else:
            diff = str(data.get("diff") or "")
            parts = _split_diff(diff)
            files = [("create_file", p) for p in data.get("files_created") or []] + \
                    [("update_file", p) for p in data.get("files_modified") or []] + \
                    [("delete_file", p) for p in data.get("files_deleted") or []]
            if not files and isinstance(args.get("path"), str):
                files = [("update_file", args["path"])]
            for op, path in files:
                rel = _rel(str(path))
                hunk = next((v for k, v in parts.items() if k == rel or k.endswith("/" + rel) or rel.endswith("/" + k)),
                            None)
                if hunk is None:   # no hunk for this file (a failed patch has no diff): what was asked for
                    hunk = diff or _asked(args)
                ops.append((op, str(path), hunk))
        if not ops or not all(path for _, path, _ in ops):
            self.out.function(name, args, output=text, ok=not failed, call_id=cid)
            return
        for i, (op, path, hunk) in enumerate(ops):
            self.out.patch(op, _rel(path), hunk, ok=not failed, output=output, call_id=cid if i == 0 else f"{cid}_{i}")

    def _mcp_done(self, call: dict[str, Any], output: str | None, error: str | None) -> None:
        self.out.close_text()
        item = {**call["item"], "status": "failed" if error is not None else "completed", "output": output,
                "error": {"type": "mcp_tool_execution_error", "content": [{"type": "text", "text": error}]}
                if error is not None else None}
        self.out.emit({"type": "response.output_item.done", "output_index": call["index"], "item": item})
        self.out.output.append(item)

    # ── the end ────────────────────────────────────────────────────────────────────────────────────────────────────
    def usage(self) -> dict[str, Any] | None:
        t = (self.result or {}).get("tokens")
        if not isinstance(t, dict) or not any(t.values()):
            return None
        # Hermes's input count already includes cache reads and writes, as OpenAI counts them
        return Emitter.usage(input_tokens=int(t.get("input") or 0), output_tokens=int(t.get("output") or 0),
                             cached=int(t.get("cache_read") or 0), cache_write=int(t.get("cache_write") or 0))

    def finish(self, exit_code: int | None = None, stderr: str = "") -> None:
        """The terminal event, from the client's result record (or its absence)."""
        for cid in list(self.calls):                    # calls that never got a result (the turn was stopped)
            call = self.calls.pop(cid)
            if call["kind"] == "shell":
                self.out.item({"type": "shell_call_output", "call_id": cid, "status": "incomplete",
                               "output": [{"stdout": "", "stderr": "", "outcome": {"type": "timeout"}}]})
            elif call["kind"] == "mcp":
                self._mcp_done(call, None, "the call did not finish")
        r = self.result
        if r is None:
            tail = (stderr.strip() or "no output")[-1500:]
            self.out.failed("cli_error", f"hermes exited {exit_code} without a result: {tail}")
            return
        code = r.get("exit_code")
        usage = self.usage()
        if code == 130:
            self.out.failed("cancelled", "the turn was interrupted", usage=usage)
        elif r.get("error") or code not in (0, 1):
            message = str(r.get("error") or r.get("text") or f"hermes ended with exit code {code}").strip()
            self.out.failed("provider_error" if message.startswith("HTTP ") else "cli_error", message[:4000],
                            usage=usage)
        else:
            self._reply(str(r.get("text") or ""), bool(r.get("replay_gap")))
            meta = {"hermes_session_id": str(r.get("session_id") or ""), "hermes_version": CLI_VERSION}
            if code == 0:
                self.out.completed(usage=usage, metadata=meta)
            else:   # exit 1 without an error: the turn stopped partway (the max_tool_calls budget)
                self.out._end("response.incomplete", {"incomplete_details": {"reason": "max_messages"},
                                                      "usage": usage, "metadata": meta})

    def _reply(self, text: str, gap: bool) -> None:
        """The result's text is the reply as Hermes stored it. The live stream can drop a paragraph break, stops
        early after a replay gap, and leaves out a reply written after the tool budget ran out."""
        msg = self.out._msg
        if not text.strip():
            return
        if msg is None:
            self.out.message(text)
        elif gap or _squash(msg["_text"]) == _squash(text):
            msg["_text"] = text


# ── the request -> the CLI ─────────────────────────────────────────────────────────────────────────────────────────
def ignored(req: Request) -> list[str]:
    """The request fields this turn does not act on (only those the request sets)."""
    fields = [f for f in IGNORED if req.get(f) is not None]
    if any(isinstance(t, dict) and t.get("type") != "mcp" for t in req.get("tools") or []):
        fields.append("tools")                         # Hermes runs its own tools; only MCP servers are added
    for parent, child in (("reasoning", "summary"), ("text", "verbosity")):
        if (req.get(parent) or {}).get(child) is not None:
            fields.append(f"{parent}.{child}")
    return fields


def prompt(req: Request, files: list[str]) -> str:
    notes = []
    if files:
        notes.append("Attached files (saved in the working directory): " + ", ".join(files))
    fmt = (req.get("text") or {}).get("format") or {}
    if fmt.get("type") == "json_schema":
        notes.append("Reply with only a JSON value (no prose, no code fences) that matches this JSON Schema:\n"
                     + json.dumps(fmt.get("schema") or {}, ensure_ascii=False))
    elif fmt.get("type") == "json_object":
        notes.append("Reply with only a JSON object (no prose, no code fences).")
    return "\n\n".join(x for x in [req.prompt, *notes] if x)


def mcp_config(servers: list[dict[str, Any]]) -> dict[str, Any]:
    """The turn's MCP servers ($HG_MCP, resolved by the platform) as Hermes's mcp_servers. Header values are
    placeholders the door replaces on the way out; a server whose allowed_tools is empty offers nothing and is left out."""
    out: dict[str, Any] = {}
    for s in servers:
        allowed = s.get("allowed_tools")
        if allowed == []:
            continue
        entry: dict[str, Any] = {"url": s["url"], "headers": dict(s.get("headers") or {})}
        if s.get("transport") == "sse":
            entry["transport"] = "sse"
        if allowed:
            entry["tools"] = {"include": list(allowed)}
        out[s["label"]] = entry
    return out


def config(req: Request, servers: list[dict[str, Any]]) -> dict[str, Any]:
    """config.yaml for this turn (JSON is YAML): the gateway as a custom provider, the model, nothing that asks,
    phones home or calls the model on the side."""
    api = os.environ.get("HG_PROVIDER_API", "")
    url = os.environ.get("HG_PROVIDER_URL", "")
    model = os.environ.get("HG_MODEL", "")
    agent: dict[str, Any] = {"environment_hint": HINT.format(ws=WORKSPACE), "verify_on_stop": False}
    if req.reasoning_effort:
        agent["reasoning_effort"] = req.reasoning_effort
    max_calls = req.get("max_tool_calls")
    if isinstance(max_calls, int) and max_calls > 0:
        agent["max_turns"] = max_calls
    return {
        "_config_version": 51,
        "model": {"provider": PROVIDER, "default": model},
        "providers": {PROVIDER: {"name": PROVIDER, "default_model": model, "key_env": "HG_PROVIDER_TOKEN",
                                 "base_url": url if api == "anthropic" else url.rstrip("/") + "/v1",
                                 "transport": "anthropic_messages" if api == "anthropic" else "chat_completions"}},
        "fallback_providers": [],
        "agent": agent,
        "approvals": {"mode": "off", "single_query_mode": "approve", "unattended_mode": "approve"},
        "hooks_auto_accept": True,
        "security": {"allow_lazy_installs": False},
        "auxiliary": {"title_generation": {"enabled": False}},
        "curator": {"enabled": False},
        "model_catalog": {"enabled": False},
        "lsp": {"enabled": False},
        "memory": {"nudge_interval": 0},
        "mcp_servers": mcp_config(servers),
    }


def hermes() -> str:
    """The `hermes` of the dependency environment Hermes's install record (installs/<key>/facts.json) selects.

    The checkout's own launcher binds dependency state to the data root it was installed under, which is the
    read-only tools layer here; this command runs the same pinned environment with a writable $HERMES_HOME."""
    facts = next(Path(INSTALL_ROOT, "installs").glob("*/facts.json"))
    return str(Path(json.loads(facts.read_text())["packages"]["venv"]["environment"], "bin", "hermes"))


def command(query_file: str, resume: str = "") -> list[str]:
    argv = [hermes(), "chat", "--query-file", query_file, "--format", "stream-json", "--yolo", "--accept-hooks"]
    if resume:
        argv += ["--resume", resume]
    return argv


def environment(req: Request | None = None) -> dict[str, str]:
    """The gateway's and the client's environment; the client's also carries the request's instructions, which
    Hermes applies at call time (HERMES_EPHEMERAL_SYSTEM_PROMPT, never written into the session's history)."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(HERMES_HOME=HERMES_HOME, NO_COLOR="1")
    if req is not None and req.instructions.strip():
        env["HERMES_EPHEMERAL_SYSTEM_PROMPT"] = req.instructions.strip()
    return env


# ── running the CLI ────────────────────────────────────────────────────────────────────────────────────────────────
_procs: list[subprocess.Popen[str]] = []        # the gateway, then the client: each in a process group of its own


def _stop(proc: subprocess.Popen[str], wait: float) -> None:
    """SIGTERM a process group; SIGKILL what is left of it after `wait` seconds."""
    for sig, grace in ((signal.SIGTERM, wait), (signal.SIGKILL, 2)):
        if proc.poll() is not None:
            break
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, sig)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(grace)
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)            # tool processes the group still holds


def _on_sigterm(signum: int, _frame: Any) -> None:
    """Cancel or timeout: stopping the gateway interrupts the running turn and saves the session."""
    for proc in _procs:
        _stop(proc, STOP_WAIT_S)
    os._exit(128 + signum)


def _start(argv: list[str], env: dict[str, str], stdout: Any) -> subprocess.Popen[str]:
    proc = subprocess.Popen(argv, cwd=WORKSPACE, env=env, stdin=subprocess.DEVNULL, stdout=stdout,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
                            start_new_session=True)
    _procs.append(proc)
    return proc


def _drain(stream: Any, tail: list[str]) -> threading.Thread:
    def copy() -> None:
        for chunk in stream:
            sys.stderr.write(chunk)
            sys.stderr.flush()
            tail.append(chunk)
            del tail[:-50]
    t = threading.Thread(target=copy, daemon=True)
    t.start()
    return t


def _chat(argv: list[str], env: dict[str, str], mapper: Mapper) -> tuple[int, str]:
    proc = _start(argv, env, subprocess.PIPE)
    tail: list[str] = []
    t = _drain(proc.stderr, tail)
    for raw in proc.stdout:  # type: ignore[union-attr]
        line = raw.rstrip("\n")
        if line.strip():
            mapper.out.log(RAW_PREFIX + line)
            mapper.line(line)
    code = proc.wait()
    t.join(5)
    _procs.remove(proc)
    return code, "".join(tail)


def run(out: Emitter) -> None:
    signal.signal(signal.SIGTERM, _on_sigterm)
    req, state = Request.load(), State()
    out.ignored(ignored(req))
    if os.environ.get("HG_PROVIDER_API") not in ("anthropic", "openai") or not os.environ.get("HG_MODEL"):
        out.failed("no_provider", "Hermes needs a provider (anthropic or openai shape) and a model")
        return
    try:
        files = req.place_inputs()
    except OSError as e:
        out.failed("invalid_input_file", f"an input file could not be saved into the workspace: {e}")
        return
    text = prompt(req, files)
    if not text.strip():
        out.failed("invalid_prompt", "the request has no input text or files")
        return
    os.makedirs(HERMES_HOME, exist_ok=True)
    servers = Request.mcp_servers()
    Path(HERMES_HOME, "config.yaml").write_text(json.dumps(config(req, servers), indent=1))
    tmp = Path(os.environ.get("TMPDIR", "/tmp"), "hg-hermes")
    tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
    query = tmp / "prompt.md"
    query.write_text(text)
    gateway = _start([hermes(), "gateway", "run"], environment(), subprocess.DEVNULL)
    _drain(gateway.stderr, [])
    labels = [str(s["label"]) for s in servers]
    env = environment(req)
    resume = state.get("hermes_session")
    mapper = Mapper(out, state, labels)
    code, stderr = _chat(command(str(query), resume), env, mapper)
    if resume and mapper.result is not None and "No session found" in str(mapper.result.get("error")):
        out.log(f"the Hermes session {resume} is gone; starting a new one")
        state.set("hermes_session", "")
        mapper = Mapper(out, state, labels)
        code, stderr = _chat(command(str(query)), env, mapper)
    mapper.finish(code, stderr)
    _stop(gateway, STOP_WAIT_S)                    # it writes the session store out as it stops


def main() -> None:
    out = Emitter()
    try:
        run(out)
    except Exception as e:
        import traceback
        traceback.print_exc()
        if not out.ended:
            out.failed("harness_error", f"the hermes package failed: {type(e).__name__}: {e}")
    finally:
        for proc in list(_procs):
            _stop(proc, STOP_WAIT_S)


if __name__ == "__main__":
    main()
