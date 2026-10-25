"""Codex `exec --json` events -> OpenAI Responses stream events, with hgkit's Emitter.

  thread.started                      -> the thread id kept in $HG_STATE (what `codex exec resume` takes next turn)
  item.started  command_execution     -> a shell_call added in_progress (so a long command shows while it runs)
  item.updated/completed agent_message -> output_text deltas (only the new suffix each time), then the message
  item.completed reasoning            -> a reasoning item (the summary text)
  item.completed command_execution    -> shell_call done + shell_call_output (aggregated output, exit code)
  item.completed file_change          -> apply_patch_call + apply_patch_call_output per changed path
  item.completed mcp_tool_call        -> mcp_call (arguments, result content or error)
  item.completed web_search           -> web_search_call
  item.completed todo_list / other    -> function_call (+ function_call_output)
  item.completed error                -> a log line (Codex's non-fatal warnings)
  turn.completed                      -> usage (cached_input_tokens -> cached_tokens, cache_write_input_tokens
                                         -> cache_write_tokens, reasoning_output_tokens -> reasoning_tokens) on response.completed
  turn.failed / error                 -> response.failed with Codex's message (a top-level `error` alone may be a
                                         retry notice, so it fails the turn only if no turn.completed follows)

`CodexMapper.feed(line)` takes one raw stdout line from Codex; `finish(exit_code, stderr)` prints the terminal event.
Run as a script, it maps stdin to stdout (`codex exec --json ... | python3 map.py`).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from hgkit import Emitter, State

THREAD_KEY = "codex_thread"
USAGE_KEY = "codex_usage"          # the thread's cumulative usage after the last turn: {"thread": id, "usage": {...}}
USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
                "reasoning_output_tokens")
_PATCH_OPS = {"add": "create_file", "create": "create_file", "update": "update_file", "modify": "update_file",
              "delete": "delete_file", "remove": "delete_file"}
_CREATE_DIFF_MAX = 256 * 1024


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


class CodexMapper:
    def __init__(self, out: Emitter | None = None, state: State | None = None, workspace: str | None = None):
        self.out = out or Emitter()
        self.state = state or State()
        self.workspace = (workspace or os.environ.get("HG_WORKSPACE") or "/workspace").rstrip("/")
        self.thread_id = ""
        self.usage: dict[str, Any] | None = None      # Codex's cumulative thread usage, as last reported
        self.baseline: dict[str, int] = {}               # the thread's cumulative usage before this turn
        self.turn_completed = False
        self.turn_failed: str | None = None
        self.last_error = ""
        self.text_seen: dict[str, str] = {}          # agent_message id -> text already streamed
        self.text_item: str | None = None            # the agent_message currently open in the Emitter
        self.open_shell: dict[str, dict[str, Any]] = {}  # command_execution id -> the shell_call added in_progress
        self.tool_calls = 0

    # ── input ────────────────────────────────────────────────────────────────────────────────────────────────────
    def feed(self, line: str) -> None:
        s = line.strip()
        if not s:
            return
        try:
            event = json.loads(s)
        except ValueError:
            self.out.log(f"codex: {s}")
            return
        if not isinstance(event, dict):
            self.out.log(f"codex: {s}")
            return
        kind = event.get("type")
        item = event.get("item") if isinstance(event.get("item"), dict) else None
        if kind == "thread.started":
            self.thread_id = str(event.get("thread_id") or "")
            if self.thread_id:
                self.state.set(THREAD_KEY, self.thread_id)
                try:
                    prev = json.loads(self.state.get(USAGE_KEY) or "{}")
                except ValueError:
                    prev = {}
                if isinstance(prev, dict) and prev.get("thread") == self.thread_id and isinstance(prev.get("usage"), dict):
                    self.baseline = {k: _int(prev["usage"].get(k)) for k in USAGE_FIELDS}
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

    def _add_usage(self, u: dict[str, Any]) -> None:
        """turn.completed carries the THREAD's cumulative usage (`codex exec resume` restores the earlier turns'
        totals), so this turn's usage is the difference from what was recorded after the previous turn."""
        if not u:
            return
        self.usage = {k: _int(u.get(k)) for k in USAGE_FIELDS}
        if self.thread_id:
            self.state.set(USAGE_KEY, json.dumps({"thread": self.thread_id, "usage": self.usage}))

    # ── items ────────────────────────────────────────────────────────────────────────────────────────────────────
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
        if text.startswith(seen):
            delta = text[len(seen):]
        else:                                   # the text was rewritten: stream what is new from here on
            delta = text if not seen else ""
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
        self.tool_calls += 1
        iid = str(item.get("id") or "")
        opened = self.open_shell.pop(iid, None)
        status = item.get("status")
        call_status = "completed" if status in ("completed", "failed") else "incomplete"
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
            self.tool_calls += 1
            kind = change.get("kind")
            if isinstance(kind, dict):              # tolerate {"type": "add"} shapes
                kind = kind.get("type")
            op = _PATCH_OPS.get(str(kind or "update").lower(), "update_file")
            path = str(change.get("path") or "")
            diff = str(change.get("diff") or change.get("unified_diff") or "")
            if op == "create_file" and not diff:
                diff = self._create_diff(path)
            rel = path[len(self.workspace) + 1:] if path.startswith(self.workspace + "/") else path
            self.out.patch(op, rel, diff, ok=ok,
                           output=f"{'applied' if ok else 'failed'}: {op.replace('_file', '')} {rel}")

    @staticmethod
    def _create_diff(path: str) -> str:
        """A created file's content as an apply_patch create diff (each line prefixed with '+'), when it is readable."""
        try:
            p = Path(path)
            if not p.is_file() or p.stat().st_size > _CREATE_DIFF_MAX:
                return ""
            text = p.read_text(errors="replace")
        except OSError:
            return ""
        return "".join(f"+{line}\n" for line in text.splitlines())

    def _mcp(self, item: dict[str, Any]) -> None:
        self.tool_calls += 1
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
        mcp: dict[str, Any] = {"type": "mcp_call", "server_label": str(item.get("server") or ""),
                               "name": str(item.get("tool") or ""),
                               "arguments": args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False),
                               "status": "failed" if message else "completed", "output": output}
        if message:   # the SDK's McpCall.error is an object (hgkit's Emitter.mcp passes a string, which it refuses)
            mcp["error"] = {"type": "mcp_tool_execution_error", "content": [{"type": "text", "text": message}]}
        self.out.item(mcp)

    def _web_search(self, item: dict[str, Any]) -> None:
        self.tool_calls += 1
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
        args = {k: v for k, v in item.items() if k not in ("id", "type")}
        if kind == "todo_list":
            self.out.function("update_plan", {"items": item.get("items") or []}, output="plan updated")
            return
        self.tool_calls += 1
        output = None
        for key in ("result", "output", "aggregated_output"):
            if item.get(key) is not None:
                output = item[key] if isinstance(item[key], str) else json.dumps(item[key])
                break
        self.out.function(kind, args, output=output if output is not None else str(item.get("status") or "done"),
                          ok=item.get("status") != "failed")

    # ── the end ──────────────────────────────────────────────────────────────────────────────────────────────────
    def response_usage(self) -> dict[str, Any] | None:
        if self.usage is None:
            return None
        cur, base = self.usage, self.baseline
        if base and all(cur[k] >= base.get(k, 0) for k in USAGE_FIELDS):
            u = {k: cur[k] - base.get(k, 0) for k in USAGE_FIELDS}
        else:
            u = cur
        return Emitter.usage(input_tokens=u["input_tokens"], output_tokens=u["output_tokens"],
                             cached=u["cached_input_tokens"], reasoning=u["reasoning_output_tokens"],
                             cache_write=u["cache_write_input_tokens"])

    def finish(self, exit_code: int = 0, stderr_tail: str = "") -> None:
        """The one terminal event, after Codex has exited."""
        if self.out.ended:
            return
        for iid, opened in list(self.open_shell.items()):   # a command Codex never finished
            call = {**opened["item"], "status": "incomplete"}
            self.out.emit({"type": "response.output_item.done", "output_index": opened["index"], "item": call})
            self.out.output.append(call)
            self.open_shell.pop(iid, None)
        usage = self.response_usage()
        meta = {"codex_thread_id": self.thread_id} if self.thread_id else {}
        if self.turn_failed is not None:
            self.out.failed(_error_code(self.turn_failed), self.turn_failed, usage=usage)
        elif self.turn_completed:
            self.out.completed(usage=usage, metadata=meta)
        else:
            tail = stderr_tail.strip().splitlines()[-5:] if stderr_tail.strip() else []
            message = self.last_error or " ".join(tail) or f"codex exited with code {exit_code} without finishing the turn"
            self.out.failed(_error_code(message), message[:4000], usage=usage)


def main() -> None:
    mapper = CodexMapper()
    for raw in sys.stdin:
        mapper.feed(raw)
    mapper.finish(0)


if __name__ == "__main__":
    main()
