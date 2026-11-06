"""hgkit: the Responses contract for a harness package, in Python. Optional: a package in any language prints the
same JSON lines and imports nothing of this. In the sandbox image at /opt/hg/lib (on PYTHONPATH). SPEC §5.7.

hgkit knows nothing about any harness: only the request a turn receives and the events it must print.

    from hgkit import Request, Emitter
    req = Request.load()                       # the Responses request at $HG_REQUEST, with readers
    out = Emitter()                            # Responses stream events on stdout, one JSON object per line
    req.place_inputs()                         # the request's files, written into the workspace by name

    out.think("Looking at the tests")         # a reasoning item, streamed
    out.say("Running them.")                   # an assistant message, streamed
    run = out.ran("pytest -q")                 # a shell call now ...
    run.done(stdout="3 passed", exit=0)        # ... its output when it comes (or both at once: out.ran(cmd, stdout=..))
    out.wrote("hello.py", "print(1)\\n")       # a file created; out.edited(path, diff), out.deleted(path)
    call = out.called("github", "get_issue", {"n": 1})   # an MCP tool call; call.done(result) / call.failed(error)
    out.used("WebSearch", {"q": "x"}, "...")   # any other tool
    out.ignored(["temperature"])               # request fields this harness could not honour
    out.done(input=1200, output=340, cached=800)         # exactly one terminal event, always last

Every event matches the OpenAI SDK's models; the platform validates them and assigns sequence numbers.
`Emitter(validate=True)` checks each event before printing it, and `check(text)` reads a whole output the way the
platform does: both need the `openai` package (the sandbox image has it).
"""
from __future__ import annotations

import base64
import binascii
import filecmp
import json
import mimetypes
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["Call", "Emitter", "Provider", "Request", "Run", "State", "check"]

FILE_PARTS = ("input_file", "input_image")
VERBS = {"create_file": "Created", "update_file": "Edited", "delete_file": "Deleted"}


def check(text: str) -> Any:
    """A package's whole stdout, read as the platform reads it: (events, outcome), or OutputError naming the field."""
    from .validate import check as _check
    return _check(text)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") in
                     ("input_text", "output_text", "text"))


def _decode(data: str) -> bytes:
    """file_data / an image data: URL: base64 (with or without the data: prefix)."""
    if data.startswith("data:"):
        data = data.split(",", 1)[1] if "," in data else ""
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return data.encode()


def _plus(content: str) -> str:
    """A new file's content as an apply_patch create diff: every line added."""
    body = content[:-1] if content.endswith("\n") else content
    return "\n".join("+" + line for line in body.split("\n")) if content else ""


# ── the request ──────────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Provider:
    """The turn's model provider as the sandbox reaches it: through the door, with the turn's token (SPEC §5.6)."""
    api: str          # anthropic | openai | google
    url: str          # the base URL, without /v1: point the CLI here
    token: str        # the API key to present


class State:
    """Small key/value state kept with the session ($HG_STATE), e.g. the CLI's own session id for --resume."""

    def __init__(self, root: str | None = None):
        self.root = Path(root or os.environ.get("HG_STATE", "/state"))

    def get(self, key: str, default: str = "") -> str:
        p = self.root / key
        return p.read_text().strip() if p.is_file() else default

    def set(self, key: str, value: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / key).write_text(value)


