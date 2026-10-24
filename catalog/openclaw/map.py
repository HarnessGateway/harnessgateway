"""OpenClaw's `agent exec --json` envelope, mapped onto the OpenAI Responses stream. One object at
the end of the run: its payloads become the message, its usage the usage; a status other than ok
fails the turn. The tool summary carries counts only, so no tool items are produced."""
import json
import sys

raw = sys.stdin.read()
start = raw.find("{")
head = raw[:start] if start > 0 else ""
if head.strip():
    print(head.strip(), flush=True)
try:
    env = json.loads(raw[start:]) if start >= 0 else {}
except ValueError:
    env = {}
    print(raw[start:].strip(), flush=True)


def emit(e: dict) -> None:
    print(json.dumps(e, ensure_ascii=False), flush=True)


text = "\n\n".join(p.get("text", "") for p in env.get("payloads", []) or [] if isinstance(p, dict) and p.get("text"))
if text:
    emit({"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 0, "delta": text})
    emit({"type": "response.output_item.done", "item": {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                                                        "content": [{"type": "output_text", "text": text, "annotations": []}]}})
u = env.get("usage") or {}
inp, out = int(u.get("input", 0) or 0), int(u.get("output", 0) or 0)
emit({"type": "response.completed", "response": {"usage": {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
      "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}})
summary = env.get("toolSummary") or {}
if summary:
    print(f"tools: {json.dumps(summary)}", flush=True)
if env.get("status") not in (None, "ok") or env.get("ok") is False:
    err = env.get("error") or {}
    print(f"openclaw {env.get('status')}: {err.get('message') if isinstance(err, dict) else err}", flush=True)
    sys.exit(1)
