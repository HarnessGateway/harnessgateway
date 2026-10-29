"""Codex, one turn: the Responses request in, `codex exec --json` run, Responses events out.

    run()      places the request's files in the workspace, builds Codex's configuration for the turn as `-c`
               overrides (the provider at $HG_PROVIDER_URL/v1 with the per-turn token, the model, reasoning,
               instructions, the turn's MCP servers from $HG_MCP), runs `codex exec --json` (`codex exec resume
               <thread>` on a continuation) in the workspace and feeds every stdout line to a Mapper (each raw line is
               also kept in the turn's log, prefixed `codex> `). $HOME/.codex is left to the organization's seeds
    Mapper     maps Codex's exec JSONL onto hgkit.Emitter:
                 thread.started                         -> the thread id into $HG_STATE/codex_thread (exec resume)
                 item.started command_execution         -> a shell_call added in_progress (a long command shows
                                                           while it runs)
                 item.updated/completed agent_message   -> output_text deltas (only the new suffix each time), then
                                                           the message
                 item.completed reasoning               -> a reasoning item (the summary text)
                 item.completed command_execution       -> the shell_call done + shell_call_output (aggregated
                                                           output, exit code)
                 item.completed file_change             -> apply_patch_call + apply_patch_call_output per path
                 item.completed mcp_tool_call           -> mcp_call (arguments, result content or error)
                 item.completed web_search              -> web_search_call
                 item.completed todo_list / other       -> function_call (+ function_call_output)
                 item.completed error                   -> a log line (Codex's non-fatal warnings)
                 turn.completed                         -> usage on response.completed (this turn's share of the
                                                           thread's cumulative usage)
                 turn.failed / error                    -> response.failed with Codex's message (a top-level `error`
                                                           alone may be a retry notice, so it fails the turn only
                                                           if no turn.completed follows)

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

WORKSPACE = os.environ.get("HG_WORKSPACE", "/workspace")
RAW_PREFIX = "codex> "
TMP = Path("/tmp/hg-codex")
THREAD_KEY = "codex_thread"
USAGE_KEY = "codex_usage"                # the thread's cumulative usage after the last turn: {"thread", "usage"}
INSTRUCTIONS_KEY = "codex_instructions"  # the instructions the thread last received
USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
                "reasoning_output_tokens")
PATCH_OPS = {"add": "create_file", "create": "create_file", "update": "update_file", "modify": "update_file",
             "delete": "delete_file", "remove": "delete_file"}
CREATE_DIFF_MAX = 256 * 1024
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
IGNORED = ("tool_choice", "parallel_tool_calls", "max_tool_calls", "max_output_tokens", "temperature", "top_p",
           "top_logprobs", "truncation", "service_tier", "prompt", "prompt_cache_key", "include")
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
SUMMARIES = ("auto", "concise", "detailed", "none")
VERBOSITY = ("low", "medium", "high")
STDERR_NOISE = ("WARNING: proceeding", "Reading additional input")   # Codex's own chatter, not a failure reason


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _error_code(message: str) -> str:
    m = message.lower()
    if "429" in m or "rate limit" in m or "too many requests" in m or "quota" in m:
        return "rate_limit_exceeded"
    if "401" in m or "403" in m or "unauthorized" in m or "api key" in m or "forbidden" in m:
        return "provider_auth_error"
    if "model" in m and ("not found" in m or "does not exist" in m or "not supported" in m or "404" in m
                         or "invalid" in m):
        return "model_not_found"
    return "provider_error"


def toml_str(value: str) -> str:
    """A TOML basic string. JSON escapes are TOML escapes, except astral characters (kept raw here) and DEL."""
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def mcp_key(label: str) -> str:
    """A server label as a Codex server name (a `-c` path segment, so no dots): [A-Za-z0-9_-]."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", label)


