"""Pi, one turn: the Responses request in, `pi -p --mode json` run, its events out as Responses stream events.

Pi's --mode json stream (docs/json.md in the package) maps like this:

  message_update text_delta            -> output_text deltas, streamed as they arrive
  message_update thinking_end          -> a reasoning item (the block's authoritative text)
  tool_execution_start/_end   bash     -> shell_call at start, shell_call_output (with the exit code) at end
                              edit     -> apply_patch_call (update_file, Pi's unified patch) + its output
                              write    -> apply_patch_call (create_file) + its output
                              mcp__*   -> mcp_call (server, tool, arguments, output or error)
                              other    -> function_call at start, function_call_output at end
  message_end (assistant)              -> usage summed over every assistant message of the turn;
                                          stopReason error -> response.failed, length -> response.incomplete
  auto_retry_end success=false         -> response.failed with Pi's final error

`Mapper` is the pure mapping (fed one decoded event at a time, tested offline in tests/); `run` configures and runs Pi.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from hgkit import Emitter, Request, State

EFFORTS = {"none": "off", "minimal": "minimal", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh",
           "max": "max"}
SHELL_TOOLS = {"bash", "shell", "powershell"}
_EXIT = re.compile(r"Command exited with code (-?\d+)\s*$")
# Request fields Pi cannot honour, named in metadata.hg_ignored_fields when a request sets them.
IGNORED = ("tool_choice", "parallel_tool_calls", "temperature", "top_p", "top_logprobs", "max_tool_calls",
           "truncation", "include", "prompt_cache_key", "service_tier")


def _text(result: Any) -> str:
    """A tool result's text: Pi results are {content: [{type: text, text}], details, ...}."""
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            return "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
        if isinstance(content, str):
            return content
        return json.dumps(result, ensure_ascii=False)
    return "" if result is None else str(result)


def _server(label: str) -> str:
    """A label as a Pi MCP server name (letters, digits, _ and -)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", label)


def _tool_name(name: str) -> str:
    """How Pi names a server in its tool names (mcp__<server>__<tool>): every character other than letters, digits
    and _ becomes _."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _plus(text: str) -> str:
    """A new file's content as an apply_patch (V4A) create_file diff: every line added."""
    return "".join(f"+{line}\n" for line in text.splitlines())


