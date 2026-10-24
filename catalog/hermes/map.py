"""Hermes Agent's quiet chat output (chat -q -Q), mapped onto the OpenAI Responses stream: the answer
as one message; the `session_id:` line Hermes prints on stderr (given as the file in argv[1]) into
$HG_STATE/session for --resume; Hermes's tool review blocks (lines opened by ┊ and the diff under
them) kept as the log rather than the answer. Hermes reports no token usage this way."""
import json
import os
import re
import sys
from pathlib import Path

out = sys.stdin.buffer.read().decode("utf-8", "replace")          # all of it: Hermes has exited once stdin ends
err = Path(sys.argv[1]).read_text(errors="replace") if len(sys.argv) > 1 and Path(sys.argv[1]).is_file() else ""
if (m := re.search(r"session_id:\s*(\S+)", err)) and os.environ.get("HG_STATE"):
    Path(os.environ["HG_STATE"], "session").write_text(m.group(1))

answer, log, in_diff = [], [], False
for line in out.splitlines():
    bare = line.strip()
    if bare.startswith("\u250a"):                   # ┊ a tool review block begins
        in_diff = "diff" in line
        log.append(line)
        continue
    if in_diff and (re.match(r"^(a/|b/|@@|\+|-|\\ No newline)", bare) or " \u2192 " in bare):
        log.append(line)
        continue
    in_diff = False
    answer.append(line)
text = "\n".join(answer).strip()


def emit(e: dict) -> None:
    print(json.dumps(e, ensure_ascii=False), flush=True)


for line in log:
    print(line, flush=True)
if text:
    emit({"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 0, "delta": text})
    emit({"type": "response.output_item.done", "item": {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                                                        "content": [{"type": "output_text", "text": text, "annotations": []}]}})