class Mapper:
    """Codex `exec --json` lines in, Responses events out (through the Emitter)."""

    def __init__(self, out: Emitter, state: State, workspace: str = WORKSPACE, servers: dict[str, str] | None = None):
        self.out = out
        self.servers = servers or {}                     # Codex server name -> the turn's MCP server label
        self.state = state
        self.workspace = workspace.rstrip("/")
        self.thread_id = ""
        self.usage: dict[str, int] | None = None        # Codex's cumulative thread usage, as last reported
        self.baseline: dict[str, int] = {}               # the thread's cumulative usage before this turn
        self.turn_completed = False
        self.turn_failed: str | None = None
        self.last_error = ""
        self.text_seen: dict[str, str] = {}              # agent_message id -> text already streamed
        self.text_item: str | None = None                # the agent_message currently open in the Emitter
        self.open_shell: dict[str, dict[str, Any]] = {}  # command_execution id -> the shell_call added in_progress

    # ── input ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def line(self, raw: str) -> None:
        s = raw.strip()
        if not s:
            return
        try:
            event = json.loads(s)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            self.out.log(f"codex: {s}")
            return
        kind = event.get("type")
        item = event.get("item") if isinstance(event.get("item"), dict) else None
        if kind == "thread.started":
            self._thread(str(event.get("thread_id") or ""))
        elif kind == "item.started" and item:
            self._started(item)
        elif kind == "item.updated" and item:
            if item.get("type") == "agent_message":
                self._agent_text(item, final=False)
        elif kind == "item.completed" and item:
            self._completed(item)
        elif kind == "turn.completed":
            self.turn_completed = True
            self._add_usage(event.get("usage") or {})
        elif kind == "turn.failed":
            err = event.get("error") if isinstance(event.get("error"), dict) else {}
            self.turn_failed = str(err.get("message") or event.get("message") or "the Codex turn failed")
            self._add_usage(event.get("usage") or {})
        elif kind == "error":
            self.last_error = str(event.get("message") or "")
            self.out.log(f"codex error: {self.last_error}")

    def _thread(self, thread_id: str) -> None:
        self.thread_id = thread_id
        if not thread_id:
            return
        self.state.set(THREAD_KEY, thread_id)
        try:
            prev = json.loads(self.state.get(USAGE_KEY) or "{}")
        except ValueError:
            prev = {}
        if isinstance(prev, dict) and prev.get("thread") == thread_id and isinstance(prev.get("usage"), dict):
            self.baseline = {k: _int(prev["usage"].get(k)) for k in USAGE_FIELDS}

    def _add_usage(self, u: dict[str, Any]) -> None:
        """turn.completed carries the THREAD's cumulative usage (`codex exec resume` restores the earlier turns'
        totals), so this turn's usage is the difference from what was recorded after the previous turn."""
        if not u:
            return
        self.usage = {k: _int(u.get(k)) for k in USAGE_FIELDS}
        if self.thread_id:
            self.state.set(USAGE_KEY, json.dumps({"thread": self.thread_id, "usage": self.usage}))

    # ── items ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def _started(self, item: dict[str, Any]) -> None:
        if item.get("type") == "command_execution":
            iid = str(item.get("id") or "")
            if not iid or iid in self.open_shell:
                return
            self._close_text()
            out = self.out
            call = {"id": f"sh_{out.index}_{iid}"[:64], "type": "shell_call", "call_id": f"call_{iid}"[:64],
                    "status": "in_progress", "action": {"commands": [str(item.get("command") or "")]}}
            out.emit({"type": "response.output_item.added", "output_index": out.index, "item": call})
            self.open_shell[iid] = {"item": call, "index": out.index}
            out.index += 1
        elif item.get("type") == "agent_message":
            self._agent_text(item, final=False)

    def _completed(self, item: dict[str, Any]) -> None:
        kind = item.get("type")
        if kind == "agent_message":
            self._agent_text(item, final=True)
            return
        if kind == "error":
            self.out.log(f"codex warning: {item.get('message', '')}")
            return
        self._close_text()
        if kind == "reasoning":
            self.out.reasoning(str(item.get("text") or ""))
        elif kind == "command_execution":
            self._command(item)
        elif kind == "file_change":
            self._file_change(item)
        elif kind == "mcp_tool_call":
            self._mcp(item)
        elif kind == "web_search":
            self._web_search(item)
        else:
            self._other(item)

    def _agent_text(self, item: dict[str, Any], final: bool) -> None:
        iid = str(item.get("id") or "")
        text = str(item.get("text") or "")
        if self.text_item is not None and self.text_item != iid:
            self._close_text()
        seen = self.text_seen.get(iid, "")
        delta = text[len(seen):] if text.startswith(seen) else ""   # rewritten text: nothing new to stream
        if delta:
            self.out.text(delta)
            self.text_item = iid
            self.text_seen[iid] = seen + delta
        if final:
            self._close_text()

    def _close_text(self) -> None:
        self.out.close_text()
        self.text_item = None

    def _command(self, item: dict[str, Any]) -> None:
        iid = str(item.get("id") or "")
        opened = self.open_shell.pop(iid, None)
        call_status = "completed" if item.get("status") in ("completed", "failed") else "incomplete"
        command = str(item.get("command") or "")
        if opened:
            call = {**opened["item"], "status": call_status, "action": {"commands": [command]}}
            self.out.emit({"type": "response.output_item.done", "output_index": opened["index"], "item": call})
            self.out.output.append(call)
        else:
            call = self.out.item({"type": "shell_call", "call_id": f"call_{iid or self.out.index}"[:64],
                                  "status": call_status, "action": {"commands": [command]}})
        code = item.get("exit_code")
        outcome = {"type": "exit", "exit_code": code} if isinstance(code, int) else {"type": "timeout"}
        self.out.item({"type": "shell_call_output", "call_id": call["call_id"], "status": "completed",
                       "output": [{"stdout": str(item.get("aggregated_output") or ""), "stderr": "",
                                   "outcome": outcome}]})

    def _file_change(self, item: dict[str, Any]) -> None:
        ok = item.get("status") != "failed"
        for change in item.get("changes") or []:
            if not isinstance(change, dict):
                continue
            kind = change.get("kind")
            if isinstance(kind, dict):              # tolerate {"type": "add"} shapes
                kind = kind.get("type")
            op = PATCH_OPS.get(str(kind or "update").lower(), "update_file")
            path = str(change.get("path") or "")
            diff = str(change.get("diff") or change.get("unified_diff") or "")
            if op == "create_file" and not diff:
                diff = self._create_diff(path)
            rel = path[len(self.workspace) + 1:] if path.startswith(self.workspace + "/") else path
            self.out.patch(op, rel, diff, ok=ok,
                           output=f"{'applied' if ok else 'failed'}: {op.replace('_file', '')} {rel}")

    @staticmethod
    def _create_diff(path: str) -> str:
        """A created file's content as an apply_patch create diff (each line prefixed with '+'), when readable."""
        try:
            p = Path(path)
            if not p.is_file() or p.stat().st_size > CREATE_DIFF_MAX:
                return ""
            text = p.read_text(errors="replace")
        except OSError:
            return ""
        return "".join(f"+{line}\n" for line in text.splitlines())

    def _mcp(self, item: dict[str, Any]) -> None:
        error = item.get("error")
        message = (error.get("message") if isinstance(error, dict) else str(error)) if error else None
        result = item.get("result")
        output = None
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list):
                texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                output = "\n".join(texts) if texts and len(texts) == len(content) else json.dumps(content)
            elif result.get("structured_content") is not None:
                output = json.dumps(result["structured_content"])
            else:
                output = json.dumps(result)
        elif result is not None:
            output = result if isinstance(result, str) else json.dumps(result)
        if item.get("status") == "failed" and not message:
            message = "the MCP tool call failed"
        args = item.get("arguments")
        server = str(item.get("server") or "")
        self.out.mcp(self.servers.get(server, server), str(item.get("tool") or ""),
                     args if isinstance(args, str) else (args or {}), output=output, error=message)

    def _web_search(self, item: dict[str, Any]) -> None:
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        atype = action.get("type")
        if atype == "open_page":
            act: dict[str, Any] = {"type": "open_page", "url": action.get("url")}
        elif atype == "find_in_page" and action.get("url") and action.get("pattern"):
            act = {"type": "find_in_page", "url": action["url"], "pattern": action["pattern"]}
        else:
            act = {"type": "search", "query": str(item.get("query") or action.get("query") or "")}
            if isinstance(action.get("queries"), list):
                act["queries"] = [str(q) for q in action["queries"]]
        item_id = str(item.get("id") or "")
        ws = {"type": "web_search_call", "status": "completed", "action": act}
        if item_id.startswith("ws_"):                   # the provider's own web_search_call id
            ws["id"] = item_id
        self.out.item(ws)

    def _other(self, item: dict[str, Any]) -> None:
        kind = str(item.get("type") or "tool")
        if kind == "todo_list":
            self.out.function("update_plan", {"items": item.get("items") or []}, output="plan updated")
            return
        output = None
        for key in ("result", "output", "aggregated_output"):
            if item.get(key) is not None:
                output = item[key] if isinstance(item[key], str) else json.dumps(item[key])
                break
        args = {k: v for k, v in item.items() if k not in ("id", "type")}
        self.out.function(kind, args, output=output if output is not None else str(item.get("status") or "done"),
                          ok=item.get("status") != "failed")

    # ── the end ────────────────────────────────────────────────────────────────────────────────────────────────────
    def response_usage(self) -> dict[str, Any] | None:
        if self.usage is None:
            return None
        cur, base = self.usage, self.baseline
        grew = base and all(cur[k] >= base.get(k, 0) for k in USAGE_FIELDS)
        u = {k: cur[k] - base.get(k, 0) for k in USAGE_FIELDS} if grew else cur
        return Emitter.usage(input_tokens=u["input_tokens"], output_tokens=u["output_tokens"],
                             cached=u["cached_input_tokens"], reasoning=u["reasoning_output_tokens"],
                             cache_write=u["cache_write_input_tokens"])

    def finish(self, exit_code: int | None = 0, stderr: str = "") -> None:
        """The one terminal event, after Codex has exited."""
        for iid, opened in list(self.open_shell.items()):   # a command Codex never finished
            call = {**opened["item"], "status": "incomplete"}
            self.out.emit({"type": "response.output_item.done", "output_index": opened["index"], "item": call})
            self.out.output.append(call)
            self.open_shell.pop(iid, None)
        usage = self.response_usage()
        if self.turn_failed is not None:
            self.out.failed(_error_code(self.turn_failed), self.turn_failed, usage=usage)
        elif self.turn_completed:
            self.out.completed(usage=usage, metadata={"codex_thread_id": self.thread_id} if self.thread_id else {})
        else:
            tail = stderr.strip().splitlines()[-5:]
            message = self.last_error or " ".join(tail) or f"codex exited with code {exit_code} without finishing the turn"
            self.out.failed(_error_code(message), message[:4000], usage=usage)


