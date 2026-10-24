"""Oh My Pi's --mode json events (Pi's protocol, which omp shares), mapped onto the OpenAI Responses stream.

  message_update.assistantMessageEvent text_delta / thinking_delta  -> output_text deltas; thinking gathered into a reasoning item
  tool_execution_start / tool_execution_end -> shell_call (bash), apply_patch_call (edit, write), mcp_call (mcp__*), function_call (the rest), each with its output
  message_end (assistant)  -> the message item closed; usage on response.completed
  agent_end / error        -> an error fails the turn (exit 1)
"""
import json
import os
import sys
from pathlib import Path

n = 0
text = ""
thinking = ""
message_id = ""
calls: dict[str, tuple[str, str]] = {}               # toolCallId -> (item kind, tool name)
usage = None
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
    low = name.lower()
    if low.startswith("mcp__"):
        return "mcp_call"
    if low in ("bash", "powershell", "shell"):
        return "shell_call"
    if low in ("edit", "write", "apply_patch"):
        return "apply_patch_call"
    return "function_call"


def tool_start(e: dict) -> None:
    close_message()
    name, args, cid = e.get("toolName", ""), e.get("args") or {}, e.get("toolCallId") or new_id("call")
    kind = classify(name)
    calls[cid] = (kind, name)
    if kind == "shell_call":
        done({"type": "shell_call", "id": new_id("sh"), "call_id": cid, "status": "completed", "action": {"commands": [str(args.get("command", ""))]}})
    elif kind == "apply_patch_call":
        op = {"type": "create_file" if name.lower() == "write" else "update_file", "path": str(args.get("path", "")),
              "diff": str(args.get("content", "")) if name.lower() == "write" else f"-{args.get('oldText', '')}\n+{args.get('newText', '')}"}
        done({"type": "apply_patch_call", "id": new_id("ap"), "call_id": cid, "status": "completed", "operation": op})
    elif kind == "mcp_call":
        _, server, tool = (name.split("__", 2) + ["", ""])[:3]
        done({"type": "mcp_call", "id": cid, "server_label": server, "name": tool, "arguments": json.dumps(args), "status": "in_progress"})
    else:
        done({"type": "function_call", "id": new_id("fc"), "call_id": cid, "name": name, "arguments": json.dumps(args), "status": "completed"})


def tool_end(e: dict) -> None:
    cid = e.get("toolCallId") or ""
    kind, name = calls.get(cid, ("function_call", e.get("toolName", "")))
    result = e.get("result") or {}
    content = result.get("content") if isinstance(result, dict) else result
    out = "".join(c.get("text", "") for c in content if isinstance(c, dict)) if isinstance(content, list) else str(content or "")
    error = bool(e.get("isError"))
    if kind == "shell_call":
        details = result.get("details") if isinstance(result, dict) else {}
        code = details.get("exitCode") if isinstance(details, dict) else None
        done({"type": "shell_call_output", "id": new_id("sho"), "call_id": cid, "status": "completed",
              "output": [{"stdout": out, "stderr": "", "outcome": {"type": "exit", "exit_code": int(code) if isinstance(code, int) else (1 if error else 0)}}]})
    elif kind == "apply_patch_call":
        done({"type": "apply_patch_call_output", "id": new_id("apo"), "call_id": cid, "status": "failed" if error else "completed", "output": out})
    elif kind == "mcp_call":
        _, server, tool = (name.split("__", 2) + ["", ""])[:3]
        done({"type": "mcp_call", "id": cid, "server_label": server, "name": tool, "arguments": "{}", "status": "failed" if error else "completed",
              "output": None if error else out, "error": out if error else None})
    else:
        done({"type": "function_call_output", "id": new_id("fco"), "call_id": cid, "output": out, "status": "incomplete" if error else "completed"})


def handle(e: dict) -> None:
    global text, thinking, message_id, usage, failed
    kind = e.get("type")
    if kind == "session" and e.get("id") and os.environ.get("HG_STATE"):
        Path(os.environ["HG_STATE"], "session").write_text(str(e["id"]))
    elif kind == "message_update":
        ev = e.get("assistantMessageEvent") or {}
        if ev.get("type") == "text_delta" and ev.get("delta"):
            if not message_id:
                message_id = new_id("msg")
                emit({"type": "response.output_item.added", "item": {"type": "message", "id": message_id, "role": "assistant", "status": "in_progress", "content": []}})
            text += ev["delta"]
            emit({"type": "response.output_text.delta", "item_id": message_id, "output_index": 0, "content_index": 0, "delta": ev["delta"]})
        elif ev.get("type") == "thinking_delta" and ev.get("delta"):
            thinking += ev["delta"]
        elif ev.get("type") == "error":
            failed = True
            print(f"pi error: {ev.get('error') or ev.get('reason')}", flush=True)
    elif kind == "message_end":
        m = e.get("message") or {}
        if m.get("role") == "assistant":
            if m.get("usage"):
                usage = m["usage"]
            if m.get("stopReason") == "error":
                failed = True
                print(f"pi: the model call ended in error: {m.get('errorMessage', '')}", flush=True)
            close_message()
    elif kind == "tool_execution_start":
        tool_start(e)
    elif kind == "tool_execution_end":
        tool_end(e)
    elif kind in ("agent_end", "agent_settled"):
        close_message()


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
if usage:
    inp, out = int(usage.get("input", 0) or 0), int(usage.get("output", 0) or 0)
    emit({"type": "response.completed", "response": {"usage": {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
          "input_tokens_details": {"cached_tokens": int(usage.get("cacheRead", 0) or 0), "cache_write_tokens": int(usage.get("cacheWrite", 0) or 0)},
          "output_tokens_details": {"reasoning_tokens": int(usage.get("reasoning", 0) or 0)}}}})
sys.exit(1 if failed else 0)
