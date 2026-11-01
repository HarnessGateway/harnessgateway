#!/usr/bin/env python3
"""Check a mapper's stdout offline: every line that is JSON with "type": "response.…" is an event, text deltas carry
logprobs, items carry their required fields, usage carries its details, and exactly one terminal event comes last.

    python3 check_events.py mapper-stdout.txt        (or on stdin)

Prints what is wrong with the first bad line and exits 1; otherwise a one-line summary and exit 0. This is the shape
the platform checks with the OpenAI SDK's own models; passing here is necessary, not sufficient."""
import json
import pathlib
import sys

TERMINAL = {"response.completed", "response.incomplete", "response.failed"}
ITEM_FIELDS = {
    "message": {"id", "type", "role", "status", "content"},
    "reasoning": {"id", "type", "summary"},
    "shell_call": {"id", "type", "call_id", "status", "action"},
    "shell_call_output": {"id", "type", "call_id", "status", "output"},
    "apply_patch_call": {"id", "type", "call_id", "status", "operation"},
    "apply_patch_call_output": {"id", "type", "call_id", "status"},
    "function_call": {"type", "call_id", "name", "arguments"},
    "function_call_output": {"type", "call_id", "output"},
    "mcp_call": {"id", "type", "server_label", "name", "arguments"},
    "web_search_call": {"id", "type", "status", "action"},
}


def fail(n: int, why: str, line: str) -> None:
    print(f"line {n}: {why}\n  {line[:300]}")
    sys.exit(1)


def main() -> None:
    text = pathlib.Path(sys.argv[1]).read_text() if len(sys.argv) > 1 else sys.stdin.read()
    events = terminal = logs = 0
    ended = False
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            logs += 1
            continue
        if not isinstance(e, dict) or not str(e.get("type", "")).startswith("response."):
            logs += 1
            continue
        if ended:
            fail(n, "an event after the terminal event", line)
        events += 1
        t = e["type"]
        if "sequence_number" in e:
            fail(n, "sequence_number is the platform's to assign", line)
        if t == "response.output_text.delta":
            if "logprobs" not in e or not isinstance(e.get("delta"), str):
                fail(n, "a text delta needs delta and logprobs: []", line)
            for k in ("item_id", "output_index", "content_index"):
                if k not in e:
                    fail(n, f"a text delta needs {k}", line)
        elif t in ("response.output_item.added", "response.output_item.done"):
            item = e.get("item")
            if not isinstance(item, dict) or "output_index" not in e:
                fail(n, "needs item and output_index", line)
            kind = item.get("type")
            need = ITEM_FIELDS.get(kind)
            if need is None:
                fail(n, f"unknown item type {kind!r}", line)
            missing = sorted(need - set(item))
            if missing:
                fail(n, f"{kind} item missing {', '.join(missing)}", line)
            if kind == "mcp_call" and isinstance(item.get("error"), str):
                fail(n, "mcp_call.error is an object, not a string", line)
        elif t in TERMINAL:
            terminal += 1
            ended = True
            r = e.get("response")
            if not isinstance(r, dict):
                fail(n, "a terminal event carries response", line)
            u = r.get("usage")
            if u is not None:
                for k in ("input_tokens", "output_tokens", "total_tokens"):
                    if not isinstance(u.get(k), int):
                        fail(n, f"usage.{k} is an integer", line)
                if "cached_tokens" not in (u.get("input_tokens_details") or {}) or \
                        "reasoning_tokens" not in (u.get("output_tokens_details") or {}):
                    fail(n, "usage needs input_tokens_details.cached_tokens and output_tokens_details.reasoning_tokens", line)
            if t == "response.failed" and not (r.get("error") or {}).get("message"):
                fail(n, "response.failed needs error.message", line)
            if t == "response.incomplete" and not (r.get("incomplete_details") or {}).get("reason"):
                fail(n, "response.incomplete needs incomplete_details.reason", line)
            for k, v in (r.get("metadata") or {}).items():
                if not isinstance(v, str):
                    fail(n, f"metadata.{k} is a string", line)
    if terminal != 1:
        print(f"{terminal} terminal events: exactly one, last, is required")
        sys.exit(1)
    print(f"ok: {events} events, {logs} log lines, one terminal event")


if __name__ == "__main__":
    main()
