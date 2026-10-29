"""hgkit: helpers for a HarnessGateway package's mapper, in the sandbox image at /opt/hg/lib (on PYTHONPATH). SPEC §5.7.

Optional and CLI-agnostic: a package may print Responses events any way it likes. hgkit makes the right sequence easy:

    from hgkit import Request, Emitter
    req = Request.load()                    # $HG_REQUEST, with readers for the common fields
    out = Emitter()                         # prints Responses stream events on stdout, one JSON object per line
    names = req.place_inputs()              # the request's files, written into the workspace by name
    out.text("Hello")                       # streams into the current assistant message (deltas, then done)
    out.shell("ls", stdout="a\\nb", exit_code=0)
    out.ignored(["temperature"])            # request fields this CLI could not honour (metadata.hg_ignored_fields)
    out.completed(usage=Emitter.usage(10, 5))   # exactly one terminal event, always last

Every event and item matches the OpenAI SDK's models; the platform validates them and assigns sequence numbers.
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
from pathlib import Path
from typing import Any

__all__ = ["Request", "Emitter", "State"]

FILE_PARTS = ("input_file", "input_image")


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


class Request:
    """The Responses request the platform handed to this turn, as the caller sent it (secret values are placeholders),
    with readers for the usual fields."""

    def __init__(self, body: dict[str, Any]):
        self.body = body

    @classmethod
    def load(cls, path: str | None = None) -> Request:
        return cls(json.loads(Path(path or os.environ["HG_REQUEST"]).read_text()))

    def get(self, key: str, default: Any = None) -> Any:
        return self.body.get(key, default)

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

    @staticmethod
    def mcp_servers(path: str | None = None) -> list[dict[str, Any]]:
        """The turn's MCP servers, as the platform resolved them: [{label, url, transport, headers, allowed_tools}],
        header values holding placeholders the door replaces on the way out."""
        p = Path(path or os.environ.get("HG_MCP", "/run/hg/mcp.json"))
        return json.loads(p.read_text()) if p.is_file() else []

    @staticmethod
    def input_files(folder: str | None = None) -> dict[str, dict[str, Any]]:
        """Files the request referenced by file_id, already in the sandbox: {file_id: {filename, path, bytes}}."""
        index = Path(folder or os.environ.get("HG_FILES", "/run/hg/files")) / "index.json"
        return json.loads(index.read_text()) if index.is_file() else {}

    def place_inputs(self, workspace: str | None = None) -> list[str]:
        """Write the input's files (by file_id or inline) into the workspace by name and return the names written. A
        name taken by different bytes gets a numeric suffix; the same bytes under the same name are not written twice.
        Files given only by URL are left to the package."""
        root = Path(workspace or os.environ.get("HG_WORKSPACE", "/workspace"))
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


class Emitter:
    """Prints a valid Responses event stream. Items get output indexes in order; text streams into one message at a
    time (opened on first text, closed when anything else is emitted or the turn ends)."""

    def __init__(self, stream: Any = None):
        self.stream = stream or sys.stdout
        self.index = 0
        self.output: list[dict[str, Any]] = []
        self._msg: dict[str, Any] | None = None
        self._ended = False
        self._ignored: list[str] = []

    def emit(self, event: dict[str, Any]) -> None:
        assert not self._ended, "nothing may follow the terminal event"
        self.stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.stream.flush()

    def log(self, text: str) -> None:
        """A line for the turn's log (never shown to the client)."""
        self.stream.write(text.replace("\n", " ") + "\n")
        self.stream.flush()

    # items ──────────────────────────────────────────────────────────────────────────────────────────────────────────
    def item(self, item: dict[str, Any]) -> dict[str, Any]:
        """A complete output item: added, then done."""
        self.close_text()
        item.setdefault("id", _id(item["type"][:4]))
        self.emit({"type": "response.output_item.added", "output_index": self.index, "item": item})
        self.emit({"type": "response.output_item.done", "output_index": self.index, "item": item})
        self.output.append(item)
        self.index += 1
        return item

    def text(self, delta: str) -> None:
        if not delta:
            return
        if self._msg is None:
            self._msg = {"id": _id("msg"), "type": "message", "role": "assistant", "status": "in_progress", "content": [],
                         "_text": "", "_index": self.index}
            self.emit({"type": "response.output_item.added", "output_index": self.index,
                       "item": {k: v for k, v in self._msg.items() if not k.startswith("_")}})
            self.emit({"type": "response.content_part.added", "output_index": self.index, "item_id": self._msg["id"],
                       "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})
            self.index += 1
        self._msg["_text"] += delta
        self.emit({"type": "response.output_text.delta", "output_index": self._msg["_index"], "item_id": self._msg["id"],
                   "content_index": 0, "delta": delta, "logprobs": []})

    def close_text(self) -> None:
        m = self._msg
        if m is None:
            return
        self._msg = None
        part = {"type": "output_text", "text": m["_text"], "annotations": []}
        base = {"output_index": m["_index"], "item_id": m["id"], "content_index": 0}
        self.emit({"type": "response.output_text.done", **base, "text": m["_text"], "logprobs": []})
        self.emit({"type": "response.content_part.done", **base, "part": part})
        item = {"id": m["id"], "type": "message", "role": "assistant", "status": "completed", "content": [part]}
        self.emit({"type": "response.output_item.done", "output_index": m["_index"], "item": item})
        self.output.append(item)

    def message(self, text: str) -> None:
        """A whole assistant message at once."""
        self.close_text()
        self.text(text)
        self.close_text()

    def reasoning(self, text: str) -> None:
        if text.strip():
            self.item({"type": "reasoning", "summary": [{"type": "summary_text", "text": text}]})

    def shell(self, command: str, *, stdout: str = "", stderr: str = "", exit_code: int | None = 0,
              call_id: str | None = None) -> None:
        cid = call_id or _id("call")
        self.item({"type": "shell_call", "call_id": cid, "status": "completed", "action": {"commands": [command]}})
        outcome = {"type": "exit", "exit_code": exit_code} if exit_code is not None else {"type": "timeout"}
        self.item({"type": "shell_call_output", "call_id": cid, "status": "completed",
                   "output": [{"stdout": stdout, "stderr": stderr, "outcome": outcome}]})

    def patch(self, operation: str, path: str, diff: str = "", *, ok: bool = True, output: str = "",
              call_id: str | None = None) -> None:
        """operation: create_file | update_file | delete_file."""
        cid = call_id or _id("call")
        op: dict[str, Any] = {"type": operation, "path": path}
        if operation != "delete_file":
            op["diff"] = diff
        self.item({"type": "apply_patch_call", "call_id": cid, "status": "completed", "operation": op})
        self.item({"type": "apply_patch_call_output", "call_id": cid, "status": "completed" if ok else "failed",
                   "output": output})

    def function(self, name: str, arguments: Any, output: str | None = None, *, ok: bool = True,
                 call_id: str | None = None) -> None:
        cid = call_id or _id("call")
        args = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
        self.item({"type": "function_call", "call_id": cid, "name": name, "arguments": args, "status": "completed"})
        if output is not None:
            self.item({"type": "function_call_output", "call_id": cid, "output": output,
                       "status": "completed" if ok else "incomplete"})

    def mcp(self, server: str, name: str, arguments: Any, output: str | None = None, error: str | None = None) -> None:
        args = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
        failure = {"type": "mcp_tool_execution_error", "content": [{"type": "text", "text": error}]} if error else None
        self.item({"type": "mcp_call", "server_label": server, "name": name, "arguments": args,
                   "status": "failed" if error else "completed", "output": output, "error": failure})

    def ignored(self, fields: list[str]) -> None:
        """Request fields this turn did not act on, reported in the terminal event's metadata.hg_ignored_fields."""
        self._ignored += [f for f in dict.fromkeys(fields) if f not in self._ignored]

    # the end ────────────────────────────────────────────────────────────────────────────────────────────────────────
    @staticmethod
    def usage(input_tokens: int = 0, output_tokens: int = 0, cached: int = 0, reasoning: int = 0,
              cache_write: int = 0) -> dict[str, Any]:
        """Responses usage. input_tokens includes cached and cache-written tokens, as OpenAI counts them."""
        return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens,
                "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": cache_write},
                "output_tokens_details": {"reasoning_tokens": reasoning}}

    def _end(self, kind: str, response: dict[str, Any]) -> None:
        self.close_text()
        if self._ignored:
            response["metadata"] = {**(response.get("metadata") or {}), "hg_ignored_fields": ",".join(self._ignored)}
        self.emit({"type": kind, "response": response})
        self._ended = True

    def completed(self, usage: dict[str, Any] | None = None, metadata: dict[str, str] | None = None) -> None:
        self._end("response.completed", {"usage": usage, "metadata": metadata or {}})

    def incomplete(self, reason: str = "max_output_tokens", usage: dict[str, Any] | None = None) -> None:
        self._end("response.incomplete", {"incomplete_details": {"reason": reason}, "usage": usage})

    def failed(self, code: str, message: str, usage: dict[str, Any] | None = None) -> None:
        self._end("response.failed", {"error": {"code": code, "message": message}, "usage": usage})

    @property
    def ended(self) -> bool:
        return self._ended
