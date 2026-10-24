#!/usr/bin/env python3
"""The filter: foobar's native events in on stdin, Responses output items out on stdout, one JSON
object per line, as they happen. Two kinds of line:

    {"item": {…}}      a finished output item, in the Responses schema's own item types
    {"delta": "…"}     a piece of the assistant's message text

The item types are the standard ones, nothing of ours: `reasoning` for what the agent thought,
`apply_patch_call` and `apply_patch_call_output` for a file it wrote, `shell_call` and
`shell_call_output` for a command it ran (stdout, stderr, exit code), `mcp_call` for an MCP tool
with its output, and `function_call` with `function_call_output` for anything else. The template
validates every item against the schema and refuses the turn if one does not fit. A package for
another CLI replaces this file and run.sh, nothing else.
"""
import json
import sys


def out(item):
    print(json.dumps({"item": item}), flush=True)


calls = {}
n = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except ValueError:
        continue                                     # a stray print from the CLI: not an event
    n += 1
    kind, cid = ev.get("ev"), f"call_{ev.get('id', n)}"
    if kind == "think":
        out({"type": "reasoning", "id": f"rs_{n}", "summary": [{"type": "summary_text", "text": ev["text"]}], "status": "completed"})
    elif kind == "tool":
        calls[cid] = ev
        if ev["name"] == "write_file":
            out({"type": "apply_patch_call", "id": f"apc_{n}", "call_id": cid, "status": "completed",
                 "operation": {"type": "create_file", "path": ev["args"]["path"], "diff": ev["args"]["content"]}})
        elif ev["name"] == "run":
            out({"type": "shell_call", "id": f"sh_{n}", "call_id": cid, "status": "completed", "action": {"commands": [ev["args"]["cmd"]]}})
        else:
            out({"type": "function_call", "id": f"fc_{n}", "call_id": cid, "name": ev["name"], "arguments": json.dumps(ev["args"]), "status": "completed"})
    elif kind == "tool_result":
        call = calls.get(cid, {})
        if call.get("name") == "write_file":
            out({"type": "apply_patch_call_output", "id": f"apco_{n}", "call_id": cid, "status": "completed" if ev["ok"] else "failed", "output": ev["out"]})
        elif call.get("name") == "run":
            out({"type": "shell_call_output", "id": f"sho_{n}", "call_id": cid, "status": "completed",
                 "output": [{"stdout": ev.get("out", ""), "stderr": ev.get("err", ""), "outcome": {"type": "exit", "exit_code": ev.get("code", 0)}}]})
        else:
            out({"type": "function_call_output", "id": f"fco_{n}", "call_id": cid, "output": ev["out"], "status": "completed" if ev["ok"] else "incomplete"})
    elif kind == "mcp":
        out({"type": "mcp_call", "id": f"mcp_{n}", "server_label": ev["server"], "name": ev["tool"], "arguments": json.dumps(ev["args"]),
             "output": ev["out"], "error": None, "status": "completed"})
    elif kind == "say":
        print(json.dumps({"delta": ev["delta"]}), flush=True)
