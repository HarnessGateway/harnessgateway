"""hgkit: helpers for a HarnessGateway package's mapper, in the sandbox image at /opt/hg/lib (on PYTHONPATH).

Optional: a package may print Responses events any way it likes. hgkit just makes the right sequence easy:

    from hgkit import Request, Emitter
    req = Request.load()                    # $HG_REQUEST, with helpers for the common fields
    out = Emitter()                         # prints Responses stream events on stdout, one JSON object per line
    out.text("Hello")                       # streams into the current assistant message (deltas, then done)
    out.shell("ls", stdout="a\\nb", exit_code=0)
    out.completed(usage={"input_tokens": 10, "output_tokens": 5})   # exactly one terminal event, always last

Every event and item matches the OpenAI SDK's models; the platform validates them and assigns sequence numbers.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

__all__ = ["Request", "Emitter", "State", "settings", "input_files"]


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


class Request:
    """The Responses request the platform handed to this turn, verbatim, with readers for the usual fields."""

    def __init__(self, body: dict[str, Any]):
        self.body = body

    @classmethod
    def load(cls, path: str | None = None) -> Request:
        return cls(json.loads(Path(path or os.environ["HG_REQUEST"]).read_text()))

    def get(self, key: str, default: Any = None) -> Any:
        return self.body.get(key, default)

    def _parts(self) -> list[dict[str, Any]]:
        given = self.body.get("input")
        if isinstance(given, str):
            return [{"type": "input_text", "text": given}]
        parts: list[dict[str, Any]] = []
        for item in given or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") in ("input_text", "input_file", "input_image"):
                parts.append(item)
            elif item.get("type", "message") == "message" and item.get("role") in ("user", "system", "developer"):
                content = item.get("content")
                if isinstance(content, str):
                    parts.append({"type": "input_text", "text": content})
                else:
                    parts.extend(p for p in content or [] if isinstance(p, dict))
        return parts

    @property
    def prompt(self) -> str:
        """All input text, joined (the latest user turn's text plus any system/developer messages in the input)."""
        return "\n".join(p.get("text", "") for p in self._parts() if p.get("type") == "input_text").strip()

    @property
    def instructions(self) -> str:
        ins = self.body.get("instructions")
        if isinstance(ins, list):
            return "\n".join(x.get("content", "") if isinstance(x.get("content"), str) else
                             "\n".join(c.get("text", "") for c in x.get("content") or []) for x in ins if isinstance(x, dict))
        return ins or ""

    @property
    def reasoning_effort(self) -> str:
        return (self.body.get("reasoning") or {}).get("effort") or ""

    @property
    def inline_files(self) -> list[tuple[str, bytes]]:
        """input_file / input_image parts that carry their bytes inline (file_data or a data: URL)."""
        import base64
        out = []
        for i, p in enumerate(self._parts()):
            data = p.get("file_data") or (p.get("image_url") if str(p.get("image_url", "")).startswith("data:") else None)
            if not data:
                continue
            name = p.get("filename") or f"input-{i + 1}"
            raw = base64.b64decode(data.split(",", 1)[1]) if data.startswith("data:") else data.encode()
            out.append((os.path.basename(name), raw))
        return out

    @property
    def mcp_servers(self) -> list[dict[str, Any]]:
        return [t for t in self.body.get("tools") or [] if isinstance(t, dict) and t.get("type") == "mcp"]


def settings() -> dict[str, str]:
    """This organization's settings for the package (declared in harness.json), secrets included."""
    path = os.environ.get("HG_SETTINGS", "")
    return json.loads(Path(path).read_text()) if path and Path(path).is_file() else {}


def input_files() -> dict[str, dict[str, Any]]:
    """Files the request referenced by file_id, already in the sandbox: {file_id: {filename, path, bytes}}."""
    index = Path(os.environ.get("HG_FILES", "/run/hg/files")) / "index.json"
    return json.loads(index.read_text()) if index.is_file() else {}


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
        self.item({"type": "mcp_call", "server_label": server, "name": name, "arguments": args,
                   "status": "failed" if error else "completed", "output": output, "error": error})

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
