"""Claude Code's stream-json, mapped onto the OpenAI Responses stream.

  system init / result   -> the session id into $HG_STATE/session (what --resume takes next turn)
  assistant text         -> response.output_text.delta, then a message item
  assistant thinking     -> a reasoning item
  tool_use Bash          -> shell_call;  Write / Edit -> apply_patch_call;  mcp__s__t -> mcp_call;  other -> function_call
  user tool_result       -> the matching *_output item
  result                 -> usage on response.completed; a non-success result fails the turn (exit 1)
Everything else the CLI prints is passed through as a plain line, kept in the turn's log.
"""
import json
import os
import sys
from pathlib import Path

n = 0
text = ""                                            # the assistant text of the message being streamed
message_id = ""
calls: dict[str, tuple[str, str, str]] = {}          # tool_use id -> (item type, item id, tool name)
failed = False


def emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def new_id(prefix: str) -> str:
    global n
    n += 1
    return f"{prefix}_{n}"


def done(item: dict) -> None:
    emit({"type": "response.output_item.done", "item": item})


def close_message() -> None:
    global text, message_id
    if message_id:
        done({"type": "message", "id": message_id, "role": "assistant", "status": "completed",
              "content": [{"type": "output_text", "text": text, "annotations": []}]})
    text, message_id = "", ""


def remember(session_id: str) -> None:
    if session_id and os.environ.get("HG_STATE"):
        Path(os.environ["HG_STATE"], "session").write_text(session_id)


def tool_call(block: dict) -> None:
    name, args, cid = block.get("name", ""), block.get("input") or {}, block.get("id", new_id("call"))
    if name == "Bash":
        item = {"type": "shell_call", "id": new_id("sh"), "call_id": cid, "status": "completed", "action": {"commands": [args.get("command", "")]}}
    elif name in ("Write", "Edit"):
        op = ({"type": "create_file", "path": args.get("file_path", ""), "diff": args.get("content", "")} if name == "Write" else
              {"type": "update_file", "path": args.get("file_path", ""), "diff": f"-{args.get('old_string', '')}\n+{args.get('new_string', '')}"})
        item = {"type": "apply_patch_call", "id": new_id("ap"), "call_id": cid, "status": "completed", "operation": op}
    elif name.startswith("mcp__"):
        _, server, tool = (name.split("__", 2) + ["", ""])[:3]
        item = {"type": "mcp_call", "id": new_id("mcp"), "server_label": server, "name": tool, "arguments": json.dumps(args), "status": "in_progress"}
    else:
        item = {"type": "function_call", "id": new_id("fc"), "call_id": cid, "name": name, "arguments": json.dumps(args), "status": "completed"}
    calls[cid] = (item["type"], item["id"], name)
    done(item)


def tool_result(block: dict) -> None:
    cid, content = block.get("tool_use_id", ""), block.get("content")
    out = content if isinstance(content, str) else "".join(c.get("text", "") for c in content or [] if isinstance(c, dict))
    error = bool(block.get("is_error"))
    kind, item_id, name = calls.get(cid, ("function_call", "", ""))
    if kind == "shell_call":
        done({"type": "shell_call_output", "id": new_id("sho"), "call_id": cid, "status": "completed",
              "output": [{"stdout": out, "stderr": "", "outcome": {"type": "exit", "exit_code": 1 if error else 0}}]})
    elif kind == "apply_patch_call":
        done({"type": "apply_patch_call_output", "id": new_id("apo"), "call_id": cid, "status": "failed" if error else "completed", "output": out})
    elif kind == "mcp_call":
        _, server, tool = (name.split("__", 2) + ["", ""])[:3]
        done({"type": "mcp_call", "id": item_id, "server_label": server, "name": tool, "arguments": "{}", "status": "failed" if error else "completed",
              "output": None if error else out, "error": out if error else None})
    else:
        done({"type": "function_call_output", "id": new_id("fco"), "call_id": cid, "output": out, "status": "incomplete" if error else "completed"})


def handle(e: dict) -> None:
    global text, message_id, failed
    kind = e.get("type")
    if kind == "system" and e.get("subtype") == "init":
        remember(e.get("session_id", ""))
    elif kind in ("assistant", "user"):
        for block in e.get("message", {}).get("content", []) or []:
            bk = block.get("type")
            if kind == "assistant" and bk == "text" and block.get("text"):
                if not message_id:
                    message_id = new_id("msg")
                    emit({"type": "response.output_item.added", "item": {"type": "message", "id": message_id, "role": "assistant", "status": "in_progress", "content": []}})
                text += block["text"]
                emit({"type": "response.output_text.delta", "item_id": message_id, "output_index": 0, "content_index": 0, "delta": block["text"]})
                continue
            close_message()
            if kind == "assistant" and bk == "thinking" and block.get("thinking"):
                done({"type": "reasoning", "id": new_id("rs"), "status": "completed", "summary": [{"type": "summary_text", "text": block["thinking"]}]})
            elif kind == "assistant" and bk == "tool_use":
                tool_call(block)
            elif kind == "user" and bk == "tool_result":
                tool_result(block)
    elif kind == "result":
        close_message()
        remember(e.get("session_id", ""))
        if e.get("structured_output") is not None:
            done({"type": "message", "id": new_id("msg"), "role": "assistant", "status": "completed",
                  "content": [{"type": "output_text", "text": json.dumps(e["structured_output"]), "annotations": []}]})
        usage = e.get("usage") or {}
        inp, out = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        emit({"type": "response.completed", "response": {"usage": {
            "input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
            "input_tokens_details": {"cached_tokens": int(usage.get("cache_read_input_tokens", 0)), "cache_write_tokens": int(usage.get("cache_creation_input_tokens", 0))},
            "output_tokens_details": {"reasoning_tokens": 0}}}})
        if e.get("is_error") or e.get("subtype") != "success":
            failed = True
            print(f"claude ended with {e.get('subtype')}: {str(e.get('result') or '')[:500]}", flush=True)


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
close_message()
sys.exit(1 if failed else 0)