# ── running the CLI ────────────────────────────────────────────────────────────────────────────────────────────────
def attachments(req: Request) -> tuple[list[str], list[str]]:
    """The request's files placed in the workspace: (images for -i, prompt notes). http(s) URLs are only named (the
    sandbox cannot fetch them)."""
    images: list[str] = []
    notes: list[str] = []
    for name in req.place_inputs(WORKSPACE):
        if name.lower().endswith(IMAGE_EXT):
            images.append(str(Path(WORKSPACE, name)))
        else:
            notes.append(f"./{name}")
    given = req.get("input")
    for item in given if isinstance(given, list) else []:
        content = item.get("content") if isinstance(item, dict) else None
        for part in [item, *(content if isinstance(content, list) else [])]:
            url = part.get("image_url") or part.get("file_url") if isinstance(part, dict) else None
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                notes.append(url)
    return images, notes


def overrides(req: Request, model: str) -> tuple[list[str], dict[str, str]]:
    """Codex's configuration for this turn, as `-c key=value` overrides on top of $HOME/.codex/config.toml (which
    belongs to the organization's seeds), and the Codex server name -> label of each MCP server."""
    reasoning = req.get("reasoning") or {}
    text = req.get("text") or {}
    pairs: list[tuple[str, str]] = [
        ("model_provider", '"hg"'),
        ("model", toml_str(model)),
        ("approval_policy", '"never"'),
        ("sandbox_mode", '"danger-full-access"'),          # the gVisor sandbox is the boundary
        ("check_for_update_on_startup", "false"),
        ("hide_agent_reasoning", "false"),
    ]
    catalog = Path(os.environ.get("HG_TOOLS", "/opt/hg/tools")) / "codex-models.json"
    if catalog.is_file():
        pairs.append(("model_catalog_json", toml_str(str(catalog))))
    effort = reasoning.get("effort")
    if effort in EFFORTS:
        pairs.append(("model_reasoning_effort", toml_str(effort)))
    summary = reasoning.get("summary") or reasoning.get("generate_summary") or "auto"
    if summary in SUMMARIES:
        pairs.append(("model_reasoning_summary", toml_str(summary)))
    if text.get("verbosity") in VERBOSITY:
        pairs.append(("model_verbosity", toml_str(text["verbosity"])))
    if req.instructions:
        pairs.append(("developer_instructions", toml_str(req.instructions)))
    tools = [t for t in req.get("tools") or [] if isinstance(t, dict)]
    web = any(str(t.get("type", "")).startswith("web_search") for t in tools)
    pairs.append(("web_search", '"live"' if web else '"disabled"'))
    url = os.environ.get("HG_PROVIDER_URL", "").rstrip("/") + "/v1"
    pairs.append(("model_providers.hg", "{" + ", ".join((
        'name = "HarnessGateway"', f"base_url = {toml_str(url)}", 'env_key = "HG_CODEX_API_KEY"',
        'wire_api = "responses"', "request_max_retries = 2", "stream_max_retries = 2")) + "}"))
    pairs += [("analytics.enabled", "false"), ("feedback.enabled", "false")]
    pairs += [(f"features.{f}", "false") for f in ("unbounded_connection_retries", "plugins", "apps", "remote_plugin",
                                                   "daemon_auto_start", "in_app_updates")]
    servers: dict[str, str] = {}
    for s in req.mcp_servers():
        key = mcp_key(str(s["label"]))
        servers[key] = str(s["label"])
        fields = [f"url = {toml_str(str(s['url']))}", "startup_timeout_sec = 30"]
        if s.get("headers"):        # placeholders: the door puts the values in on the way to the server's host
            fields.append("http_headers = {" + ", ".join(f"{toml_str(str(k))} = {toml_str(str(v))}"
                                                          for k, v in s["headers"].items()) + "}")
        if s.get("allowed_tools") is not None:
            fields.append("enabled_tools = [" + ", ".join(toml_str(str(n)) for n in s["allowed_tools"]) + "]")
        pairs.append((f"mcp_servers.{key}", "{" + ", ".join(fields) + "}"))
    return [f"{k}={v}" for k, v in pairs], servers


