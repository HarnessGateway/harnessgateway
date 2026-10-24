"""DeepSeek Harness's --json events, mapped onto the OpenAI Responses stream.

  session        -> the session id into $HG_STATE/session (what --session-id takes next turn)
  text           -> a message block (one delta, then the item);  thinking -> a reasoning item
  tool_call / tool_result -> shell_call (bash), apply_patch_call (fs writes), mcp_call (mcp__*), function_call, each with its result
  status step_end usage -> summed into usage on response.completed
  error          -> the turn fails (exit 1);  final -> the last text, if nothing else carried it
"""
import json
import os
import sys
from pathlib import Path

n = 0
calls: dict[str, tuple[str, str, dict]] = {}
usage = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
seen_usage = False
had_text = False
failed = False


def emit(e: dict) -> None:
    print(json.dumps(e, ensure_ascii=False), flush=True)


def new_id(p: str) -> str:
    global n
    n += 1
    return f"{p}_{n}"


def done(item: dict) -> None:
    emit({"type": "response.output_item.done", "item": item})


def classify(name: str) -> str:
    low = name.lower()
    if low.startswith("mcp__"):
        return "mcp_call"
    if low in ("bash", "pwsh", "shell"):
        return "shell_call"
    if low in ("fs_write", "write", "edit", "fs_edit", "apply_patch", "write_file", "edit_file", "str_replace"):
        return "apply_patch_call"
    return "function_call"


def tool_call(e: dict) -> None:
    name, args, cid = str(e.get("tool", "")), e.get("input") or {}, str(e.get("callId") or new_id("call"))
    kind = classify(name)
    calls[cid] = (kind, name, args if isinstance(args, dict) else {})
    if kind == "shell_call":
        done({"type": "shell_call", "id": new_id("sh"), "call_id": cid, "status": "completed", "action": {"commands": [str((args or {}).get("command", ""))]}})
    elif kind == "apply_patch_call":
        a = args if isinstance(args, dict) else {}
        done({"type": "apply_patch_call", "id": new_id("ap"), "call_id": cid, "status": "completed",
              "operation": {"type": "update_file", "path": str(a.get("path", a.get("file_path", ""))), "diff": str(a.get("content", a.get("new_string", a.get("patch", ""))))}})
    elif kind == "mcp_call":
        _, server, tool = (name.split("__", 2) + ["", ""])[:3]
        done({"type": "mcp_call", "id": cid, "server_label": server, "name": tool, "arguments": json.dumps(args), "status": "in_progress"})
    else:
        done({"type": "function_call", "id": new_id("fc"), "call_id": cid, "name": name, "arguments": json.dumps(args), "status": "completed"})


def tool_result(e: dict) -> None:
    cid = str(e.get("callId") or "")
    kind, name, _ = calls.get(cid, ("function_call", "", {}))
    result = e.get("result")
    out = result if isinstance(result, str) else json.dumps(result) if result is not None else ""
    error = e.get("status") == "error"
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
    global failed, seen_usage, had_text
    kind = e.get("type")
    if kind == "session" and e.get("sessionId") and os.environ.get("HG_STATE"):
        Path(os.environ["HG_STATE"], "session").write_text(str(e["sessionId"]))
    elif kind == "text" and e.get("text"):
        had_text = True
        mid = new_id("msg")
        emit({"type": "response.output_text.delta", "item_id": mid, "output_index": 0, "content_index": 0, "delta": e["text"]})
        done({"type": "message", "id": mid, "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": e["text"], "annotations": []}]})
    elif kind == "thinking" and e.get("text"):
        done({"type": "reasoning", "id": new_id("rs"), "status": "completed", "summary": [{"type": "summary_text", "text": e["text"]}]})
    elif kind == "tool_call":
        tool_call(e)
    elif kind == "tool_result":
        tool_result(e)
    elif kind == "status":
        u = e.get("usage") or {}
        if u:
            seen_usage = True
            usage["input"] += int(u.get("inputTokens", 0) or 0)
            usage["output"] += int(u.get("outputTokens", 0) or 0)
            usage["reasoning"] += int(u.get("reasoningTokens", 0) or 0)
            usage["cache_read"] += int(u.get("cacheReadTokens", 0) or 0)
            usage["cache_write"] += int(u.get("cacheWriteTokens", 0) or 0)
        if e.get("phase") == "turn_end" and e.get("reason") not in (None, "completed", "end_turn", "stop"):
            print(f"dsh turn ended: {e.get('reason')}", flush=True)
    elif kind == "final":
        if not had_text and e.get("text"):
            done({"type": "message", "id": new_id("msg"), "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": e["text"], "annotations": []}]})
    elif kind == "error":
        failed = True
        print(f"dsh error: {e.get('message', '')}", flush=True)


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
if seen_usage:
    emit({"type": "response.completed", "response": {"usage": {"input_tokens": usage["input"], "output_tokens": usage["output"], "total_tokens": usage["input"] + usage["output"],
          "input_tokens_details": {"cached_tokens": usage["cache_read"], "cache_write_tokens": usage["cache_write"]},
          "output_tokens_details": {"reasoning_tokens": usage["reasoning"]}}}})
sys.exit(1 if failed else 0)