class Request:
    """The Responses request the platform handed to this turn, as the caller sent it (secret values are placeholders),
    with readers for the usual fields and for the turn's environment."""

    def __init__(self, body: dict[str, Any], env: dict[str, str] | None = None):
        self.body = body
        self.env = dict(os.environ if env is None else env)
        self.state = State(self.env.get("HG_STATE"))

    @classmethod
    def load(cls, path: str | None = None) -> Request:
        return cls(json.loads(Path(path or os.environ["HG_REQUEST"]).read_text()))

    def get(self, key: str, default: Any = None) -> Any:
        return self.body.get(key, default)

    # ── the environment ────────────────────────────────────────────────────────────────────────────────────────────
    @property
    def model(self) -> str:
        """The model name the CLI is given (HG_MODEL): the request's, the session's, or the harness's default."""
        return self.env.get("HG_MODEL", "")

    @property
    def provider(self) -> Provider | None:
        """None for a harness that runs without a model."""
        url = self.env.get("HG_PROVIDER_URL", "")
        return Provider(self.env.get("HG_PROVIDER_API", ""), url, self.env.get("HG_PROVIDER_TOKEN", "")) if url else None

    @property
    def workspace(self) -> Path:
        return Path(self.env.get("HG_WORKSPACE", "/workspace"))

    @property
    def session_id(self) -> str:
        return self.env.get("HG_SESSION", "")

    @property
    def response_id(self) -> str:
        return self.env.get("HG_RESPONSE", "")

    @property
    def turn(self) -> int:
        return int(self.env.get("HG_TURN") or 1)

    @property
    def package_version(self) -> str:
        """The package's version from its manifest (HG_PACKAGE_VERSION): `<CLI version>-<revision>`."""
        return self.env.get("HG_PACKAGE_VERSION", "")

    @property
    def cli_version(self) -> str:
        return self.package_version.rsplit("-", 1)[0]

    # ── the task ───────────────────────────────────────────────────────────────────────────────────────────────────
    def _items(self) -> list[dict[str, Any]]:
        given = self.body.get("input")
        if isinstance(given, str):
            return [{"type": "message", "role": "user", "content": given}]
        return [i for i in given or [] if isinstance(i, dict)]

    def _user_parts(self) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        for item in self._items():
            if item.get("type") in ("input_text", *FILE_PARTS):
                parts.append(item)
            elif item.get("type", "message") == "message" and item.get("role", "user") == "user":
                content = item.get("content")
                parts += [{"type": "input_text", "text": content}] if isinstance(content, str) else \
                    [p for p in content or [] if isinstance(p, dict)]
        return parts

    @property
    def prompt(self) -> str:
        """The task: the input's user text, joined."""
        return "\n".join(p.get("text", "") for p in self._user_parts() if p.get("type") == "input_text").strip()

    @property
    def instructions(self) -> str:
        """The request's guidance for this task: `instructions`, then system and developer messages in the input. A
        package appends it to its CLI's standing system prompt."""
        out = [_text(self.body.get("instructions"))] if isinstance(self.body.get("instructions"), str) else \
            [_text(i.get("content")) for i in self.body.get("instructions") or [] if isinstance(i, dict)]
        out += [_text(i.get("content")) for i in self._items()
                if i.get("type", "message") == "message" and i.get("role") in ("system", "developer")]
        return "\n\n".join(t.strip() for t in out if t and t.strip())

    @property
    def reasoning_effort(self) -> str:
        return (self.body.get("reasoning") or {}).get("effort") or ""

    @property
    def text_format(self) -> dict[str, Any] | None:
        """The requested structured output (`text.format`) when it is not plain text: {type: json_schema, schema, ...}
        or {type: json_object}."""
        fmt = (self.body.get("text") or {}).get("format")
        return fmt if isinstance(fmt, dict) and fmt.get("type") not in (None, "text") else None

    @property
    def max_tool_calls(self) -> int | None:
        v = self.body.get("max_tool_calls")
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None

    def mcp_servers(self, path: str | None = None) -> list[dict[str, Any]]:
        """The turn's MCP servers, as the platform resolved them: [{label, url, transport, headers, allowed_tools}],
        header values holding placeholders the door replaces on the way out."""
        p = Path(path or self.env.get("HG_MCP", "/run/hg/mcp.json"))
        return json.loads(p.read_text()) if p.is_file() else []

    def history(self, path: str | None = None) -> list[dict[str, Any]]:
        """The session's earlier turns in order, as the platform recorded them (`$HG_HISTORY`): [{id, status, input,
        output}], each in Responses form, file bytes left out; empty on a session's first turn. What a package
        continues from when its CLI's own session is not there: a turn killed mid-way, a fork onto this harness from
        another, a CLI that keeps no session."""
        p = Path(path or self.env.get("HG_HISTORY", "/run/hg/history.json"))
        return json.loads(p.read_text()) if p.is_file() else []

    def transcript(self, limit: int = 60_000) -> str:
        """The history as plain text a CLI can be given as context: what each side said, the commands run, the files
        changed, the tools called; the most recent `limit` characters."""
        lines = [line for turn in self.history() for line in _transcript_lines(turn)]
        text = "\n".join(lines)
        return text if len(text) <= limit else "…" + text[-limit:]

    def input_files(self, folder: str | None = None) -> dict[str, dict[str, Any]]:
        """Files the request referenced by file_id, already in the sandbox: {file_id: {filename, path, bytes}}."""
        index = Path(folder or self.env.get("HG_FILES", "/run/hg/files")) / "index.json"
        return json.loads(index.read_text()) if index.is_file() else {}

    def place_inputs(self, workspace: str | None = None) -> list[str]:
        """Write the input's files (by file_id or inline) into the workspace by name and return the names written. A
        name taken by different bytes gets a numeric suffix; the same bytes under the same name are not written twice.
        Files given only by URL are left to the package."""
        root = Path(workspace) if workspace else self.workspace
        index, names = self.input_files(), []
        for n, part in enumerate(p for p in self._user_parts() if p.get("type") in FILE_PARTS):
            entry = index.get(part.get("file_id") or "")
            data = part.get("file_data") or (part.get("image_url") if str(part.get("image_url", "")).startswith("data:")
                                             else None)
            if entry:
                name, source = entry["filename"], Path(entry["path"])
                target = _free(root, name, source)
                if not target.exists():
                    shutil.copyfile(source, target)
            elif data:
                ext = mimetypes.guess_extension(data[5:].split(";", 1)[0]) if data.startswith("data:") else None
                name, raw = part.get("filename") or f"input-{n + 1}{ext or ''}", _decode(data)
                target = _free(root, name, raw)
                if not target.exists():
                    target.write_bytes(raw)
            else:
                continue
            names.append(target.name)
        return names


