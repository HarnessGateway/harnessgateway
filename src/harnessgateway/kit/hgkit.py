"""hgkit: the mapping between an OpenAI Responses request and an agent CLI, for a harness package's `run`.

A package's `run` is called once a turn. It reads the request, starts its CLI, prints what the CLI does as
Responses stream events while it works, prints one terminal line, and exits 0. This file gives it:

    Request     the request, as the caller sent it, and what else the platform wrote for this turn
    State       small named values that stay with the session: a CLI's own session id, to resume it
    Cli         the CLI as a process: its output a line at a time, and stopped when the turn is
    Emitter     prints the lines the platform accepts

It needs Python 3.9 or later and nothing outside the standard library. Using it is optional: a package in any
language can read the same files and print the same lines.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from typing import IO, Any

__all__ = ["Cli", "Emitter", "Request", "State"]

_STATE_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*")


def _read(*path: str) -> str:
    with open(os.path.join(*path), encoding="utf-8") as file:
        return file.read()


class Request:
    """This turn's inputs: the Responses request, and what the platform put beside it."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self._turn = env["HG_TURN"]
        self.body: dict[str, Any] = json.loads(_read(self._turn, "request.json"))        # as it was sent
        self.history: list[dict[str, Any]] = json.loads(_read(self._turn, "history.json"))    # turns not yet seen
        self.handover = _read(self._turn, "handover.txt").strip()     # who worked on the session since, if anyone
        self.model = env.get("HG_MODEL", "")
        self.provider_api = env.get("HG_PROVIDER_API", "")            # the API shape to speak: anthropic, …
        self.provider_url = env.get("HG_PROVIDER_URL", "")
        self.provider_key = env.get("HG_PROVIDER_KEY", "")            # a placeholder: the real key never is here

    @property
    def prompt(self) -> str:
        """The text the caller sent: a string input as it is, or the text of a list input's user messages."""
        given = self.body.get("input")
        if isinstance(given, str):
            return given
        texts = []
        for item in given if isinstance(given, list) else []:
            if isinstance(item, dict) and item.get("role") == "user":
                content = item.get("content")
                texts += [content] if isinstance(content, str) else [
                    part["text"] for part in content or []
                    if isinstance(part, dict) and part.get("type") == "input_text"]
        return "\n\n".join(texts)

    @property
    def instructions(self) -> str:
        value = self.body.get("instructions")
        return value if isinstance(value, str) else ""

    @property
    def effort(self) -> str:
        """The reasoning effort the caller asked for, or nothing."""
        reasoning = self.body.get("reasoning")
        return (reasoning.get("effort") or "") if isinstance(reasoning, dict) else ""

    @property
    def mcp_servers(self) -> list[dict[str, Any]]:
        """The MCP servers the request names, each as the Responses API describes one."""
        return [tool for tool in self.body.get("tools") or [] if isinstance(tool, dict) and tool.get("type") == "mcp"]

    @property
    def files(self) -> list[str]:
        """Where each uploaded file the request names is, in the order of their ids."""
        folder = os.path.join(self._turn, "files")
        ids = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
        return [os.path.join(folder, file_id, name) for file_id in ids
                for name in sorted(os.listdir(os.path.join(folder, file_id)))]

    def brief(self, *, instructions: bool = True) -> str:
        """What the agent is asked this turn, as one text: the handover line, when another harness worked on the
        session last; the request's instructions (pass instructions=False when the CLI takes them some other
        way); where the request's files are; then the prompt."""
        parts = [self.handover,
                 f"Instructions for this request:\n{self.instructions}" if instructions and self.instructions else "",
                 "Files given with this request: " + ", ".join(self.files) if self.files else "",
                 self.prompt]
        return "\n\n".join(part for part in parts if part)


