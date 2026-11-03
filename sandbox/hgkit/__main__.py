"""`python3 -m hgkit check [FILE]`: read a package's stdout (a file, or stdin) the way the platform reads it, and say
what it would refuse, naming the line and the field. Exit 0 when the output is valid."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 1 or argv[0] != "check" or any(a in ("-h", "--help") for a in argv):
        print("usage: python3 -m hgkit check [FILE]   (stdin when no file is given)", file=sys.stderr)
        return 2
    from .validate import OutputError, check, read_line
    try:
        text = Path(argv[1]).read_text() if len(argv) > 1 else sys.stdin.read()
    except OSError as e:
        print(f"cannot read {argv[1]}: {e.strerror}", file=sys.stderr)
        return 2
    for n, raw in enumerate(text.splitlines(), 1):               # the first bad line, with its number
        try:
            read_line(raw)
        except OutputError as e:
            print(f"line {n}: {e}\n  {raw[:300]}")
            return 1
    try:
        events, outcome = check(text)
    except OutputError as e:
        print(f"refused: {e}")
        return 1
    logs = sum(1 for raw in text.splitlines() if raw.strip() and read_line(raw).kind == "log")
    print(f"ok: {len(events)} events, {logs} log lines, {outcome.status} with {len(outcome.output)} output items")
    if outcome.metadata:
        print("metadata: " + json.dumps(outcome.metadata))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