def _transcript_lines(turn: dict[str, Any]) -> list[str]:
    """One history turn as transcript lines: who said what, then what the agent did."""
    lines: list[str] = []
    for item in turn.get("input") or []:
        if item.get("type", "message") != "message":
            continue
        content, role = item.get("content"), item.get("role", "user")
        names = [p.get("filename") or p.get("file_id") or "image" for p in content
                 if isinstance(p, dict) and p.get("type") in FILE_PARTS] if isinstance(content, list) else []
        text = _text(content)
        if text or names:
            lines.append(f"{'User' if role == 'user' else 'Instructions'}: {text}"
                         + (f" [attached: {', '.join(names)}]" if names else ""))
    for item in turn.get("output") or []:
        lines += _action_lines(item)
    if turn.get("status") == "cancelled":
        lines.append("(this turn was interrupted before it finished)")
    return lines


def _action_lines(item: dict[str, Any]) -> list[str]:
    kind = item.get("type")
    if kind == "message":
        return [f"Assistant: {_text(item.get('content'))}"]
    if kind == "shell_call":
        return ["Ran: " + " && ".join((item.get("action") or {}).get("commands") or [])]
    if kind == "shell_call_output":
        out = []
        for r in item.get("output") or []:
            o, tail = r.get("outcome") or {}, ((r.get("stdout") or "") + (r.get("stderr") or "")).strip()
            end = "timeout" if o.get("type") == "timeout" else f"exit {o.get('exit_code', 0)}"
            out.append(f"  -> {end}" + (f": {tail[-400:]}" if tail else ""))
        return out
    if kind == "apply_patch_call":
        op = item.get("operation") or {}
        return [f"{VERBS.get(str(op.get('type')), 'Changed')}: {op.get('path', '')}"]
    if kind in ("function_call", "mcp_call"):
        name = f"{item.get('server_label')}.{item.get('name')}" if kind == "mcp_call" else item.get("name", "tool")
        return [f"Used: {name}({str(item.get('arguments', ''))[:300]})"]
    if kind == "function_call_output":
        return [f"  -> {str(item.get('output', ''))[-400:]}"]
    return []


def _free(root: Path, name: str, content: Path | bytes) -> Path:
    """`name` in `root`, or `stem-2.ext`, `stem-3.ext`...: the first that is free or already holds the same bytes."""
    name = os.path.basename(name) or "input"
    stem, ext = os.path.splitext(name)
    for i in range(1, 10_000):
        p = root / (name if i == 1 else f"{stem}-{i}{ext}")
        if not p.exists() or (p.is_file() and (filecmp.cmp(p, content, shallow=False) if isinstance(content, Path)
                                               else p.read_bytes() == content)):
            return p
    raise FileExistsError(name)


