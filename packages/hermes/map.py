"""Hermes Agent's ACP session, mapped onto Responses stream events (hgkit.Emitter).

run.py drives `hermes acp` (Agent Client Protocol: JSON-RPC 2.0 over stdio) and feeds this mapper four kinds of
record, in the order they happened:

    ("acp", msg)   a `session/update` notification sent during `session/prompt`:
                   agent_message_chunk -> output_text deltas (token-level, Hermes streams them),
                   agent_thought_chunk -> a reasoning item, tool_call -> remembered (an edit's diff: old/new text)
    ("log", line)  a line of Hermes's stderr log: tells a turn that ended normally ("Turn ended: ...") from one a
                   provider error ended ("[ERROR] agent.conversation_loop: ...")
    ("db", rows)   new rows of Hermes's own transcript (`messages` in $HERMES_HOME/state.db): the exact tool calls
                   (name, JSON arguments) and their exact results, which ACP only renders as prose for an editor.
                   terminal -> shell_call + shell_call_output; write_file / patch -> apply_patch_call + output;
                   mcp_<server>_<tool> -> mcp_call; every other tool -> function_call + function_call_output
    ("end", info)  the `session/prompt` result (stopReason, usage) or how the turn stopped -> the terminal event

The records are also what tests/ replays: the mapper never reads the clock, the environment or the filesystem.
"""
from __future__ import annotations

import difflib
import json
import re
from typing import Any

from hgkit import Emitter

ERROR_LINE = re.compile(r"\[ERROR\] agent\.conversation_loop: (.*)")
TURN_ENDED = re.compile(r"Turn ended(?: with pending tool result \(agent may appear stuck\)\.)?:? reason=(\S+)")
API_CALL_OK = re.compile(r"\[INFO\] agent\.conversation_loop: API call #\d+")
WORKSPACE = "/workspace/"


def _loads(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except ValueError:
        try:
            return json.JSONDecoder().raw_decode(text.strip())[0]
        except ValueError:
            return None


def _rel(path: str) -> str:
    return path[len(WORKSPACE):] if path.startswith(WORKSPACE) else path


def _plus(text: str) -> str:
    """A whole new file as a V4A create_file diff: every line added."""
    return "".join(f"+{line}\n" for line in text.splitlines())


def _udiff(old: str, new: str) -> str:
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=3)
    return "\n".join(line for line in lines if not line.startswith(("---", "+++"))) + "\n"


def _split_diff(diff: str) -> dict[str, str]:
    """A multi-file unified diff (`--- a/x` / `+++ b/x` headers) into {path: hunks}."""
    out: dict[str, list[str]] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[4:].strip()
            current = current[2:] if current.startswith("b/") else current
            out.setdefault(current, [])
        elif line.startswith("--- ") or line.startswith("diff --git"):
            continue
        elif current is not None:
            out[current].append(line)
    return {k: "\n".join(v) + "\n" for k, v in out.items()}


