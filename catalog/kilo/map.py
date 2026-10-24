"""Kilo Code's `run --format json` events, mapped onto the OpenAI Responses stream.

  every event's sessionID -> $HG_STATE/session (what -s takes next turn)
  text part        -> a message (the part's text as one delta, then the item)
  reasoning part   -> a reasoning item (emitted by Kilo only with --thinking)
  tool_use part    -> shell_call (bash), apply_patch_call (edit, write), mcp_call (<server>_<tool> from MCP), function_call, each with the part's output
  step_finish part -> usage, summed over steps, on response.completed
  error            -> the turn fails (exit 1)
"""
import json
import os
import sys
from pathlib import Path

n = 0
usage = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
seen_usage = False
failed = False
remembered = False


def emit(e: dict) -> None:
    print(json.dumps(e, ensure_ascii=False), flush=True)


def new_id(p: str) -> str:
    global n
    n += 1
    return f"{p}_{n}"


def done(item: dict) -> None:
    emit({"type": "response.output_item.done", "item": item})


def tool(part: dict) -> None:
    name = str(part.get("tool", ""))
    state = part.get("state") or {}
    args, out = state.get("input") or {}, state.get("output")
    out = out if isinstance(out, str) else json.dumps(out) if out is not None else ""
    error = state.get("status") == "error"
    cid = str(part.get("callID") or new_id("call"))
    if name == "bash":
        done({"type": "shell_call", "id": new_id("sh"), "call_id": cid, "status": "completed", "action": {"commands": [str(args.get("command", ""))]}})
        done({"type": "shell_call_output", "id": new_id("sho"), "call_id": cid, "status": "completed",
              "output": [{"stdout": out, "stderr": str(state.get("error") or ""), "outcome": {"type": "exit", "exit_code": 1 if error else 0}}]})
    elif name in ("edit", "write", "apply_patch"):
        op = {"type": "create_file" if name == "write" else "update_file", "path": str(args.get("filePath", args.get("path", ""))),
              "diff": str(args.get("content", "")) if name == "write" else f"-{args.get('oldString', '')}\n+{args.get('newString', '')}"}
        done({"type": "apply_patch_call", "id": new_id("ap"), "call_id": cid, "status": "completed", "operation": op})
        done({"type": "apply_patch_call_output", "id": new_id("apo"), "call_id": cid, "status": "failed" if error else "completed", "output": str(state.get("error") or out)})
    elif "_" in name and name.split("_", 1)[0] not in ("todo", "web", "read", "list", "task"):
        server, tname = name.split("_", 1)
        done({"type": "mcp_call", "id": new_id("mcp"), "server_label": server, "name": tname, "arguments": json.dumps(args), "status": "failed" if error else "completed",
              "output": None if error else out, "error": str(state.get("error")) if error else None})
    else:
        done({"type": "function_call", "id": new_id("fc"), "call_id": cid, "name": name, "arguments": json.dumps(args), "status": "completed"})
        done({"type": "function_call_output", "id": new_id("fco"), "call_id": cid, "output": str(state.get("error") or out), "status": "incomplete" if error else "completed"})


def handle(e: dict) -> None:
    global failed, seen_usage, remembered
    kind, part = e.get("type"), e.get("part") or {}
    if e.get("sessionID") and not remembered and os.environ.get("HG_STATE"):
        Path(os.environ["HG_STATE"], "session").write_text(str(e["sessionID"]))
        remembered = True
    if kind == "text" and part.get("text"):
        mid = new_id("msg")
        emit({"type": "response.output_text.delta", "item_id": mid, "output_index": 0, "content_index": 0, "delta": part["text"]})
        done({"type": "message", "id": mid, "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": part["text"], "annotations": []}]})
    elif kind == "reasoning" and part.get("text"):
        done({"type": "reasoning", "id": new_id("rs"), "status": "completed", "summary": [{"type": "summary_text", "text": part["text"]}]})
    elif kind == "tool_use":
        tool(part)
    elif kind == "step_finish":
        t = part.get("tokens") or {}
        if t:
            seen_usage = True
            usage["input"] += int(t.get("input", 0) or 0)
            usage["output"] += int(t.get("output", 0) or 0)
            usage["reasoning"] += int(t.get("reasoning", 0) or 0)
            cache = t.get("cache") or {}
            usage["cache_read"] += int(cache.get("read", 0) or 0)
            usage["cache_write"] += int(cache.get("write", 0) or 0)
    elif kind == "error":
        failed = True
        err = e.get("error") or {}
        print(f"kilo error: {err.get('name', '')} {(err.get('data') or {}).get('message', '')}", flush=True)


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