# ── the response ─────────────────────────────────────────────────────────────────────────────────────────────────────
class Run:
    """A shell call the agent started; `done` reports its output."""

    def __init__(self, out: Emitter, call_id: str):
        self.out, self.call_id = out, call_id

    def done(self, stdout: str = "", stderr: str = "", exit: int | None = 0, timed_out: bool = False) -> None:
        outcome = {"type": "timeout"} if timed_out or exit is None else {"type": "exit", "exit_code": exit}
        self.out.item({"type": "shell_call_output", "call_id": self.call_id, "status": "completed",
                       "output": [{"stdout": stdout, "stderr": stderr, "outcome": outcome}]})


class Call:
    """A tool call the agent made (an MCP tool, or any other); `done` reports its result, `failed` its error."""

    def __init__(self, out: Emitter, call_id: str, item: dict[str, Any] | None, index: int):
        self.out, self.call_id, self._item, self._index = out, call_id, item, index

    def done(self, output: str = "") -> None:
        self._finish("completed", output, None)

    def failed(self, error: str = "") -> None:
        self._finish("failed", error, {"type": "mcp_tool_execution_error", "content": error})

    def cancelled(self) -> None:
        """The turn ended before the result came."""
        self._finish("incomplete", "", None)

    def _finish(self, status: str, text: str, error: dict[str, Any] | None) -> None:
        if self._item is not None:                      # an MCP call: one item, added when called, done now
            item = {**self._item, "status": status, "output": text if status == "completed" else None, "error": error}
            self.out.close()
            self.out.emit({"type": "response.output_item.done", "output_index": self._index, "item": item})
            self.out.output.append(item)
        else:                                           # any other tool: its output item
            self.out.item({"type": "function_call_output", "call_id": self.call_id, "output": text,
                           "status": "completed" if status == "completed" else "incomplete"})


