"""The CLI's output, mapped onto the OpenAI Responses stream. Reads the CLI's JSON lines on stdin and
prints Responses stream events on stdout, one per line; the platform relays them and builds the
final response from the `response.output_item.done` events (and the usage of `response.completed`).

Replace `translate` for a real CLI. The helpers below make the standard events; the item shapes
are the SDK's (openai.types.responses), validated by the platform before a client sees them.
"""
import json
import sys

n = 0


def emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def item_id(prefix: str) -> str:
    global n
    n += 1
    return f"{prefix}_{n}"


def message(text: str) -> None:
    """One assistant message: its text streamed as deltas, then the completed item."""
    mid = item_id("msg")
    emit({"type": "response.output_item.added", "item": {"type": "message", "id": mid, "role": "assistant", "status": "in_progress", "content": []}})
    emit({"type": "response.output_text.delta", "item_id": mid, "output_index": 0, "content_index": 0, "delta": text})
    emit({"type": "response.output_item.done", "item": {"type": "message", "id": mid, "role": "assistant", "status": "completed",
                                                        "content": [{"type": "output_text", "text": text, "annotations": []}]}})


def shell(command: str, output: str, exit_code: int = 0) -> None:
    """A command the agent ran, as a shell_call and its shell_call_output."""
    cid = item_id("call")
    emit({"type": "response.output_item.done", "item": {"type": "shell_call", "id": item_id("sh"), "call_id": cid, "status": "completed",
                                                        "action": {"commands": [command]}}})
    emit({"type": "response.output_item.done", "item": {"type": "shell_call_output", "id": item_id("sho"), "call_id": cid, "status": "completed",
                                                        "output": [{"stdout": output, "stderr": "", "outcome": {"type": "exit", "exit_code": exit_code}}]}})


def write(path: str, content: str) -> None:
    """A file the agent wrote, as an apply_patch_call and its output."""
    cid = item_id("call")
    emit({"type": "response.output_item.done", "item": {"type": "apply_patch_call", "id": item_id("ap"), "call_id": cid, "status": "completed",
                                                        "operation": {"type": "create_file", "path": path, "diff": content}}})
    emit({"type": "response.output_item.done", "item": {"type": "apply_patch_call_output", "id": item_id("apo"), "call_id": cid, "status": "completed", "output": "ok"}})


def reasoning(text: str) -> None:
    emit({"type": "response.output_item.done", "item": {"type": "reasoning", "id": item_id("rs"), "status": "completed",
                                                        "summary": [{"type": "summary_text", "text": text}]}})


def completed(input_tokens: int, output_tokens: int, cached: int = 0, reasoning_tokens: int = 0) -> None:
    """The usage: the platform takes it from a response.completed event."""
    emit({"type": "response.completed", "response": {"usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                                                                "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": 0},
                                                                "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
                                                                "total_tokens": input_tokens + output_tokens}}})


def translate(line: str) -> None:
    """echo-cli's lines. A real CLI's JSON mode has its own vocabulary; this is where it is read."""
    try:
        e = json.loads(line)
    except ValueError:
        print(line, flush=True)                  # not JSON: a plain line is kept as the turn's log
        return
    kind = e.get("kind")
    if kind == "text":
        message(e["text"])
    elif kind == "tool" and e.get("name") == "shell":
        shell(e["command"], e.get("output", ""))
    elif kind == "tool" and e.get("name") == "write":
        write(e["path"], e.get("content", ""))
    elif kind == "done":
        completed(int(e.get("input_tokens", 0)), int(e.get("output_tokens", 0)))


for raw in sys.stdin:
    if raw.strip():
        translate(raw.rstrip("\n"))
