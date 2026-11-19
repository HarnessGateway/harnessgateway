"""hgkit: what a harness package's `run` needs, in one file.

A package's `run` is called once per turn. It reads the turn's inputs, starts its CLI, prints events to
standard output as the CLI works, prints one terminal line, and exits 0. This file gives it:

    Request     the turn's inputs
    State       small named values that stay with the session
    seed        copy the package's seed files into the mind and the work folder, once
    outbound    the variables a process the CLI starts needs to reach out (an MCP server's, say)
    Cli         run the CLI and read its output line by line (or replay a recording of it)
    Emitter     print lines the platform accepts

It needs Python 3.9 or later and nothing outside the standard library. Using it is optional: a package in
any language can print the same lines itself.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import filecmp
import functools
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from typing import IO, Any, Callable

__all__ = ["Cli", "Emitter", "InputFile", "Provider", "Request", "State", "outbound", "seed"]

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

    def brief(self, *, instructions: bool = True) -> str:
        """What the agent is asked this turn, as one text: the handover line, when another harness worked on
        this session last; the request's instructions (pass instructions=False when the CLI takes them some
        other way); where the request's files were put; then the prompt."""
        work = self._env["HG_WORK"]
        placed = [os.path.relpath(path, work) for path in self.place_files()]
        parts = [
            self.handover,
            f"Instructions for this request:\n{self.instructions}" if instructions and self.instructions else "",
            "Files given with this request: " + ", ".join(placed) if placed else "",
            self.prompt,
        ]
        return "\n\n".join(part for part in parts if part)

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

    def _inline(self) -> list[tuple[str, str, str]]:
        """Files and images sent inside the request itself, in order: a name, a second name for when the first
        is another file's, and the content as the request carries it."""
        found: list[tuple[str, str, str]] = []
        given = self.body.get("input")
        for item in given if isinstance(given, list) else []:
            content = item.get("content") if isinstance(item, dict) else None
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                number = len(found) + 1
                if part.get("type") == "input_file" and isinstance(part.get("file_data"), str):
                    name = os.path.basename(str(part.get("filename") or ""))        # a name, never a path
                    if name in ("", ".", ".."):
                        name = f"file-{number}"
                    found.append((name, f"inline-{number}-{name}", part["file_data"]))
                elif part.get("type") == "input_image" and str(part.get("image_url") or "").startswith("data:"):
                    media = part["image_url"][5:].split(",")[0].split(";")[0]
                    name = f"image-{number}{mimetypes.guess_extension(media) or '.bin'}"
                    found.append((name, f"inline-{number}-{name}", part["image_url"]))
        return found

    def place_files(self) -> list[str]:
        """Put the request's files into `inputs/` in the work folder, where the agent can open them: the
        uploaded files it names, then the files and images sent inside it. Their paths.

        A file takes the place of one of the same name left by an earlier turn: what is there is what this
        request sent. Two files of one name in the same request both stay, the second under a longer name.
        Placing again changes nothing. Content the request only gives a URL for is not fetched."""
        folder = os.path.join(self._env["HG_WORK"], "inputs")
        taken: set[str] = set()

        def place(name: str, other: str, same: Callable[[str], bool], write: Callable[[str], Any]) -> str:
            os.makedirs(folder, exist_ok=True)
            name = other if name in taken else name                     # this request already sent that name
            taken.add(name)
            target = os.path.join(folder, name)
            if not (os.path.exists(target) and same(target)):
                write(target)
            return target

        placed = []
        for file in self.files:
            placed.append(place(file.filename, f"{file.id}-{file.filename}",
                                functools.partial(filecmp.cmp, file.path, shallow=False),
                                functools.partial(shutil.copyfile, file.path)))
        for name, other, data in self._inline():
            try:
                content = base64.b64decode(data.partition(",")[2] if data.startswith("data:") else data,
                                           validate=True)
            except ValueError:
                raise ValueError(f"the file {name!r} sent in the request is not valid base64") from None
            placed.append(place(name, other, functools.partial(_holds, content=content),
                                functools.partial(_write, content=content)))
        return placed


def _holds(path: str, content: bytes) -> bool:
    with open(path, "rb") as file:
        return file.read() == content


def _write(path: str, content: bytes) -> None:
    with open(path, "wb") as file:
        file.write(content)


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


