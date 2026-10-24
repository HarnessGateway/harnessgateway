"""Cline's --json stream, mapped onto the OpenAI Responses stream.

  run_start (with -v)        -> the session id into $HG_STATE/session
  agent_event content_end text      -> a message (the accumulated text as one delta, then the item)
  agent_event content_end reasoning -> a reasoning item
  agent_event content_start/end tool -> shell_call (bash), apply_patch_call (editor, apply_patch), mcp_call (mcp__*), function_call, each with its output
  run_result                 -> usage on response.completed; a finishReason other than completed fails the turn
"""
import json
import os
import sys
from pathlib import Path

n = 0
calls: dict[str, tuple[str, str]] = {}
failed = False
had_text = False


def emit(e: dict) -> None:
    print(json.dumps(e, ensure_ascii=False), flush=True)


def new_id(p: str) -> str:
    global n
    n += 1
    return f"{p}_{n}"


def done(item: dict) -> None:
    emit({"type": "response.output_item.done", "item": item})


def classify(name: str) -> str:
    low = (name or "").lower()
    if low.startswith("mcp__"):
        return "mcp_call"
    if low in ("bash", "shell", "execute_command"):
        return "shell_call"
    if low in ("editor", "apply_patch", "write_to_file", "replace_in_file", "edit"):
        return "apply_patch_call"
    return "function_call"


def content(ev: dict) -> None:
    global had_text
    ctype, kind = ev.get("contentType"), ev.get("type")
    if ctype == "text" and kind == "content_end":
        text = ev.get("accumulated") or ev.get("text") or ""
        if text:
            had_text = True
            mid = new_id("msg")
            emit({"type": "response.output_text.delta", "item_id": mid, "output_index": 0, "content_index": 0, "delta": text})
            done({"type": "message", "id": mid, "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]})
    elif ctype == "reasoning" and kind == "content_end":
        text = ev.get("accumulated") or ev.get("reasoning") or ""
        if text and not ev.get("redacted"):
            done({"type": "reasoning", "id": new_id("rs"), "status": "completed", "summary": [{"type": "summary_text", "text": text}]})
    elif ctype == "tool" and kind == "content_start":
        name, args, cid = ev.get("toolName", ""), ev.get("input") or {}, ev.get("toolCallId") or new_id("call")
        k = classify(name)
        calls[cid] = (k, name)
        if k == "shell_call":
            done({"type": "shell_call", "id": new_id("sh"), "call_id": cid, "status": "completed", "action": {"commands": [str(args.get("command", ""))]}})
        elif k == "apply_patch_call":
            done({"type": "apply_patch_call", "id": new_id("ap"), "call_id": cid, "status": "completed",
                  "operation": {"type": "update_file", "path": str(args.get("path", args.get("file_path", ""))), "diff": str(args.get("content", args.get("patch", args.get("file_text", ""))))}})
        elif k == "mcp_call":
            _, server, tool = (name.split("__", 2) + ["", ""])[:3]
            done({"type": "mcp_call", "id": cid, "server_label": server, "name": tool, "arguments": json.dumps(args), "status": "in_progress"})
        else:
            done({"type": "function_call", "id": new_id("fc"), "call_id": cid, "name": name, "arguments": json.dumps(args), "status": "completed"})
    elif ctype == "tool" and kind == "content_end":
        cid = ev.get("toolCallId") or ""
        k, name = calls.get(cid, ("function_call", ev.get("toolName", "")))
        out, error = ev.get("output"), ev.get("error")
        out = out if isinstance(out, str) else json.dumps(out) if out is not None else ""
        if k == "shell_call":
            done({"type": "shell_call_output", "id": new_id("sho"), "call_id": cid, "status": "completed", "output": [{"stdout": out, "stderr": str(error or ""), "outcome": {"type": "exit", "exit_code": 1 if error else 0}}]})
        elif k == "apply_patch_call":
            done({"type": "apply_patch_call_output", "id": new_id("apo"), "call_id": cid, "status": "failed" if error else "completed", "output": str(error or out)})
        elif k == "mcp_call":
            _, server, tool = (name.split("__", 2) + ["", ""])[:3]
            done({"type": "mcp_call", "id": cid, "server_label": server, "name": tool, "arguments": "{}", "status": "failed" if error else "completed", "output": None if error else out, "error": str(error) if error else None})
        else:
            done({"type": "function_call_output", "id": new_id("fco"), "call_id": cid, "output": str(error or out), "status": "incomplete" if error else "completed"})


def handle(e: dict) -> None:
    global failed
    kind = e.get("type")
    if kind == "run_start" and e.get("sessionId") and os.environ.get("HG_STATE"):
        Path(os.environ["HG_STATE"], "session").write_text(str(e["sessionId"]))
    elif kind == "agent_event" and isinstance(e.get("event"), dict):
        ev = e["event"]
        if ev.get("type") in ("content_start", "content_end"):
            content(ev)
        elif ev.get("type") == "error":
            failed = True
            print(f"cline error: {ev.get('error')}", flush=True)
    elif kind == "run_result":
        if not had_text and e.get("text"):
            done({"type": "message", "id": new_id("msg"), "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": e["text"], "annotations": []}]})
        u = e.get("usage") or {}
        inp, out = int(u.get("inputTokens", 0) or 0), int(u.get("outputTokens", 0) or 0)
        emit({"type": "response.completed", "response": {"usage": {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
              "input_tokens_details": {"cached_tokens": int(u.get("cacheReadTokens", 0) or 0), "cache_write_tokens": int(u.get("cacheWriteTokens", 0) or 0)},
              "output_tokens_details": {"reasoning_tokens": 0}}}})
        if e.get("finishReason") not in (None, "completed"):
            failed = True
            print(f"cline ended with {e.get('finishReason')}", flush=True)
    elif kind == "run_aborted":
        failed = True
        print(f"cline aborted: {e.get('message', '')}", flush=True)


for raw in sys.stdin:
    line = raw.rstrip("\n")
    if not line.strip():
        continue
    try:
        ev = json.loads(line)
    except ValueError:
        print(line, flush=True)
        continue
    handle(ev) if isinstance(ev, dict) else print(line, flush=True)
sys.exit(1 if failed else 0)
