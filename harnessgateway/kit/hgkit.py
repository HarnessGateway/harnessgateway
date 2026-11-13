"""hgkit: what a harness package's `run` needs, in one file.

A package's `run` is called once per turn. It reads the turn's inputs, starts its CLI, prints events to
standard output as the CLI works, prints one terminal line, and exits 0. This file gives it:

    Request     the turn's inputs
    State       small named values that stay with the session
    seed_mind   copy the package's seed files into the mind, once
    Cli         run the CLI and read its output line by line (or replay a recording of it)
    Emitter     print lines the platform accepts

It needs Python 3.9 or later and nothing outside the standard library. Using it is optional: a package in
any language can print the same lines itself.
"""

from __future__ import annotations

import contextlib
import filecmp
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from typing import IO, Any, Callable

__all__ = ["Cli", "Emitter", "InputFile", "Provider", "Request", "State", "seed_mind"]

_STATE_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*")


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


# ── the turn's inputs ──────────────────────────────────────────────────────────────────────────────────────


class Provider:
    """Where the model is. `key` is a placeholder: the real key is put in by the platform's proxy, on the way out."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self.name = env.get("HG_PROVIDER", "")
        self.api = env.get("HG_PROVIDER_API", "")          # anthropic | openai-responses | openai-chat
        self.url = env.get("HG_PROVIDER_URL", "")
        self.key = env.get("HG_PROVIDER_KEY", "")


class InputFile:
    """An uploaded file the request refers to."""

    def __init__(self, file_id: str, filename: str, path: str) -> None:
        self.id = file_id
        self.filename = filename
        self.path = path


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [part["text"] for part in content
                if isinstance(part, dict) and part.get("type") == "input_text" and isinstance(part.get("text"), str)]
    return []


class Request:
    """The request, and everything else the platform wrote for this turn."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = _env(env)
        self._turn = self._env["HG_TURN"]
        with open(os.path.join(self._turn, "request.json"), encoding="utf-8") as file:
            self.body: dict[str, Any] = json.load(file)                 # the Responses request, as it was sent
        with open(os.path.join(self._turn, "history.json"), encoding="utf-8") as file:
            self.history: list[dict[str, Any]] = json.load(file)        # turns this harness has not seen
        with open(os.path.join(self._turn, "handover.txt"), encoding="utf-8") as file:
            self.handover = file.read().strip()
        self.model = self._env.get("HG_MODEL", "")
        self.provider = Provider(self._env)

    @property
    def prompt(self) -> str:
        """The text the caller sent: a string input as it is, or the text of a list input's user messages."""
        given = self.body.get("input")
        if isinstance(given, str):
            return given
        texts: list[str] = []
        for item in given if isinstance(given, list) else []:
            if isinstance(item, dict) and item.get("role") == "user" and item.get("type", "message") == "message":
                texts.extend(_texts(item.get("content")))
        return "\n\n".join(texts)

    def prompt_with_handover(self) -> str:
        """The prompt, with the handover line in front of it when another harness worked on this session last."""
        return f"{self.handover}\n\n{self.prompt}" if self.handover else self.prompt

    @property
    def instructions(self) -> str | None:
        value = self.body.get("instructions")
        return value if isinstance(value, str) else None

    @property
    def effort(self) -> str | None:
        reasoning = self.body.get("reasoning")
        return reasoning.get("effort") if isinstance(reasoning, dict) else None

    @property
    def mcp_servers(self) -> list[dict[str, Any]]:
        tools = self.body.get("tools")
        return [tool for tool in tools if isinstance(tool, dict) and tool.get("type") == "mcp"] if tools else []

    @property
    def files(self) -> list[InputFile]:
        folder = os.path.join(self._turn, "files")
        found = []
        for file_id in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            for filename in sorted(os.listdir(os.path.join(folder, file_id))):
                found.append(InputFile(file_id, filename, os.path.join(folder, file_id, filename)))
        return found

    def place_files(self) -> list[str]:
        """Copy the input files into `inputs/` in the work folder, where the agent can open them. Their paths."""
        folder = os.path.join(self._env["HG_WORK"], "inputs")
        placed = []
        for file in self.files:
            os.makedirs(folder, exist_ok=True)
            target = os.path.join(folder, file.filename)
            if os.path.exists(target) and not filecmp.cmp(file.path, target, shallow=False):
                target = os.path.join(folder, f"{file.id}-{file.filename}")       # another file has that name
            if not os.path.exists(target):
                shutil.copyfile(file.path, target)
            placed.append(target)
        return placed


# ── what stays with the session ────────────────────────────────────────────────────────────────────────────