def seed(env: Mapping[str, str] | None = None) -> None:
    """Copy the package's seeds into the session: `seeds/mind` into the mind, `seeds/work` into the work folder.
    Nothing that is already there is touched.

    The mind is this harness's own. The work folder is the session's: the agent can change what is put there,
    the caller sees it among the session's files, and another harness that works on the session finds it.
    Seed the work folder only with what the CLI reads from nowhere else."""
    variables = _env(env)
    for name, place in (("mind", variables["HG_MIND"]), ("work", variables["HG_WORK"])):
        seeds = os.path.join(variables["HG_HARNESS"], "seeds", name)
        for folder, _, names in os.walk(seeds):
            target = os.path.join(place, os.path.relpath(folder, seeds))
            os.makedirs(target, exist_ok=True)
            for file in names:
                if not os.path.lexists(os.path.join(target, file)):
                    shutil.copy2(os.path.join(folder, file), os.path.join(target, file))


# What the platform sets so that a process reaches out: the proxy's address, and its certificate authority.
_OUTBOUND = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "NO_PROXY", "no_proxy", "NODE_USE_ENV_PROXY",
             "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "NODE_EXTRA_CA_CERTS",
             "PIP_CERT")


def outbound(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The variables a process needs to reach anything from the sandbox, as this turn has them.

    The CLI has them already. A process the CLI starts with an environment of its own choosing does not: a
    stdio MCP server is given what its configuration block names and little else. Write these into that
    configuration at the start of every turn, never into the package: the proxy's address is this turn's own."""
    variables = _env(env)
    return {name: variables[name] for name in _OUTBOUND if name in variables}


# ── the CLI ────────────────────────────────────────────────────────────────────────────────────────────────


_PR_SET_CHILD_SUBREAPER = 36          # <linux/prctl.h>


def _keep_what_the_cli_starts() -> None:
    """Have every process the CLI leaves running apart from itself (a daemon it starts for its own use) stay
    below `run`, where a stop can find it. On Linux; elsewhere a stop reaches the CLI's own process group."""
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL(None).prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)


def _started_by_run() -> list[int]:
    """The processes below this one that are still running, read from /proc. None where there is no /proc."""
    parents = {}
    with contextlib.suppress(OSError):
        for entry in filter(str.isdigit, os.listdir("/proc")):
            with contextlib.suppress(OSError), open(f"/proc/{entry}/stat", encoding="utf-8") as file:
                state, parent = file.read().rpartition(")")[2].split()[:2]
                if state != "Z":                            # one that has ended and is not yet collected
                    parents[int(entry)] = int(parent)
    found: list[int] = []
    above = {os.getpid()}
    while above:
        above = {pid for pid, parent in parents.items() if parent in above}
        found.extend(above)
    return found


class Cli:
    """The harness's CLI as a process: its standard output, one line at a time.

    When the platform stops the turn, `run` is sent a termination signal. The CLI is sent it too, with every
    process it started, wherever that put itself; when they have ended `run` ends there, with exit code 0.
    Nothing after `lines()` is reached, so a stopped turn prints no terminal line and starts no other CLI.
    What the package must keep for the next turn (the CLI's session id), it saves the moment it has it.

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
        self._stopped = False
        self.exit_code: int | None = None

    def _stop(self, *_: Any) -> None:
        self._stopped = True
        assert self._process is not None
        started = _started_by_run()
        with contextlib.suppress(ProcessLookupError):
            if started:
                for pid in started:
                    os.kill(pid, signal.SIGTERM)
            elif self._process.poll() is None:
                os.killpg(self._process.pid, signal.SIGTERM)

    def lines(self) -> Iterator[str]:
        recording = self._env.get("HG_REPLAY")
        if recording:
            with open(recording, encoding="utf-8") as file:
                for line in file:
                    yield line.rstrip("\n")
            self.exit_code = 0
            return
        _keep_what_the_cli_starts()
        # The command is the package's own. Its standard error is ours: the turn's log.
        self._process = subprocess.Popen(  # noqa: S603
            self._command, env=dict(self._env), cwd=self._cwd, stdout=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
            stdin=subprocess.PIPE if self._stdin is not None else subprocess.DEVNULL, start_new_session=True,
        )
        # Signals are handled in the main thread only, which is where a `run` reads its CLI.
        handled = threading.current_thread() is threading.main_thread()
        before = signal.signal(signal.SIGTERM, self._stop) if handled else None
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
        if handled:
            signal.signal(signal.SIGTERM, before)       # between CLIs a stop ends `run` by itself
        if self._stopped:
            while _started_by_run():                    # what the CLI started is ending; the platform's grace
                time.sleep(0.1)                         # is the limit on how long that may take
            raise SystemExit(0)


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
