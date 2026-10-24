"""Codex's `exec --json` events, mapped onto the OpenAI Responses stream.

  thread.started            -> the thread id into $HG_STATE/thread (what `exec resume` takes next turn)
  item.completed agent_message  -> a message (its text as one delta, then the item)
  item.completed reasoning      -> a reasoning item
  item.completed command_execution -> shell_call + shell_call_output (the command, its output, its exit code)
  item.completed file_change    -> an apply_patch_call (+ output) per changed path
  item.completed mcp_tool_call  -> mcp_call;  web_search -> web_search_call;  todo_list, collab_tool_call -> function_call
  turn.completed            -> usage on response.completed;  turn.failed / error -> the turn fails (exit 1)
"""
import json
import os
import sys
from pathlib import Path

n = 0
failed = False


def emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def new_id(prefix: str) -> str:
    global n
    n += 1
    return f"{prefix}_{n}"


def done(item: dict) -> None:
    emit({"type": "response.output_item.done", "item": item})


def completed_item(item: dict) -> None:
    kind = item.get("type")
    if kind == "agent_message":
        mid = new_id("msg")
        emit({"type": "response.output_text.delta", "item_id": mid, "output_index": 0, "content_index": 0, "delta": item.get("text", "")})
        done({"type": "message", "id": mid, "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": item.get("text", ""), "annotations": []}]})
    elif kind == "reasoning":
        done({"type": "reasoning", "id": new_id("rs"), "status": "completed", "summary": [{"type": "summary_text", "text": item.get("text", "")}]})
    elif kind == "command_execution":
        cid = new_id("call")
        status = item.get("status", "completed")
        done({"type": "shell_call", "id": new_id("sh"), "call_id": cid, "status": "completed" if status in ("completed", "failed") else "incomplete",
              "action": {"commands": [item.get("command", "")]}})
        code = item.get("exit_code")
        outcome = {"type": "exit", "exit_code": int(code)} if isinstance(code, int) else {"type": "timeout"}
        done({"type": "shell_call_output", "id": new_id("sho"), "call_id": cid, "status": "completed",
              "output": [{"stdout": item.get("aggregated_output", "") or "", "stderr": "", "outcome": outcome}]})
    elif kind == "file_change":
        for change in item.get("changes", []) or []:
            cid = new_id("call")
            op = {"add": "create_file", "update": "update_file", "delete": "delete_file"}.get(change.get("kind", "update"), "update_file")
            operation = {"type": op, "path": change.get("path", "")}
            if op != "delete_file":
                operation["diff"] = change.get("diff", "") or ""
            done({"type": "apply_patch_call", "id": new_id("ap"), "call_id": cid, "status": "completed", "operation": operation})
            done({"type": "apply_patch_call_output", "id": new_id("apo"), "call_id": cid, "status": "failed" if item.get("status") == "failed" else "completed", "output": item.get("status", "completed")})
    elif kind == "mcp_tool_call":
        error = item.get("error")
        result = item.get("result")
        out = json.dumps(result.get("content") if isinstance(result, dict) and "content" in result else result) if result is not None else None
        done({"type": "mcp_call", "id": new_id("mcp"), "server_label": item.get("server", ""), "name": item.get("tool", ""),
              "arguments": json.dumps(item.get("arguments", {})), "status": "failed" if error else "completed",
              "output": out, "error": error.get("message") if isinstance(error, dict) else (str(error) if error else None)})
    elif kind == "web_search":
        done({"type": "web_search_call", "id": new_id("ws"), "status": "completed", "action": {"type": "search", "query": item.get("query", "")}})
    elif kind in ("todo_list", "collab_tool_call"):
        done({"type": "function_call", "id": new_id("fc"), "call_id": new_id("call"), "name": kind, "arguments": json.dumps({k: v for k, v in item.items() if k not in ("id", "type")}), "status": "completed"})
    elif kind == "error":
        print(f"codex: {item.get('message', '')}", flush=True)


def handle(e: dict) -> None:
    global failed
    kind = e.get("type")
    if kind == "thread.started" and e.get("thread_id") and os.environ.get("HG_STATE"):
        Path(os.environ["HG_STATE"], "thread").write_text(e["thread_id"])
    elif kind == "item.completed" and isinstance(e.get("item"), dict):
        completed_item(e["item"])
    elif kind == "turn.completed":
        u = e.get("usage") or {}
        inp, out = int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
        emit({"type": "response.completed", "response": {"usage": {
            "input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
            "input_tokens_details": {"cached_tokens": int(u.get("cached_input_tokens", 0)), "cache_write_tokens": int(u.get("cache_write_input_tokens", 0))},
            "output_tokens_details": {"reasoning_tokens": int(u.get("reasoning_output_tokens", 0))}}}})
    elif kind in ("turn.failed", "error"):
        failed = True
        err = e.get("error") if isinstance(e.get("error"), dict) else {}
        print(f"codex {kind}: {err.get('message') or e.get('message', '')}", flush=True)


for raw in sys.stdin:
    line = raw.rstrip("\n")
    if not line.strip():
        continue
    try:
        ev = json.loads(line)
    except ValueError:
        print(line, flush=True)
        continue
    if isinstance(ev, dict):
        handle(ev)
    else:
        print(line, flush=True)
sys.exit(1 if failed else 0)
