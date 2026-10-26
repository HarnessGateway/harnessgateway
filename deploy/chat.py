"""Talk to a harness like a local agent: your input at `<`, its reply streamed at `>`, with what it does in between.

    HG_KEY=sk-hg-... python deploy/chat.py [--model claude-code/deepseek/deepseek-flash] [--api http://localhost:8000]

Every message continues the same session (workspace, files, the agent's memory) until you start a new one.
Commands: /new (a fresh session), /model <harness/provider/model> (new session on another model), /exit.
Ctrl+C while the agent works cancels that turn; Ctrl+C or Ctrl+D at the prompt quits. Needs: pip install openai.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys

from openai import OpenAI

DIM, RED, CYAN, RESET = ("\033[2m", "\033[31m", "\033[36m", "\033[0m") if sys.stdout.isatty() else ("",) * 4
OUTPUT_LINES = 6
THINK = "✻"                                   # marks the agent's thinking


def out(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def clip(text: str, lines: int = OUTPUT_LINES) -> str:
    rows = text.rstrip("\n").splitlines()
    shown = "\n".join(rows[:lines])
    return shown + (f"\n… {len(rows) - lines} more lines" if len(rows) > lines else "")


def indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + row for row in text.splitlines())


class Turn:
    """Renders one turn's stream: text after `>`, reasoning dimmed, each tool use on its own lines."""

    def __init__(self) -> None:
        self.at_line_start = True
        self.in_text = False
        self.in_reasoning = False

    def _line(self, text: str) -> None:
        if not self.at_line_start:
            out("\n")
        out(text + "\n")
        self.at_line_start, self.in_text, self.in_reasoning = True, False, False

    def text(self, delta: str) -> None:
        if not self.in_text:
            if not self.at_line_start:
                out("\n")
            out(f"{CYAN}>{RESET} ")
            self.in_text, self.in_reasoning = True, False
        out(delta)
        self.at_line_start = delta.endswith("\n")

    def reasoning(self, delta: str) -> None:
        """Thinking, dimmed, marked with ✻ where a block starts and indented under it."""
        if not self.in_reasoning:
            if not self.at_line_start:
                out("\n")
            out(f"{DIM}{THINK} ")
            self.in_reasoning, self.in_text = True, False
        out(f"{DIM}{delta.replace(chr(10), chr(10) + '  ')}{RESET}")
        self.at_line_start = False

    def item(self, item: dict) -> None:
        kind = item.get("type")
        if kind == "shell_call":
            for command in (item.get("action") or {}).get("commands", []):
                self._line(f"{DIM}  $ {command}{RESET}")
        elif kind == "shell_call_output":
            for result in item.get("output") or []:
                text = (result.get("stdout") or "") + (result.get("stderr") or "")
                code = (result.get("outcome") or {}).get("exit_code")
                if text.strip():
                    self._line(f"{DIM}{indent(clip(text))}{RESET}")
                if code not in (0, None):
                    self._line(f"{DIM}    (exit {code}){RESET}")
        elif kind == "apply_patch_call":
            op = item.get("operation") or {}
            verb = {"create_file": "created", "update_file": "edited", "delete_file": "deleted"}.get(op.get("type"), op.get("type"))
            self._line(f"{DIM}  ✎ {verb} {op.get('path', '')}{RESET}")
        elif kind == "function_call":
            self._line(f"{DIM}  ⚙ {item.get('name')}({clip(item.get('arguments') or '', 1)}){RESET}")
        elif kind == "mcp_call":
            self._line(f"{DIM}  ⚙ {item.get('server_label')}.{item.get('name')}{RESET}")
        elif kind == "web_search_call":
            self._line(f"{DIM}  ⌕ web search{RESET}")

    def end(self, response) -> None:
        if not self.at_line_start:
            out("\n")
        if response.status == "failed" and response.error:
            reason = (response.metadata or {}).get("hg_error_code") or response.error.code
            out(f"{RED}! {reason}: {response.error.message}{RESET}\n")
        elif response.status != "completed":
            out(f"{DIM}({response.status}){RESET}\n")


def run_turn(client: OpenAI, model: str, text: str, previous: str | None) -> str | None:
    """One turn, streamed. Returns the response id to continue from (None when the turn could not start)."""
    turn, rid = Turn(), None
    kwargs = {"previous_response_id": previous} if previous else {}
    try:
        stream = client.responses.create(model=model, input=text, stream=True, **kwargs)
        for event in stream:
            kind = event.type
            if kind == "response.created":
                rid = event.response.id
            elif kind == "response.output_text.delta":
                turn.text(event.delta)
            elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                turn.reasoning(event.delta)
            elif kind == "response.output_item.done":
                turn.item(event.item.model_dump())
            elif kind in ("response.completed", "response.failed", "response.incomplete"):
                turn.end(event.response)
    except KeyboardInterrupt:
        if rid:
            with contextlib.suppress(Exception):
                client.responses.cancel(rid)
        out(f"\n{DIM}(cancelled){RESET}\n")
    except Exception as e:                              # a refused request (bad model, no provider, ...)
        out(f"{RED}! {getattr(e, 'message', None) or e}{RESET}\n")
        return previous
    return rid or previous


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=os.environ.get("HG_MODEL", "claude-code/deepseek/deepseek-flash"))
    ap.add_argument("--api", default=os.environ.get("HG_API", "http://localhost:8000"))
    ap.add_argument("--key", default=os.environ.get("HG_KEY", ""))
    args = ap.parse_args()
    if not args.key:
        sys.exit("set HG_KEY (or pass --key) to your organization's sk-hg-... key")
    with contextlib.suppress(ImportError):
        import readline  # noqa: F401  (line editing and history at the prompt)
    client = OpenAI(base_url=f"{args.api.rstrip('/')}/v1", api_key=args.key, timeout=3600, max_retries=0)
    model, previous = args.model, None
    out(f"{DIM}{model} at {args.api} · /new, /model <name>, /exit · Ctrl+C stops a turn{RESET}\n")
    while True:
        try:
            text = input(f"\n{CYAN}<{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            out("\n")
            return
        if not text:
            continue
        if text in ("/exit", "/quit"):
            return
        if text == "/new":
            previous = None
            out(f"{DIM}(new session){RESET}\n")
            continue
        if text.startswith("/model"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2:
                model, previous = parts[1], None
            out(f"{DIM}({model}{', new session' if len(parts) == 2 else ''}){RESET}\n")
            continue
        previous = run_turn(client, model, text, previous)


if __name__ == "__main__":
    main()