class Mapper:
    def __init__(self, out: Emitter, mcp_servers: dict[str, str] | None = None):
        """mcp_servers: Hermes's sanitized server name -> the request's server_label."""
        self.out = out
        self.mcp_servers = mcp_servers or {}
        self.reasoning = ""
        self.pending: dict[str, dict[str, Any]] = {}     # call id -> {"name", "args"}, in call order
        self.done: set[str] = set()
        self.edits: list[dict[str, Any]] = []             # ACP diffs of write_file/patch tool_calls, FIFO
        self.acp_tools: dict[str, dict[str, Any]] = {}    # ACP tool calls, for when the transcript is unreadable
        self.error: str | None = None                     # a provider error ended the turn (so far)
        self.error_text = ""                              # what Hermes said about it (held back from the answer)
        self.turn_exit: str | None = None
        self.tools_mapped = 0

    # ── records ────────────────────────────────────────────────────────────────────────────────────────────────
    def record(self, kind: str, value: Any) -> None:
        getattr(self, kind)(value)

    def acp(self, msg: dict[str, Any]) -> None:
        if msg.get("method") != "session/update":
            return
        update = (msg.get("params") or {}).get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = (update.get("content") or {}).get("text") or ""
            if self.error is not None:
                self.error_text += text                   # Hermes's error summary, not an answer
                return
            self._flush_reasoning()
            if self.out._msg is None:                     # Hermes opens a message after tools with blank lines
                text = text.lstrip("\n")
            self.out.text(text)
        elif kind == "agent_thought_chunk":
            self.reasoning += (update.get("content") or {}).get("text") or ""
        elif kind == "tool_call":
            tc = {"title": update.get("title") or "", "kind": update.get("kind") or "other",
                  "content": update.get("content") or [], "rawInput": update.get("rawInput")}
            self.acp_tools[update.get("toolCallId") or str(len(self.acp_tools))] = tc
            for c in tc["content"]:
                if isinstance(c, dict) and c.get("type") == "diff":
                    self.edits.append({"path": c.get("path") or "", "old": c.get("oldText"), "new": c.get("newText")})
        elif kind == "tool_call_update":
            tc = self.acp_tools.get(update.get("toolCallId") or "")
            if tc is not None:
                tc.update(status=update.get("status"), result=update.get("content") or [],
                          rawOutput=update.get("rawOutput"))

    def log(self, line: str) -> None:
        if m := ERROR_LINE.search(line):
            self.error = self.error or m.group(1).strip()
        elif API_CALL_OK.search(line):
            if self.error is not None:                    # a retry succeeded after all: give back what was held
                held, self.error, self.error_text = self.error_text, None, ""
                if held:
                    self.out.text(held)
        elif m := TURN_ENDED.search(line):
            self.turn_exit = m.group(1)

    def db(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            if row.get("role") == "assistant" and row.get("tool_calls"):
                for tc in _loads(row["tool_calls"]) or []:
                    fn = tc.get("function") or {}
                    cid = tc.get("id") or tc.get("call_id")
                    if cid and cid not in self.done and cid not in self.pending:
                        self.pending[cid] = {"name": fn.get("name") or "tool", "args": fn.get("arguments") or "{}"}
            elif row.get("role") == "tool":
                cid = row.get("tool_call_id")
                call = self.pending.pop(cid, None) if cid else None
                if call is not None:
                    self._tool(cid, call["name"], call["args"], row.get("content") or "")

    def end(self, info: dict[str, Any]) -> None:
        """info: {"stopReason", "usage"} from session/prompt, plus the driver's own facts:
        "cache_write" (from the session row), "failure" ({"code","message"}) when the turn could not run,
        "transcript": False when state.db could not be read."""
        self._flush_reasoning()
        for cid, call in list(self.pending.items()):      # called but never answered (cancelled, crashed)
            self._tool(cid, call["name"], call["args"], None)
        self.pending.clear()
        if info.get("transcript") is False and not self.tools_mapped:
            self._acp_fallback()
        usage = self._usage(info.get("usage"), info.get("cache_write", 0))
        failure = info.get("failure")
        stop = info.get("stopReason")
        if failure:
            self.out.failed(failure.get("code", "harness_error"), failure.get("message", ""), usage=usage)
        elif self.error is not None and self.turn_exit is None:
            message = self.error_text.strip() or self.error
            self.out.failed("provider_error", message[:4000], usage=usage)
        elif stop == "cancelled":
            self.out.failed("cancelled", "the turn was cancelled", usage=usage)
        elif stop == "refusal":
            self.out.failed("session_not_found", "Hermes did not find the session to continue", usage=usage)
        else:
            meta = {"hermes_turn_exit": self.turn_exit} if self.turn_exit else {}
            self.out.completed(usage=usage, metadata=meta)

    # ── items ──────────────────────────────────────────────────────────────────────────────────────────────────
    def _flush_reasoning(self) -> None:
        if self.reasoning.strip():
            self.out.reasoning(self.reasoning.strip())
        self.reasoning = ""

    def _tool(self, cid: str, name: str, raw_args: str, result: str | None) -> None:
        self._flush_reasoning()
        self.done.add(cid)
        self.tools_mapped += 1
        args = _loads(raw_args)
        args = args if isinstance(args, dict) else {"raw": raw_args}
        data = _loads(result) if result is not None else None
        data = data if isinstance(data, dict) else {}
        if name == "terminal" and isinstance(args.get("command"), str):
            if result is None:
                self.out.item({"type": "shell_call", "call_id": cid, "status": "incomplete",
                               "action": {"commands": [args["command"]]}})
                return
            code = data.get("exit_code")
            self.out.shell(args["command"], call_id=cid, stdout=str(data.get("output") or ("" if data else result)),
                           stderr=str(data.get("error") or ""), exit_code=code if isinstance(code, int) else None)
            return
        if name in ("write_file", "patch") and self._patch(cid, name, args, data, result):
            return
        server, tool = self._mcp_name(name)
        if server is not None:
            err = data.get("error") if isinstance(data.get("error"), str) else None
            self.out.mcp(server, tool, args, output=None if err else result, error=err or (
                "the call did not finish" if result is None else None))
            return
        failed = result is None or data.get("success") is False or bool(data.get("error") and not data.get("content"))
        self.out.function(name, args, output=result if result is not None else "", ok=not failed, call_id=cid)

    def _patch(self, cid: str, name: str, args: dict[str, Any], data: dict[str, Any], result: str | None) -> bool:
        ok = result is not None and not data.get("error") and data.get("success") is not False
        output = (data.get("error") or data.get("message") or "") if result is not None else "the call did not finish"
        if name == "write_file" and isinstance(args.get("path"), str):
            path, content = args["path"], str(args.get("content") or "")
            edit = self._edit_for(path)
            if edit and edit.get("old") is not None:
                self.out.patch("update_file", _rel(path), _udiff(edit["old"], edit.get("new") or content),
                               ok=ok, output=str(output), call_id=cid)
            else:
                self.out.patch("create_file", _rel(path), _plus(content), ok=ok, output=str(output), call_id=cid)
            return True
        if name == "patch":
            diff = str(data.get("diff") or "")
            files = [("create_file", p) for p in data.get("files_created") or []] + \
                    [("update_file", p) for p in data.get("files_modified") or []] + \
                    [("delete_file", p) for p in data.get("files_deleted") or []]
            if not files and isinstance(args.get("path"), str):
                files = [("update_file", args["path"])]
            if not files:
                return False
            parts = _split_diff(diff) if diff else {}
            for i, (op, path) in enumerate(files):
                rel = _rel(str(path))
                hunk = next((v for k, v in parts.items() if k == rel or k.endswith("/" + rel) or rel.endswith("/" + k)),
                            None)
                if hunk is None:
                    edit = self._edit_for(str(path))
                    if edit and edit.get("old") is not None and edit.get("new") is not None:
                        hunk = _udiff(edit["old"], edit["new"])
                    elif args.get("old_string") is not None:
                        hunk = "".join(f"-{x}\n" for x in str(args["old_string"]).splitlines()) + \
                               "".join(f"+{x}\n" for x in str(args.get("new_string") or "").splitlines())
                    else:
                        hunk = diff or str(args.get("patch") or "")
                self.out.patch(op, rel, hunk, ok=ok, output=str(output),
                               call_id=cid if i == 0 else f"{cid}_{i}")
            return True
        return False

    def _edit_for(self, path: str) -> dict[str, Any] | None:
        for i, e in enumerate(self.edits):
            p = e["path"]
            if p == path or _rel(p) == _rel(path) or p.endswith("/" + _rel(path)) or path.endswith("/" + _rel(p)):
                return self.edits.pop(i)
        return None

    def _mcp_name(self, name: str) -> tuple[str | None, str]:
        for sanitized, label in sorted(self.mcp_servers.items(), key=lambda kv: -len(kv[0])):
            prefix = f"mcp_{sanitized}_"
            if name.startswith(prefix):
                return label, name[len(prefix):]
        return None, name

    def _acp_fallback(self) -> None:
        """No transcript: map ACP's own (editor-oriented) tool records."""
        for tid, tc in self.acp_tools.items():
            text = "\n".join(c.get("content", {}).get("text", "") for c in tc.get("result") or []
                             if isinstance(c, dict) and c.get("type") == "content")
            command = next((c.get("content", {}).get("text", "")[2:] for c in tc.get("content") or []
                            if isinstance(c, dict) and str(c.get("content", {}).get("text", "")).startswith("$ ")), None)
            if tc.get("kind") == "execute" and command:
                self.out.shell(command, stdout=text, exit_code=0 if tc.get("status") == "completed" else 1, call_id=tid)
            else:
                self.out.function(tc.get("title") or "tool", tc.get("rawInput") or {}, output=text,
                                  ok=tc.get("status") == "completed", call_id=tid)

    @staticmethod
    def _usage(usage: dict[str, Any] | None, cache_write: int) -> dict[str, Any] | None:
        """ACP's Usage counts input tokens the way Hermes does (input + cache read + cache write): as OpenAI does."""
        if not usage:
            return None
        return Emitter.usage(input_tokens=int(usage.get("inputTokens") or 0),
                             output_tokens=int(usage.get("outputTokens") or 0),
                             cached=int(usage.get("cachedReadTokens") or 0),
                             reasoning=int(usage.get("thoughtTokens") or 0),
                             cache_write=max(0, int(cache_write or 0)))