class State:
    """Small named values kept with the session, for this harness alone: what a CLI needs to pick up where it
    left off. They are files under the session's mind, so they are saved and restored with it."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._folder = (os.environ if env is None else env)["HG_STATE"]

    def _path(self, name: str) -> str:
        if not _STATE_NAME.fullmatch(name):
            raise ValueError(f"a state name is letters, digits, '.', '_' and '-': {name!r}")
        return os.path.join(self._folder, name)

    def get(self, name: str) -> str | None:
        try:
            return _read(self._path(name))
        except FileNotFoundError:
            return None

    def set(self, name: str, value: str) -> None:
        os.makedirs(self._folder, exist_ok=True)
        with open(self._path(name), "w", encoding="utf-8") as file:
            file.write(value)

    def clear(self, name: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.remove(self._path(name))


class Cli:
    """The harness's CLI as a process: its standard output, one line at a time. Its standard error is the
    turn's log.

    When the platform stops the turn, `run` is sent a termination signal. The CLI and everything it started are
    sent it too; when they have ended, `run` ends there, with exit code 0. Nothing after `lines()` is reached,
    so a stopped turn prints no terminal line. What must be kept for the next turn (the CLI's session id) is
    saved the moment it is known.
    """

    def __init__(self, command: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: str | None = None,
                 stdin: str | None = None) -> None:
        self._command = list(command)
        self._env = dict(os.environ if env is None else env)
        self._cwd = cwd
        self._stdin = stdin
        self._stopped = False
        self.exit_code: int | None = None

    def lines(self) -> Iterator[str]:
        process = subprocess.Popen(
            self._command, env=self._env, cwd=self._cwd, stdout=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.PIPE if self._stdin is not None else subprocess.DEVNULL,
            start_new_session=True)                # a group of its own, so that all of it can be told to stop

        def stop(*_: Any) -> None:
            self._stopped = True
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        # Signals are handled in the main thread only, which is where a `run` reads its CLI.
        handled = threading.current_thread() is threading.main_thread()
        before = signal.signal(signal.SIGTERM, stop) if handled else None
        if self._stdin is not None and process.stdin is not None:
            process.stdin.write(self._stdin)
            process.stdin.close()
        assert process.stdout is not None
        for line in process.stdout:
            yield line.rstrip("\n")
        self.exit_code = process.wait()
        if handled:
            signal.signal(signal.SIGTERM, before)
        if self._stopped:
            raise SystemExit(0)


class Emitter:
    """Prints a turn's output: Responses stream events, lines for the log, and one terminal line.

    Text and reasoning arrive in pieces and are gathered into one item, which ends when something of another kind
    is printed. Everything else is printed whole.
    """

    def __init__(self, out: IO[str] | None = None) -> None:
        self._out = sys.stdout if out is None else out
        self._index = 0                            # the next item's place in the response's output
        self._open: dict[str, str] | None = None   # the text or reasoning item being gathered: kind, id, text
        self._ended = False
        self._lock = threading.Lock()              # a `run` may print from more than one thread

    # what the agent says

    def say(self, text: str) -> None:
        """A piece of the agent's answer."""
        self._piece("message", text)

    def think(self, text: str) -> None:
        """A piece of the agent's reasoning."""
        self._piece("reasoning", text)

    # what the agent does

    def ran(self, command: str, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        """A shell command the agent ran, and how it ended."""
        call = self._id("call")
        self._whole({"id": self._id("sh"), "type": "shell_call", "call_id": call, "status": "completed",
                     "action": {"commands": [command]}})
        self._whole({"id": self._id("sho"), "type": "shell_call_output", "call_id": call, "status": "completed",
                     "output": [{"stdout": stdout, "stderr": stderr,
                                 "outcome": {"type": "exit", "exit_code": exit_code}}]})

    def wrote(self, path: str, diff: str = "") -> None:
        """A file the agent created."""
        self._patched({"type": "create_file", "path": path, "diff": diff})

    def edited(self, path: str, diff: str = "") -> None:
        """A file the agent changed."""
        self._patched({"type": "update_file", "path": path, "diff": diff})

    def deleted(self, path: str) -> None:
        """A file the agent removed."""
        self._patched({"type": "delete_file", "path": path})

    def called(self, server: str, tool: str, arguments: Any = None, output: str | None = None,
               error: str | None = None) -> None:
        """A call the agent made to a tool of an MCP server."""
        item: dict[str, Any] = {"id": self._id("mcp"), "type": "mcp_call", "server_label": server, "name": tool,
                                "arguments": json.dumps({} if arguments is None else arguments),
                                "status": "completed" if error is None else "failed"}
        if output is not None:
            item["output"] = output
        if error is not None:
            item["error"] = {"type": "mcp_tool_execution_error", "content": error}
        self._whole(item)

    def used(self, name: str, given: str = "", output: str = "") -> None:
        """Any other tool the agent used: what it was given, and what came back."""
        call = self._id("call")
        self._whole({"id": self._id("ctc"), "type": "custom_tool_call", "call_id": call, "name": name, "input": given})
        self._whole({"id": self._id("ctco"), "type": "custom_tool_call_output", "call_id": call, "status": "completed",
                     "output": output})

    # the log, and the end

    def log(self, text: str) -> None:
        """A line for the turn's log. The caller of the API never sees it."""
        with self._lock:
            self._print({"type": "hg.log", "text": text})

    def done(self, usage: dict[str, Any] | None = None) -> None:
        """The turn finished. `usage` is what the model reported: input_tokens and output_tokens."""
        self._end({"type": "hg.done"}, usage)

    def incomplete(self, reason: str, usage: dict[str, Any] | None = None) -> None:
        """The turn stopped early: `max_output_tokens` or `content_filter`."""
        self._end({"type": "hg.incomplete", "reason": reason}, usage)

    def failed(self, code: str, message: str, usage: dict[str, Any] | None = None) -> None:
        """The turn failed. `code` is a short lower-case name; `message` tells a person what went wrong."""
        self._end({"type": "hg.failed", "code": code, "message": message}, usage)

    # how

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:24]}"

    def _print(self, line: dict[str, Any]) -> None:
        if self._ended:
            raise RuntimeError("the turn has ended: nothing is printed after the terminal line")
        self._out.write(json.dumps(line) + "\n")
        self._out.flush()

    def _end(self, line: dict[str, Any], usage: dict[str, Any] | None) -> None:
        with self._lock:
            self._close()
            self._print({**line, **({"usage": usage} if usage is not None else {})})
            self._ended = True

    def _patched(self, operation: dict[str, Any]) -> None:
        call = self._id("call")
        self._whole({"id": self._id("ap"), "type": "apply_patch_call", "call_id": call, "status": "completed",
                     "operation": operation})
        self._whole({"id": self._id("apo"), "type": "apply_patch_call_output", "call_id": call, "status": "completed"})

    def _whole(self, item: dict[str, Any]) -> None:
        """An item that is complete when it is first heard of."""
        with self._lock:
            self._close()
            self._print({"type": "response.output_item.added", "output_index": self._index, "item": item})
            self._print({"type": "response.output_item.done", "output_index": self._index, "item": item})
            self._index += 1

    def _piece(self, kind: str, text: str) -> None:
        if not text:
            return
        with self._lock:
            if self._open is not None and self._open["kind"] != kind:
                self._close()
            if self._open is None:
                self._begin(kind)
            assert self._open is not None
            self._open["text"] += text
            at = {"item_id": self._open["id"], "output_index": self._index}
            if kind == "message":
                self._print({"type": "response.output_text.delta", **at, "content_index": 0, "delta": text,
                             "logprobs": []})
            else:
                self._print({"type": "response.reasoning_summary_text.delta", **at, "summary_index": 0, "delta": text})

    def _begin(self, kind: str) -> None:
        self._open = {"kind": kind, "id": self._id("msg" if kind == "message" else "rs"), "text": ""}
        at = {"item_id": self._open["id"], "output_index": self._index}
        if kind == "message":
            self._print({"type": "response.output_item.added", "output_index": self._index, "item": {
                "id": self._open["id"], "type": "message", "role": "assistant", "status": "in_progress",
                "content": []}})
            self._print({"type": "response.content_part.added", **at, "content_index": 0,
                         "part": {"type": "output_text", "text": "", "annotations": []}})
        else:
            self._print({"type": "response.output_item.added", "output_index": self._index, "item": {
                "id": self._open["id"], "type": "reasoning", "summary": []}})
            self._print({"type": "response.reasoning_summary_part.added", **at, "summary_index": 0,
                         "part": {"type": "summary_text", "text": ""}})

    def _close(self) -> None:
        """End the text or reasoning item being gathered, if there is one."""
        if self._open is None:
            return
        text, at = self._open["text"], {"item_id": self._open["id"], "output_index": self._index}
        if self._open["kind"] == "message":
            part: dict[str, Any] = {"type": "output_text", "text": text, "annotations": []}
            self._print({"type": "response.output_text.done", **at, "content_index": 0, "text": text, "logprobs": []})
            self._print({"type": "response.content_part.done", **at, "content_index": 0, "part": part})
            item = {"id": self._open["id"], "type": "message", "role": "assistant", "status": "completed",
                    "content": [part]}
        else:
            part = {"type": "summary_text", "text": text}
            self._print({"type": "response.reasoning_summary_text.done", **at, "summary_index": 0, "text": text})
            self._print({"type": "response.reasoning_summary_part.done", **at, "summary_index": 0, "part": part})
            item = {"id": self._open["id"], "type": "reasoning", "summary": [part]}
        self._print({"type": "response.output_item.done", "output_index": self._index, "item": item})
        self._index += 1
        self._open = None