class State:
    """Small named values kept in the mind, under $HG_STATE: typically the CLI's own session id.

    Being in the mind, they are saved, restored, rewound and forked with the session.
    """

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._folder = _env(env)["HG_STATE"]

    def _path(self, name: str) -> str:
        if not _STATE_NAME.fullmatch(name):
            raise ValueError(f"a state name is letters, digits, '_', '-' and '.': {name!r}")
        return os.path.join(self._folder, name)

    def get(self, name: str) -> str | None:
        try:
            with open(self._path(name), encoding="utf-8") as file:
                return file.read()
        except FileNotFoundError:
            return None

    def set(self, name: str, value: str) -> None:
        path = self._path(name)
        os.makedirs(self._folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            file.write(value)

    def clear(self, name: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._path(name))


def seed_mind(env: Mapping[str, str] | None = None) -> None:
    """Copy the package's `seeds/mind` into the mind. Nothing that is already there is touched."""
    variables = _env(env)
    seeds, mind = os.path.join(variables["HG_HARNESS"], "seeds", "mind"), variables["HG_MIND"]
    for folder, _, names in os.walk(seeds):
        target = os.path.join(mind, os.path.relpath(folder, seeds))
        os.makedirs(target, exist_ok=True)
        for name in names:
            if not os.path.lexists(os.path.join(target, name)):
                shutil.copy2(os.path.join(folder, name), os.path.join(target, name))


# ── the CLI ────────────────────────────────────────────────────────────────────────────────────────────────


class Cli:
    """The harness's CLI as a process: its standard output, one line at a time.

    When the platform stops the turn, `run` is sent a termination signal; the CLI is stopped with it, `lines()`
    ends, and `stopped` is true.

    When HG_REPLAY names a file, the CLI is not started and the file's lines are given instead. That is how a
    package's translation is tested against a recording of its CLI's real output.

    To make such a recording, give an environment the variable HGKIT_RECORD naming a file (in the work folder,
    say): every line the CLI prints is also written there.
    """

    def __init__(self, command: Sequence[str], env: Mapping[str, str] | None = None, cwd: str | None = None,
                 stdin: str | None = None) -> None:
        self._command = list(command)
        self._env = _env(env)
        self._cwd = cwd
        self._stdin = stdin
        self._process: subprocess.Popen[str] | None = None
        self.exit_code: int | None = None
        self.stopped = False

    def stop(self, *_: Any) -> None:
        self.stopped = True
        if self._process is not None and self._process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self._process.pid, signal.SIGTERM)

    def lines(self) -> Iterator[str]:
        recording = self._env.get("HG_REPLAY")
        if recording:
            with open(recording, encoding="utf-8") as file:
                for line in file:
                    yield line.rstrip("\n")
            self.exit_code = 0
            return
        # The command is the package's own. Its standard error is ours: the turn's log.
        self._process = subprocess.Popen(  # noqa: S603
            self._command, env=dict(self._env), cwd=self._cwd, stdout=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
            stdin=subprocess.PIPE if self._stdin is not None else subprocess.DEVNULL, start_new_session=True,
        )
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self.stop)
        if self._stdin is not None and self._process.stdin is not None:
            self._process.stdin.write(self._stdin)
            self._process.stdin.close()
        assert self._process.stdout is not None
        record_to = self._env.get("HGKIT_RECORD")
        record = open(record_to, "a", encoding="utf-8") if record_to else None  # noqa: SIM115 - closed below
        try:
            for line in self._process.stdout:
                if record is not None:
                    record.write(line)
                    record.flush()
                yield line.rstrip("\n")
        finally:
            if record is not None:
                record.close()
        self.exit_code = self._process.wait()


# ── the lines the platform accepts ─────────────────────────────────────────────────────────────────────────


