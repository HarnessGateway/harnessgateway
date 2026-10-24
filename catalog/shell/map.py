"""One command's run as Responses items: shell_call, shell_call_output, and a message with its stdout.
Usage: map.py N EXIT_CODE SCRIPT STDOUT_FILE STDERR_FILE. Bodies are capped so a verbose command
still fits a response record."""
import json
import sys
from pathlib import Path

CAP = 256 * 1024

n, code, script, out_file, err_file = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
stdout = Path(out_file).read_text(errors="replace")[:CAP]
stderr = Path(err_file).read_text(errors="replace")[:CAP]


def emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


emit({"type": "response.output_item.done", "item": {"type": "shell_call", "id": f"sh_{n}", "call_id": f"call_{n}", "status": "completed",
                                                    "action": {"commands": [script]}}})
emit({"type": "response.output_item.done", "item": {"type": "shell_call_output", "id": f"sho_{n}", "call_id": f"call_{n}", "status": "completed",
                                                    "output": [{"stdout": stdout, "stderr": stderr, "outcome": {"type": "exit", "exit_code": code}}]}})
text = stdout if stdout.strip() else (stderr if stderr.strip() else f"(exit {code}, no output)")
emit({"type": "response.output_item.added", "item": {"type": "message", "id": f"msg_{n}", "role": "assistant", "status": "in_progress", "content": []}})
emit({"type": "response.output_text.delta", "item_id": f"msg_{n}", "output_index": 0, "content_index": 0, "delta": text})
emit({"type": "response.output_item.done", "item": {"type": "message", "id": f"msg_{n}", "role": "assistant", "status": "completed",
                                                    "content": [{"type": "output_text", "text": text, "annotations": []}]}})