def command(req: Request, model: str, prompt: str, images: list[str], config: list[str], thread: str = "") -> list[str]:
    args = ["--json", "--skip-git-repo-check", "-m", model]
    for c in config:
        args += ["-c", c]
    fmt = req.text_format or {}
    if fmt.get("type") == "json_schema" and isinstance(fmt.get("schema"), dict):
        schema = TMP / "schema.json"
        schema.write_text(json.dumps(fmt["schema"]))
        args += ["--output-schema", str(schema)]
    if req.get("store") is False:
        args.append("--ephemeral")
    for img in images:
        args += ["-i", img]
    head = ["codex", "exec", "resume", *args, thread] if thread else ["codex", "exec", *args]
    return [*head, "--", prompt]


def environment() -> dict[str, str]:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(Path(env.get("HOME", "/home/hg")) / ".codex")
    env["HG_CODEX_API_KEY"] = env.get("HG_PROVIDER_TOKEN", "")     # the provider's env_key
    return env


def ignored(req: Request) -> list[str]:
    """Request fields Codex cannot honour, when present."""
    fields = [f for f in IGNORED if req.get(f) is not None]
    if any(isinstance(t, dict) and t.get("type") == "function" for t in req.get("tools") or []):
        fields.append("tools.function")
    return fields


def _run(argv: list[str], env: dict[str, str], mapper: Mapper) -> tuple[int, str]:
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                            cwd=WORKSPACE, text=True, bufsize=1)

    def stop(signum: int, _frame: Any) -> None:     # cancel: let Codex go, then leave without a terminal event
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(signum)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        os._exit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    tail: list[str] = []

    def drain() -> None:
        for line in proc.stderr:  # type: ignore[union-attr]
            sys.stderr.write(line)
            sys.stderr.flush()
            tail.append(line.rstrip("\n"))
            del tail[:-40]

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    for raw in proc.stdout:  # type: ignore[union-attr]
        if raw.strip():
            mapper.out.log(RAW_PREFIX + raw.rstrip("\n")[:65536])     # the raw stream, for the turn's log
        mapper.line(raw)
    code = proc.wait()
    t.join(timeout=5)
    return code, "\n".join(x for x in tail if not x.startswith(STDERR_NOISE))


