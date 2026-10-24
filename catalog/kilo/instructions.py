"""The request's `instructions` into AGENTS.md as one replaceable section, so a standing AGENTS.md
the package seeded (or the repository holds) keeps its own text and the request's part changes
with every turn. Harnesses that read AGENTS.md pick it up; a CLI with a system-prompt flag can take
the text that way instead (request.sh decides)."""
import sys
from pathlib import Path

BEGIN, END = "<!-- hg:instructions -->", "<!-- /hg:instructions -->"
text = sys.argv[1].strip() if len(sys.argv) > 1 else ""
path = Path("AGENTS.md")
current = path.read_text() if path.is_file() else ""
if BEGIN in current and END in current:
    head, rest = current.split(BEGIN, 1)
    current = head.rstrip("\n") + rest.split(END, 1)[1].lstrip("\n")
    current = current.strip("\n") + ("\n" if current.strip() else "")
if text:
    section = f"{BEGIN}\n## Instructions for this request\n\n{text}\n{END}\n"
    current = (current.rstrip("\n") + "\n\n" if current.strip() else "") + section
if current.strip():
    path.write_text(current)
elif path.is_file():
    path.unlink()