class Emitter:
    """Prints a turn's output: Responses stream events, log lines, and one terminal line.

    Text and reasoning arrive in pieces and are gathered into one item, which ends when something of another
    kind is printed. Everything else is printed whole.

    Item ids are random, except under `harnessgateway package check` (HG_CHECK is set), where they are counted
    so that a test's output is the same every time.
    """

    def __init__(self, out: IO[str] | None = None, ids: Callable[[], str] | None = None,
                 env: Mapping[str, str] | None = None) -> None:
        self._out = sys.stdout if out is None else out
        self._ids = ids or (self._counted() if _env(env).get("HG_CHECK") else lambda: uuid.uuid4().hex[:24])
        self._index = 0                                     # the next item's place in the output
        self._open: dict[str, Any] | None = None         # the text or reasoning item being gathered
        self._ended = False
        self._lock = threading.Lock()

    @staticmethod
    def _counted() -> Callable[[], str]:
        numbers = iter(range(1, 1_000_000))
        return lambda: f"{next(numbers):04d}"

    # text

    def say(self, text: str) -> None:
        """A piece of the agent's answer."""
        self._piece("message", text)

    def think(self, text: str) -> None:
        """A piece of the agent's reasoning."""
        self._piece("reasoning", text)

    # tools

    def ran(self, command: str, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        """A shell command the agent ran, and how it ended."""
        suffix = self._ids()
        call = f"call_{suffix}"
        self._whole({"id": f"sh_{suffix}", "type": "shell_call", "call_id": call, "status": "completed",
                     "action": {"commands": [command]}})
        self._whole({"id": f"sho_{suffix}", "type": "shell_call_output", "call_id": call, "status": "completed",
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
        item: dict[str, Any] = {
            "id": f"mcp_{self._ids()}", "type": "mcp_call", "server_label": server, "name": tool,
            "arguments": json.dumps({} if arguments is None else arguments),
        }
        if output is not None:
            item["output"] = output
        if error is not None:
            item["error"] = {"type": "mcp_tool_execution_error", "content": error}
        item["status"] = "failed" if error is not None else "completed"
        self._whole(item)

    def used(self, name: str, given: str = "", output: str = "") -> None:
        """Any other tool the agent used: what it was given and what came back."""
        suffix = self._ids()
        call = f"call_{suffix}"
        self._whole({"id": f"ctc_{suffix}", "type": "custom_tool_call", "call_id": call, "name": name, "input": given})
        self._whole({"id": f"ctco_{suffix}", "type": "custom_tool_call_output", "call_id": call, "status": "completed",
                     "output": output})

    # the log, and the end

    def log(self, text: str) -> None:
        """A line for the turn's log. The caller never sees it."""
        with self._lock:
            self._check()
            self._print({"type": "hg.log", "text": text})

    def done(self, usage: dict[str, Any] | None = None) -> None:
        """The turn finished."""
        self._end({"type": "hg.done"}, usage)

    def incomplete(self, reason: str, usage: dict[str, Any] | None = None) -> None:
        """The turn stopped early: `max_output_tokens` or `content_filter`."""
        self._end({"type": "hg.incomplete", "reason": reason}, usage)

    def failed(self, code: str, message: str, usage: dict[str, Any] | None = None) -> None:
        """The turn failed. `code` is a short lower-case name; `message` tells a person what went wrong."""
        self._end({"type": "hg.failed", "code": code, "message": message}, usage)

    # how

    def _print(self, line: dict[str, Any]) -> None:
        self._out.write(json.dumps(line) + "\n")
        self._out.flush()

    def _check(self) -> None:
        if self._ended:
            raise RuntimeError("the turn has ended: nothing can be printed after the terminal line")

    def _end(self, line: dict[str, Any], usage: dict[str, Any] | None) -> None:
        with self._lock:
            self._check()
            self._close()
            if usage is not None:
                line["usage"] = usage
            self._print(line)
            self._ended = True

    def _patched(self, operation: dict[str, Any]) -> None:
        suffix = self._ids()
        call = f"call_{suffix}"
        self._whole({"id": f"ap_{suffix}", "type": "apply_patch_call", "call_id": call, "status": "completed",
                     "operation": operation})
        self._whole({"id": f"apo_{suffix}", "type": "apply_patch_call_output", "call_id": call, "status": "completed"})

    def _whole(self, item: dict[str, Any]) -> None:
        """An item that is complete when it is first heard of."""
        with self._lock:
            self._check()
            self._close()
            self._print({"type": "response.output_item.added", "output_index": self._index, "item": item})
            self._print({"type": "response.output_item.done", "output_index": self._index, "item": item})
            self._index += 1

    def _piece(self, kind: str, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._check()
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
                self._print({"type": "response.reasoning_summary_text.delta", **at, "summary_index": 0,
                             "delta": text})

    def _begin(self, kind: str) -> None:
        suffix = self._ids()
        if kind == "message":
            self._open = {"kind": kind, "id": f"msg_{suffix}", "text": ""}
            at = {"item_id": self._open["id"], "output_index": self._index}
            self._print({"type": "response.output_item.added", "output_index": self._index, "item": {
                "id": self._open["id"], "type": "message", "role": "assistant", "status": "in_progress",
                "content": []}})
            self._print({"type": "response.content_part.added", **at, "content_index": 0,
                         "part": {"type": "output_text", "text": "", "annotations": []}})
        else:
            self._open = {"kind": kind, "id": f"rs_{suffix}", "text": ""}
            at = {"item_id": self._open["id"], "output_index": self._index}
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
            part = {"type": "output_text", "text": text, "annotations": []}
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