class Emitter:
    """Prints a valid Responses event stream: items in order, text and thinking streamed into one open item at a time
    (closed when anything else is emitted or the turn ends), exactly one terminal event."""

    def __init__(self, stream: Any = None, validate: bool = False):
        self.stream = stream or sys.stdout
        self.index = 0
        self.output: list[dict[str, Any]] = []
        self._open: dict[str, Any] | None = None      # the message or reasoning item being streamed
        self._ended = False
        self._ignored: list[str] = []
        self._validate = None
        if validate:
            from .validate import check_event
            self._validate = check_event

    def emit(self, event: dict[str, Any]) -> None:
        assert not self._ended, "nothing may follow the terminal event"
        if self._validate is not None and event["type"] not in ("response.completed", "response.incomplete",
                                                                "response.failed"):
            self._validate(event)
        self.stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.stream.flush()

    def log(self, text: str) -> None:
        """A line for the turn's log (never shown to the client)."""
        self.stream.write(text.replace("\n", " ") + "\n")
        self.stream.flush()

    # ── streamed items ─────────────────────────────────────────────────────────────────────────────────────────────
    def say(self, delta: str) -> None:
        """Assistant text, streamed into the open message (opened on the first delta)."""
        self._stream("message", delta)

    def think(self, delta: str) -> None:
        """Reasoning, streamed into the open reasoning item (opened on the first delta)."""
        self._stream("reasoning", delta)

    def _stream(self, kind: str, delta: str) -> None:
        if not delta:
            return
        if self._open is not None and self._open["kind"] != kind:
            self.close()
        if self._open is None:
            self._open = {"kind": kind, "id": _id("msg" if kind == "message" else "rs"), "index": self.index, "text": ""}
            base = {"output_index": self.index, "item_id": self._open["id"]}
            if kind == "message":
                self.emit({"type": "response.output_item.added", "output_index": self.index,
                           "item": {"id": self._open["id"], "type": "message", "role": "assistant",
                                    "status": "in_progress", "content": []}})
                self.emit({"type": "response.content_part.added", **base, "content_index": 0,
                           "part": {"type": "output_text", "text": "", "annotations": []}})
            else:
                self.emit({"type": "response.output_item.added", "output_index": self.index,
                           "item": {"id": self._open["id"], "type": "reasoning", "summary": []}})
                self.emit({"type": "response.reasoning_summary_part.added", **base, "summary_index": 0,
                           "part": {"type": "summary_text", "text": ""}})
            self.index += 1
        o = self._open
        o["text"] += delta
        base = {"output_index": o["index"], "item_id": o["id"]}
        if kind == "message":
            self.emit({"type": "response.output_text.delta", **base, "content_index": 0, "delta": delta, "logprobs": []})
        else:
            self.emit({"type": "response.reasoning_summary_text.delta", **base, "summary_index": 0, "delta": delta})

    def close(self) -> None:
        """Close the open message or reasoning item, if any (done for you when anything else is emitted)."""
        o, self._open = self._open, None
        if o is None:
            return
        base = {"output_index": o["index"], "item_id": o["id"]}
        if o["kind"] == "message":
            part = {"type": "output_text", "text": o["text"], "annotations": []}
            self.emit({"type": "response.output_text.done", **base, "content_index": 0, "text": o["text"], "logprobs": []})
            self.emit({"type": "response.content_part.done", **base, "content_index": 0, "part": part})
            item: dict[str, Any] = {"id": o["id"], "type": "message", "role": "assistant", "status": "completed",
                                    "content": [part]}
        else:
            part = {"type": "summary_text", "text": o["text"]}
            self.emit({"type": "response.reasoning_summary_text.done", **base, "summary_index": 0, "text": o["text"]})
            self.emit({"type": "response.reasoning_summary_part.done", **base, "summary_index": 0, "part": part})
            item = {"id": o["id"], "type": "reasoning", "summary": [part]}
        self.emit({"type": "response.output_item.done", "output_index": o["index"], "item": item})
        self.output.append(item)

    # ── complete items ─────────────────────────────────────────────────────────────────────────────────────────────
    def item(self, item: dict[str, Any]) -> dict[str, Any]:
        """A complete output item of any Responses type: added, then done."""
        self.close()
        item.setdefault("id", _id(item["type"][:4]))
        self.emit({"type": "response.output_item.added", "output_index": self.index, "item": item})
        self.emit({"type": "response.output_item.done", "output_index": self.index, "item": item})
        self.output.append(item)
        self.index += 1
        return item

    def ran(self, command: str, *, stdout: str | None = None, stderr: str = "", exit: int | None = 0,
            timed_out: bool = False, timeout_ms: int | None = None, call_id: str | None = None) -> Run:
        """A shell command. With `stdout` given, the call and its output at once; without, the call now and the
        returned Run's `done(...)` later."""
        cid = call_id or _id("call")
        action: dict[str, Any] = {"commands": [command]}
        if timeout_ms is not None:
            action["timeout_ms"] = timeout_ms
        self.item({"type": "shell_call", "call_id": cid, "status": "completed", "action": action})
        run = Run(self, cid)
        if stdout is not None:
            run.done(stdout, stderr, exit, timed_out)
        return run

    def wrote(self, path: str, content: str = "", *, diff: str | None = None, ok: bool = True, output: str = "",
              call_id: str | None = None) -> None:
        """A file created (the diff is every line of `content` added, unless given)."""
        self._patch({"type": "create_file", "path": path, "diff": _plus(content) if diff is None else diff}, ok, output,
                    call_id)

    def edited(self, path: str, diff: str = "", *, old: str | None = None, new: str | None = None, ok: bool = True,
               output: str = "", call_id: str | None = None) -> None:
        """A file changed: a unified diff, or `old` and `new` text to make one from."""
        if old is not None or new is not None:
            diff = "@@\n" + "\n".join("-" + x for x in (old or "").split("\n")) + "\n" + \
                   "\n".join("+" + x for x in (new or "").split("\n"))
        self._patch({"type": "update_file", "path": path, "diff": diff}, ok, output, call_id)

    def deleted(self, path: str, *, ok: bool = True, output: str = "", call_id: str | None = None) -> None:
        self._patch({"type": "delete_file", "path": path}, ok, output, call_id)

    def _patch(self, op: dict[str, Any], ok: bool, output: str, call_id: str | None) -> None:
        cid = call_id or _id("call")
        self.item({"type": "apply_patch_call", "call_id": cid, "status": "completed", "operation": op})
        self.item({"type": "apply_patch_call_output", "call_id": cid, "status": "completed" if ok else "failed",
                   "output": output})

    def called(self, server: str, name: str, arguments: Any, output: str | None = None, error: str | None = None,
               *, call_id: str | None = None) -> Call:
        """An MCP tool call. With `output` or `error`, the whole call at once; without, in progress now and the
        returned Call's `done(result)` or `failed(error)` later."""
        self.close()
        cid = call_id or _id("call")
        item = {"id": f"mcp_{cid[-24:]}", "type": "mcp_call", "server_label": server, "name": name,
                "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
                "status": "in_progress"}
        self.emit({"type": "response.output_item.added", "output_index": self.index, "item": item})
        call = Call(self, cid, item, self.index)
        self.index += 1
        if error is not None:
            call.failed(error)
        elif output is not None:
            call.done(output)
        return call

    def used(self, name: str, arguments: Any, output: str | None = None, *, ok: bool = True,
             call_id: str | None = None) -> Call:
        """Any other tool: a function call. With `output`, the call and its output at once; without, the call now and
        the returned Call's `done(output)` or `failed(output)` later."""
        cid = call_id or _id("call")
        self.item({"type": "function_call", "call_id": cid, "name": name, "status": "completed",
                   "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)})
        call = Call(self, cid, None, -1)
        if output is not None:
            call.done(output) if ok else call.failed(output)
        return call

    def ignored(self, fields: list[str]) -> None:
        """Request fields this turn did not act on, reported in the terminal event's metadata.hg_ignored_fields."""
        self._ignored += [f for f in dict.fromkeys(fields) if f not in self._ignored]

    # ── the end ────────────────────────────────────────────────────────────────────────────────────────────────────
    @staticmethod
    def usage(input: int = 0, output: int = 0, cached: int = 0, reasoning: int = 0, cache_write: int = 0
              ) -> dict[str, Any]:
        """Responses usage. `input` is every input token, cached and cache-written ones included, as OpenAI counts
        them (a CLI on the Anthropic API reports cache reads separately: add them)."""
        return {"input_tokens": input, "output_tokens": output, "total_tokens": input + output,
                "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": cache_write},
                "output_tokens_details": {"reasoning_tokens": reasoning}}

    def _end(self, kind: str, response: dict[str, Any], usage: dict[str, Any] | None, metadata: dict[str, str] | None,
             tokens: dict[str, int]) -> None:
        self.close()
        meta = dict(metadata or {})
        if self._ignored:
            meta["hg_ignored_fields"] = ",".join(self._ignored)
        response["usage"] = self.usage(**tokens) if tokens and usage is None else usage
        response["metadata"] = meta
        self.emit({"type": kind, "response": response})
        self._ended = True

    def done(self, usage: dict[str, Any] | None = None, metadata: dict[str, str] | None = None, **tokens: int) -> None:
        """response.completed: `done(input=.., output=.., cached=..)` or `done(usage={...})`."""
        self._end("response.completed", {}, usage, metadata, tokens)

    def stopped(self, reason: str = "max_output_tokens", usage: dict[str, Any] | None = None,
                metadata: dict[str, str] | None = None, **tokens: int) -> None:
        """response.incomplete: the agent stopped before it was done (an OpenAI reason: max_output_tokens, ...)."""
        self._end("response.incomplete", {"incomplete_details": {"reason": reason}}, usage, metadata, tokens)

    def failed(self, code: str, message: str, usage: dict[str, Any] | None = None,
               metadata: dict[str, str] | None = None, **tokens: int) -> None:
        """response.failed: an OpenAI error code when one fits (rate_limit_exceeded, server_error, invalid_prompt); any
        other code is reported in metadata.hg_error_code."""
        self._end("response.failed", {"error": {"code": code, "message": message}}, usage, metadata, tokens)

    @property
    def ended(self) -> bool:
        return self._ended
