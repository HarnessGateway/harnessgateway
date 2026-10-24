"""Command Code's NDJSON (--output-format json), mapped onto the OpenAI Responses stream.

  event text_delta        -> response.output_text.delta, accumulated into one message
  event thinking_*        -> a reasoning item
  event tool_running / tool_completed / tool_errored -> shell_call (shell), apply_patch_call (write, edit), mcp_call (mcp__*), function_call, each with its result
  result                  -> the session id into $HG_STATE/session; usage on response.completed; a subtype other than success fails the turn
Only tool_running is documented; the other event names are the package's and may change with a release.
"""
import json
import os
import sys
from pathlib import Path

n = 0
text = ""
message_id = ""
thinking = ""
calls: dict[str, tuple[str, str]] = {}
failed = False


def emit(e: dict) -> None:
    print(json.dumps(e, ensure_ascii=False), flush=True)


def new_id(p: str) -> str:
    global n
    n += 1
    return f"{p}_{n}"


def done(item: dict) -> None:
    emit({"type": "response.output_item.done", "item": item})


def close_message() -> None:
    global text, message_id, thinking
    if thinking.strip():
        done({"type": "reasoning", "id": new_id("rs"), "status": "completed", "summary": [{"type": "summary_text", "text": thinking}]})
    thinking = ""
    if message_id:
        done({"type": "message", "id": message_id, "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]})
    text, message_id = "", ""


def classify(name: str) -> str:
    low = (name or "").lower()
    if low.startswith("mcp__"):
        return "mcp_call"
    if low in ("shell", "bash", "execute"):
        return "shell_call"
    if low in ("write", "edit", "write_file", "edit_file", "apply_patch", "multi_edit"):
        return "apply_patch_call"
    return "function_call"


def running(ev: dict) -> None:
    close_message()
    name, cid = str(ev.get("toolName", "")), str(ev.get("toolCallId") or new_id("call"))
    args = ev.get("input") or ev.get("args") or {}
    kind = classify(name)
    calls[cid] = (kind, name)
    if kind == "shell_call":
        done({"type": "shell_call", "id": new_id("sh"), "call_id": cid, "status": "completed", "action": {"commands": [str(args.get("command", ev.get("description", "")))]}})
    elif kind == "apply_patch_call":
        done({"type": "apply_patch_call", "id": new_id("ap"), "call_id": cid, "status": "completed",
              "operation": {"type": "update_file", "path": str(args.get("path", args.get("file_path", ""))), "diff": str(args.get("content", args.get("new_string", ev.get("description", ""))))}})
    elif kind == "mcp_call":
        _, server, tool = (name.split("__", 2) + ["", ""])[:3]
        done({"type": "mcp_call", "id": cid, "server_label": server, "name": tool, "arguments": json.dumps(args), "status": "in_progress"})
    else:
        done({"type": "function_call", "id": new_id("fc"), "call_id": cid, "name": name or "tool", "arguments": json.dumps(args) if args else json.dumps({"description": ev.get("description", "")}), "status": "completed"})


def finished(ev: dict, error: bool) -> None:
    cid = str(ev.get("toolCallId") or "")
    kind, name = calls.get(cid, ("function_call", str(ev.get("toolName", ""))))
    result = ev.get("error") if error else ev.get("result")
    out = result if isinstance(result, str) else json.dumps(result) if result is not None else ""
    if kind == "shell_call":
        done({"type": "shell_call_output", "id": new_id("sho"), "call_id": cid, "status": "completed", "output": [{"stdout": out, "stderr": "", "outcome": {"type": "exit", "exit_code": 1 if error else 0}}]})
    elif kind == "apply_patch_call":
        done({"type": "apply_patch_call_output", "id": new_id("apo"), "call_id": cid, "status": "failed" if error else "completed", "output": out})
    elif kind == "mcp_call":
        _, server, tool = (name.split("__", 2) + ["", ""])[:3]
        done({"type": "mcp_call", "id": cid, "server_label": server, "name": tool, "arguments": "{}", "status": "failed" if error else "completed", "output": None if error else out, "error": out if error else None})
    else:
        done({"type": "function_call_output", "id": new_id("fco"), "call_id": cid, "output": out, "status": "incomplete" if error else "completed"})


def handle(e: dict) -> None:
    global text, message_id, thinking, failed
    if e.get("type") == "event" and isinstance(e.get("event"), dict):
        ev = e["event"]
        kind = ev.get("type")
        if kind == "text_delta" and ev.get("delta"):
            if not message_id:
                message_id = new_id("msg")
                emit({"type": "response.output_item.added", "item": {"type": "message", "id": message_id, "role": "assistant", "status": "in_progress", "content": []}})
            text += ev["delta"]
            emit({"type": "response.output_text.delta", "item_id": message_id, "output_index": 0, "content_index": 0, "delta": ev["delta"]})
        elif kind == "thinking_delta" and ev.get("delta"):
            thinking += ev["delta"]
        elif kind == "tool_running":
            running(ev)
        elif kind == "tool_completed":
            finished(ev, False)
        elif kind in ("tool_errored", "tool_denied"):
            finished(ev, True)
        elif kind == "run_error":
            failed = True
            print(f"command-code error: {ev.get('error') or ev.get('message', '')}", flush=True)
    elif e.get("type") == "result":
        close_message()
        if e.get("sessionId") and os.environ.get("HG_STATE"):
            Path(os.environ["HG_STATE"], "session").write_text(str(e["sessionId"]))
        if not n and e.get("finalText"):
            done({"type": "message", "id": new_id("msg"), "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": e["finalText"], "annotations": []}]})
        u = e.get("usage") or {}
        inp, out = int(u.get("inputTokens", 0) or 0), int(u.get("outputTokens", 0) or 0)
        emit({"type": "response.completed", "response": {"usage": {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
              "input_tokens_details": {"cached_tokens": int(u.get("cacheReadTokens", 0) or 0), "cache_write_tokens": int(u.get("cacheWriteTokens", 0) or 0)},
              "output_tokens_details": {"reasoning_tokens": 0}}}})
        if e.get("subtype") not in (None, "success"):
            failed = True
            print(f"command-code ended with {e.get('subtype')}: {e.get('error') or ''}", flush=True)


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
close_message()
sys.exit(1 if failed else 0)