class Mapper:
    """Pi's JSON events -> Responses events through an hgkit Emitter. Feed events, then call finish()."""

    def __init__(self, out: Emitter, mcp_labels: list[str] | None = None):
        self.out = out
        self.labels = {_tool_name(x): x for x in mcp_labels or []}
        self.calls: dict[str, dict[str, Any]] = {}   # toolCallId -> {name, args}
        self.usage = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0}
        self.saw_usage = False
        self.error: str | None = None
        self.stop_reason = ""
        self.session_id = ""
        self.assistant_messages = 0
        self.thinking: dict[Any, str] = {}            # contentIndex -> thinking text not yet emitted
        self.flushed: set[Any] = set()                # contentIndexes of this message already emitted

    # ── events ─────────────────────────────────────────────────────────────────────────────────────────────────────
    def feed(self, e: dict[str, Any]) -> None:
        kind = e.get("type")
        if kind == "session":
            self.session_id = str(e.get("id") or "")
        elif kind == "message_update":
            self._update(e.get("assistantMessageEvent") or {})
        elif kind == "message_end":
            self._message_end(e.get("message") or {})
        elif kind == "tool_execution_start":
            self._tool_start(e)
        elif kind == "tool_execution_end":
            self._tool_end(e)
        elif kind == "auto_retry_start":
            self.out.log(f"pi: retrying ({e.get('attempt')}/{e.get('maxAttempts')}): {e.get('errorMessage', '')}")
        elif kind == "auto_retry_end":
            if e.get("success") is False:
                self.error = str(e.get("finalError") or self.error or "the model call failed after retries")
            elif e.get("success"):
                self.error = None
        elif kind == "compaction_end" and e.get("errorMessage"):
            self.out.log(f"pi: compaction failed: {e['errorMessage']}")

    def _flush_thinking(self) -> None:
        """Thinking still being gathered goes out as a reasoning item before anything that follows it (some APIs end
        the thinking block only when the whole message ends)."""
        for idx, text in list(self.thinking.items()):
            self.out.reasoning(text)
            self.flushed.add(idx)
        self.thinking.clear()

    def _update(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        idx = ev.get("contentIndex")
        if t == "text_delta":
            if self.thinking:
                self._flush_thinking()
            self.out.text(str(ev.get("delta") or ""))
        elif t == "thinking_delta":
            if idx not in self.flushed:
                self.thinking[idx] = self.thinking.get(idx, "") + str(ev.get("delta") or "")
        elif t == "thinking_end":
            if idx not in self.flushed:
                self.thinking.pop(idx, None)
                self.out.reasoning(str(ev.get("content") or ""))
                self.flushed.add(idx)
        elif t == "toolcall_start" and self.thinking:
            self._flush_thinking()
        elif t == "error":
            self.out.log(f"pi: stream error: {ev.get('error') or ev.get('reason')}")

    def _message_end(self, m: dict[str, Any]) -> None:
        if m.get("role") != "assistant":
            return
        self._flush_thinking()
        self.flushed.clear()
        self.out.close_text()
        self.assistant_messages += 1
        u = m.get("usage")
        if isinstance(u, dict):
            for k in self.usage:
                v = u.get(k)
                if isinstance(v, (int, float)):
                    self.usage[k] += int(v)
                    self.saw_usage = self.saw_usage or v > 0
        self.stop_reason = str(m.get("stopReason") or "")
        if self.stop_reason == "error":
            self.error = str(m.get("errorMessage") or "the model call ended in an error")
        elif self.stop_reason in ("stop", "toolUse", "length"):
            self.error = None                          # a later message succeeded after a recovered error

    def _tool_start(self, e: dict[str, Any]) -> None:
        cid = str(e.get("toolCallId") or f"call_{len(self.calls) + 1}")
        name = str(e.get("toolName") or "")
        args = e.get("args") if isinstance(e.get("args"), dict) else {}
        self.calls[cid] = {"name": name, "args": args}
        low = name.lower()
        if low in SHELL_TOOLS:
            self.out.item({"type": "shell_call", "call_id": cid, "status": "completed",
                           "action": {"commands": [str(args.get("command", ""))],
                                      **({"timeout_ms": int(args["timeout"] * 1000)}
                                         if isinstance(args.get("timeout"), (int, float)) else {})}})
        elif low in ("edit", "write") or low.startswith("mcp__"):
            pass                                       # emitted whole at the end (the patch / result is known then)
        else:
            self.out.item({"type": "function_call", "call_id": cid, "name": name,
                           "arguments": json.dumps(args, ensure_ascii=False), "status": "completed"})

    def _tool_end(self, e: dict[str, Any]) -> None:
        cid = str(e.get("toolCallId") or "")
        call = self.calls.pop(cid, None) or {"name": str(e.get("toolName") or ""), "args": {}}
        name, args = call["name"], call["args"]
        low = name.lower()
        result = e.get("result")
        text = _text(result)
        error = bool(e.get("isError"))
        if low in SHELL_TOOLS:
            sc = result.get("structuredContent") if isinstance(result, dict) else None
            code = sc.get("exit_code") if isinstance(sc, dict) else None
            if not isinstance(code, int):
                m = _EXIT.search(text)
                code = int(m.group(1)) if m else (1 if error else 0)
            timed_out = error and "timed out after" in text
            note = f"\n\nCommand exited with code {code}"
            stdout = text[:-len(note)] if code and text.endswith(note) else _EXIT.sub("", text) if code else text
            self.out.item({"type": "shell_call_output", "call_id": cid, "status": "completed",
                           "output": [{"stdout": stdout, "stderr": "",
                                       "outcome": {"type": "timeout"} if timed_out else
                                       {"type": "exit", "exit_code": code}}]})
        elif low in ("edit", "write"):
            path = str(args.get("path") or args.get("file_path") or "")
            if low == "write":
                op = {"type": "create_file", "path": path, "diff": _plus(str(args.get("content", "")))}
            else:
                details = result.get("details") if isinstance(result, dict) else None
                patch = details.get("patch") or details.get("diff") if isinstance(details, dict) else None
                if not patch:
                    edits = args.get("edits") if isinstance(args.get("edits"), list) else [args]
                    patch = "".join("".join(f"-{x}\n" for x in str(ed.get("oldText", "")).splitlines()) +
                                    "".join(f"+{x}\n" for x in str(ed.get("newText", "")).splitlines())
                                    for ed in edits if isinstance(ed, dict))
                op = {"type": "update_file", "path": path, "diff": str(patch)}
            self.out.item({"type": "apply_patch_call", "call_id": cid, "status": "completed", "operation": op})
            self.out.item({"type": "apply_patch_call_output", "call_id": cid,
                           "status": "failed" if error else "completed", "output": text})
        elif low.startswith("mcp__"):
            _, server, tool = (name.split("__", 2) + ["", ""])[:3]
            self.out.mcp(self.labels.get(server, server), tool, args, output=None if error else text,
                         error=(text or "the MCP tool failed") if error else None)
        else:
            self.out.item({"type": "function_call_output", "call_id": cid, "output": text,
                           "status": "incomplete" if error else "completed"})

    # ── the end ────────────────────────────────────────────────────────────────────────────────────────────────────
    def response_usage(self) -> dict[str, Any] | None:
        if not self.saw_usage:
            return None
        u = self.usage
        return Emitter.usage(input_tokens=u["input"] + u["cacheRead"] + u["cacheWrite"], output_tokens=u["output"],
                             cached=u["cacheRead"], cache_write=u["cacheWrite"], reasoning=u["reasoning"])

    def finish(self, returncode: int = 0, stderr_tail: str = "") -> None:
        """Exactly one terminal event, from what Pi reported (and its exit code when it reported nothing)."""
        usage = self.response_usage()
        meta = {"pi_session": self.session_id} if self.session_id else {}
        if self.error:
            low = self.error.lower()
            code = "rate_limit_exceeded" if ("429" in low or "rate limit" in low) else "provider_error"
            self.out.failed(code, self.error[:4000], usage=usage)
        elif returncode != 0 and self.assistant_messages == 0:
            tail = stderr_tail.strip()[-2000:] or f"pi exited with code {returncode}"
            self.out.failed("harness_error", tail, usage=usage)
        elif self.stop_reason == "length":
            self.out.incomplete("max_output_tokens", usage=usage)
        elif self.stop_reason == "aborted":
            self.out.failed("harness_error", "Pi aborted the turn", usage=usage)
        else:
            self.out.completed(usage=usage, metadata=meta)


# ── running Pi ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _catalog_entry(model: str, apis: tuple[str, ...]) -> dict[str, Any] | None:
    """Pi's own catalog entry for this model id (prefer its first-party provider), so the custom provider gets the
    right context window, output limit, reasoning support and compat flags."""
    tools = os.environ.get("HG_TOOLS", "/opt/hg/tools")
    files = sorted(set(glob.glob(f"{tools}/lib/node_modules/**/pi-ai/dist/providers/data/*.json", recursive=True)))
    preferred = ("openai", "anthropic", "deepseek")
    best: tuple[int, dict[str, Any]] | None = None
    for f in files:
        try:
            data = json.loads(Path(f).read_text())
        except (OSError, ValueError):
            continue
        for api, models in data.items():
            if api not in apis or not isinstance(models, dict):
                continue
            m = models.get(f"chat:{model}")
            if not isinstance(m, dict):
                continue
            rank = preferred.index(m.get("provider")) if m.get("provider") in preferred else len(preferred)
            rank = rank * 10 + apis.index(api)
            if best is None or rank < best[0]:
                best = (rank, m)
    return best[1] if best else None


def write_models_json(req: Request) -> None:
    """One custom provider, hg, at the platform's provider relay: the provider shape picks Pi's API."""
    model, shape = os.environ.get("HG_MODEL", ""), os.environ.get("HG_PROVIDER_API", "anthropic")
    base = os.environ.get("HG_PROVIDER_URL", "").rstrip("/")
    if shape == "openai":
        forced = os.environ.get("PI_OPENAI_API", "").strip()
        apis = (forced,) if forced in ("openai-responses", "openai-completions") else ("openai-responses",
                                                                                         "openai-completions")
        entry = _catalog_entry(model, apis) if not forced or forced in apis else None
        api = entry["api"] if entry else (forced or "openai-completions")
        base_url = f"{base}/v1"
    else:
        api, base_url = "anthropic-messages", base
        entry = _catalog_entry(model, ("anthropic-messages",))
    borrowed = entry or _catalog_entry(model, ("openai-completions", "openai-responses", "anthropic-messages")) or {}
    spec: dict[str, Any] = {"id": model, "name": model,
                            "reasoning": bool(borrowed.get("reasoning", True)),
                            "input": borrowed.get("input") or ["text", "image"],
                            "contextWindow": int(borrowed.get("contextWindow") or 128000),
                            "maxTokens": int(borrowed.get("maxTokens") or 32000),
                            "cost": borrowed.get("cost") or {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}
    if entry:                                     # same wire API: its compat and thinking map apply as they are
        for k in ("compat", "thinkingLevelMap", "inputLimits"):
            if entry.get(k):
                spec[k] = entry[k]
    elif api == "openai-completions" and model.lower().startswith("deepseek"):
        spec["compat"] = {"supportsStore": False, "supportsDeveloperRole": False, "maxTokensField": "max_tokens",
                          "requiresReasoningContentOnAssistantMessages": True, "thinkingFormat": "deepseek"}
    mot = req.get("max_output_tokens")
    if isinstance(mot, int) and mot > 0:
        spec["maxTokens"] = mot
    doc = {"providers": {"hg": {"name": "HarnessGateway", "baseUrl": base_url, "api": api,
                                "apiKey": "$HG_PROVIDER_TOKEN", "models": [spec]}}}
    agent = Path(os.environ["HOME"]) / ".pi" / "agent"
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "models.json").write_text(json.dumps(doc, indent=2))
    print(f"pi: provider hg api={api} base={base_url} model={model} catalog={'yes' if entry else 'no'}", flush=True)


def write_mcp_json(servers: list[dict[str, Any]], env: dict[str, str]) -> None:
    """The turn's MCP servers ($HG_MCP) as user-level Pi MCP servers with direct exposure (declared to the model as
    tools); allowed_tools narrows them. Rewritten every turn, so a turn sees exactly its own servers. Header values (the
    platform's placeholders) go through environment variables, so Pi never interprets them."""
    path = Path(os.environ["HOME"]) / ".pi" / "agent" / "mcp.json"
    entries: dict[str, Any] = {}
    for i, t in enumerate(servers):
        if t.get("transport") == "sse":
            print(f"pi: MCP server {t['label']} is left out: Pi speaks streamable HTTP, not SSE", flush=True)
            continue
        s: dict[str, Any] = {"url": t["url"], "exposure": "direct"}
        if t.get("headers"):
            s["headers"] = {}
            for j, (k, v) in enumerate(t["headers"].items()):
                env[f"HG_MCP_{i}_{j}"] = str(v)
                s["headers"][k] = "${" + f"HG_MCP_{i}_{j}" + "}"
        if t.get("allowed_tools") is not None:
            s["exposure"] = "hidden"
            s["toolExposure"] = {str(n): "direct" for n in t["allowed_tools"]}
        entries[_server(t["label"])] = s
    if entries:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcpServers": entries}, indent=2))
    elif path.exists():
        path.unlink()


def ignored(req: Request) -> list[str]:
    """The request fields this turn does not act on (only those the request sets)."""
    fields = [f for f in IGNORED if req.get(f) is not None]
    if any(isinstance(t, dict) and t.get("type") != "mcp" for t in req.get("tools") or []):
        fields.append("tools")                          # Pi runs its own tools; only MCP servers are added
    for parent, child in (("reasoning", "summary"), ("text", "verbosity")):
        if (req.get(parent) or {}).get(child) is not None:
            fields.append(f"{parent}.{child}")
    return fields


def system_append(req: Request) -> str:
    parts = [req.instructions]
    fmt = req.text_format or {}
    if fmt.get("type") == "json_schema":
        parts.append("Your final reply must be only a JSON value (no prose, no code fences) that validates against "
                     f"this JSON schema named {fmt.get('name', 'output')}:\n"
                     + json.dumps(fmt.get("schema") or {}, ensure_ascii=False))
    elif fmt.get("type") == "json_object":
        parts.append("Your final reply must be only a JSON object (no prose, no code fences).")
    return "\n\n".join(p for p in parts if p)


def build_argv(req: Request, files: list[str], servers: list[dict[str, Any]]) -> list[str]:
    argv = ["pi", "-p", "--mode", "json", "--provider", "hg", "--model", os.environ.get("HG_MODEL", ""), "--approve"]
    if req.get("store", True) is False:
        argv.append("--no-session")
    else:
        argv += ["--session-id", os.environ["HG_SESSION"]]
    extra = system_append(req)
    if extra:
        argv += ["--append-system-prompt", extra]
    effort = EFFORTS.get(req.reasoning_effort)
    if effort:
        argv += ["--thinking", effort]
    if not servers and not Path(".pi", "mcp.json").is_file():
        argv.append("--no-mcp")                       # no servers to connect: skip MCP startup altogether
    return argv + ["--", *(f"@{name}" for name in files), req.prompt]


def run(out: Emitter) -> None:
    req = Request.load()
    out.ignored(ignored(req))
    try:
        files = req.place_inputs()
    except OSError as e:
        out.failed("invalid_input_file", f"an input file could not be saved into the workspace: {e}")
        return
    if not req.prompt and not files:
        out.failed("invalid_prompt", "the request has no input text or files for Pi")
        return
    if not os.environ.get("HG_PROVIDER_URL"):
        out.failed("harness_error", "Pi needs a provider: call it as pi/<provider>/<model>")
        return
    servers = Request.mcp_servers()
    env = {**os.environ, "PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0", "NO_COLOR": "1"}
    write_models_json(req)
    write_mcp_json(servers, env)
    raw = os.environ.get("PI_RAW_EVENTS") == "1"
    proc = subprocess.Popen(build_argv(req, files, servers), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    tail = bytearray()

    def pump_stderr() -> None:
        assert proc.stderr is not None
        for chunk in iter(lambda: proc.stderr.read1(65536), b""):
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()
            tail.extend(chunk)
            del tail[:-8192]

    threading.Thread(target=pump_stderr, daemon=True).start()
    mapper = Mapper(out, [s["label"] for s in servers])
    assert proc.stdout is not None
    for line in proc.stdout:                          # split on LF only, as Pi's JSONL framing requires
        s = line.decode("utf-8", "replace").rstrip("\r\n")
        if not s.strip():
            continue
        if raw:
            sys.stderr.write(f"PI_RAW {s}\n")
            sys.stderr.flush()
        try:
            event = json.loads(s)
        except ValueError:
            out.log(f"pi: {s}")
            continue
        if isinstance(event, dict):
            mapper.feed(event)
    code = proc.wait()
    if mapper.session_id:
        State().set("pi_session", mapper.session_id)
    mapper.finish(code, tail.decode("utf-8", "replace"))


def main() -> None:
    out = Emitter()
    try:
        run(out)
    except Exception as e:
        import traceback
        traceback.print_exc()
        if not out.ended:
            out.failed("harness_error", f"the pi package failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