def run(out: Emitter) -> None:
    req, state = Request.load(), State()
    out.ignored(ignored(req))
    model = os.environ.get("HG_MODEL") or "gpt-6.1-sol"
    if os.environ.get("HG_PROVIDER_API") not in ("openai", None, ""):
        out.failed("unsupported_provider", "Codex speaks the OpenAI Responses API: use an openai-shaped provider")
        return
    TMP.mkdir(parents=True, exist_ok=True)
    try:
        images, notes = attachments(req)
    except (OSError, ValueError) as e:
        out.failed("invalid_input_file", f"an input file could not be read: {e}")
        return
    prompt = req.prompt
    if notes:
        prompt = (prompt + "\n\n" if prompt else "") + "Attached files: " + ", ".join(notes)
    if not prompt and images:
        prompt = "See the attached image(s)."
    if not prompt:
        out.failed("invalid_prompt", "the request has no input text")
        return
    thread = state.get(THREAD_KEY) if req.get("previous_response_id") else ""
    instructions = req.instructions
    if thread and instructions and instructions != state.get(INSTRUCTIONS_KEY):
        # A resumed thread keeps the developer_instructions it started with, so new or changed instructions ride
        # along with this turn's prompt.
        prompt = f"<developer_instructions>\n{instructions}\n</developer_instructions>\n\n{prompt}"
    if instructions or not thread:
        state.set(INSTRUCTIONS_KEY, instructions)
    config, servers = overrides(req, model)
    env = environment()
    Path(env["CODEX_HOME"]).mkdir(parents=True, exist_ok=True)
    out.log(f"hg-codex: running codex exec{' resume ' + thread if thread else ''} model={model} images={len(images)}")
    mapper = Mapper(out, state, servers=servers)
    code, stderr = _run(command(req, model, prompt, images, config, thread), env, mapper)
    mapper.finish(code, stderr)


def main() -> None:
    out = Emitter()
    try:
        run(out)
    except Exception as e:
        import traceback
        traceback.print_exc()
        if not out.ended:
            out.failed("harness_error", f"the codex package failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
