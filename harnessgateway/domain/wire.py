"""The OpenAI Responses wire, in both directions, by the OpenAI SDK's own types. Nothing here is a HarnessGateway
schema: the request is a Responses request and the output is Responses events.

In: the client's body is checked against `ResponseCreateParams` and kept verbatim; the platform reads only what routes
and delivers a turn (`Routing`). Out: each stdout line a package prints is either a Responses stream event (checked
against the SDK model for its `type`) or a log line. The platform owns the envelope: it emits `response.created` and
`response.in_progress`, numbers every event, and builds the final `Response` from the package's terminal event.
"""
from __future__ import annotations

import json
import time
import typing
from dataclasses import dataclass, field
from typing import Any

from openai.types import responses as R
from pydantic import BaseModel, TypeAdapter, ValidationError

from ..errors import Invalid
from ..ids import NAME
from ..limits import EVENT_LINE_MAX, INPUT_FILES_MAX

PARAMS = TypeAdapter(R.ResponseCreateParams)
ITEMS = TypeAdapter(list[R.ResponseOutputItem])
USAGE = TypeAdapter(R.ResponseUsage)
ENVELOPE = frozenset({"response.created", "response.in_progress", "response.queued"})
TERMINAL = {"response.completed": "completed", "response.incomplete": "incomplete", "response.failed": "failed"}
FINAL_STATUSES = frozenset({"completed", "incomplete", "failed", "cancelled"})
TERMINAL_EVENT = {"completed": "response.completed", "incomplete": "response.incomplete", "failed": "response.failed",
                  "cancelled": "response.failed"}
_RESPONSE_ECHO = ("instructions", "temperature", "top_p", "tools", "tool_choice", "parallel_tool_calls", "text",
                  "reasoning", "max_output_tokens", "max_tool_calls", "truncation", "store", "background",
                  "previous_response_id", "user", "safety_identifier", "prompt_cache_key", "service_tier", "top_logprobs")
_SDK_ERROR_CODES = frozenset(typing.get_args(R.response_error.ResponseError.model_fields["code"].annotation))


def _by_type(annotated_union: Any) -> dict[str, type[BaseModel]]:
    """{type literal: model} for one of the SDK's discriminated unions."""
    out: dict[str, type[BaseModel]] = {}
    for model in typing.get_args(typing.get_args(annotated_union)[0]):
        for literal in typing.get_args(model.model_fields["type"].annotation):
            out[literal] = model
    return out


EVENT_MODELS = _by_type(R.ResponseStreamEvent)
ITEM_MODELS = _by_type(R.ResponseOutputItem)


class HarnessOutputError(Exception):
    """The package printed something the Responses schema refuses. The turn fails with this message."""


def _first(e: ValidationError) -> str:
    err = e.errors()[0]
    where = ".".join(str(p) for p in err.get("loc", ()))
    return f"{where or 'value'}: {err.get('msg', 'invalid')}"


def check_item(item: Any, where: str) -> None:
    """An output item against the SDK model its own `type` names, so a refusal names the field that is wrong."""
    kind = item.get("type") if isinstance(item, dict) else None
    model = ITEM_MODELS.get(kind) if isinstance(kind, str) else None
    if model is None:
        raise HarnessOutputError(f"{where}: {kind!r} is not a Responses output item type")
    try:
        model.model_validate(item)
    except ValidationError as e:
        raise HarnessOutputError(f"{where} ({kind}).{_first(e)}") from None


# ── in ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ModelPath:
    """`model` as harness[/provider[/model]]: the harness picks the installed package, the provider picks the
    organization's endpoint, and the rest (which may hold slashes of its own) is the model name the harness gets."""
    harness: str
    provider: str = ""
    model: str = ""

    @classmethod
    def parse(cls, value: object) -> ModelPath:
        if not isinstance(value, str):
            raise Invalid("model is harness, harness/provider or harness/provider/model", param="model")
        parts = value.split("/", 2)
        if not NAME.fullmatch(parts[0]) or (len(parts) > 1 and not NAME.fullmatch(parts[1])) or \
                (len(parts) == 3 and not (0 < len(parts[2]) <= 200)):
            raise Invalid("model is harness, harness/provider or harness/provider/model", param="model")
        return cls(parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) == 3 else "")


@dataclass(frozen=True)
class Routing:
    """What the platform reads from a request; the body itself goes to the package untouched."""
    path: ModelPath
    previous_response_id: str | None
    stream: bool
    background: bool
    store: bool
    file_ids: tuple[str, ...] = ()

    @classmethod
    def parse(cls, body: object) -> Routing:
        if not isinstance(body, dict):
            raise Invalid("the request body is a JSON object")
        try:
            PARAMS.validate_python(body)
        except ValidationError as e:
            raise Invalid(f"not a valid Responses request: {_first(e)}") from None
        prev = body.get("previous_response_id")
        if prev is not None and not (isinstance(prev, str) and prev.startswith("resp_")):
            raise Invalid("previous_response_id names a response this platform returned", param="previous_response_id")
        return cls(ModelPath.parse(body.get("model")), prev, body.get("stream") is True, body.get("background") is True,
                   body.get("store", True) is not False, tuple(_file_ids(body.get("input"))))


def _file_ids(given: Any) -> list[str]:
    """Every `file_id` referenced by the input's content parts, in order, without repeats."""
    seen: list[str] = []
    parts: list[Any] = []
    for item in given if isinstance(given, list) else []:
        if isinstance(item, dict):
            parts.append(item)
            if isinstance(item.get("content"), list):
                parts.extend(item["content"])
    for p in parts:
        is_ref = isinstance(p, dict) and p.get("type") in ("input_file", "input_image") and isinstance(p.get("file_id"), str)
        if is_ref and p["file_id"] not in seen:
            seen.append(p["file_id"])
    if len(seen) > INPUT_FILES_MAX:
        raise Invalid(f"a request references at most {INPUT_FILES_MAX} files", param="input")
    return seen


def input_items(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The request's input as a list of items, the way GET /v1/responses/{id}/input_items lists it."""
    given = body.get("input")
    if isinstance(given, str):
        return [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": given}]}]
    return [i for i in given or [] if isinstance(i, dict)]


# ── out ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Terminal:
    status: str
    response: dict[str, Any]


@dataclass
class TurnOutput:
    """Reads a package's stdout line by line. Each line is a Responses event (returned for delivery, without its
    sequence number), the terminal event (kept to build the final response), or a log line (kept, never delivered)."""
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    terminal: Terminal | None = None

    def line(self, text: str) -> tuple[str, Any]:
        """("event", event) to deliver (without its sequence number), ("terminal", status), ("envelope", type) to drop,
        or ("log", text) for anything that is not a Responses event."""
        if len(text) > EVENT_LINE_MAX:
            raise HarnessOutputError(f"an output line is over {EVENT_LINE_MAX // 1024} KiB")
        s = text.strip()
        if not s.startswith("{"):
            return "log", text
        try:
            event = json.loads(s)
        except ValueError:
            return "log", text
        kind = event.get("type") if isinstance(event, dict) else None
        if not isinstance(kind, str) or not kind.startswith("response."):
            return "log", text
        if self.terminal is not None:
            raise HarnessOutputError(f"{kind} printed after the terminal event")
        if kind in ENVELOPE:
            return "envelope", kind                       # the platform owns the envelope
        if kind in TERMINAL:
            partial = event.get("response")
            self.terminal = Terminal(TERMINAL[kind], partial if isinstance(partial, dict) else {})
            return "terminal", self.terminal.status
        model = EVENT_MODELS.get(kind)
        if model is None:
            raise HarnessOutputError(f"{kind} is not a Responses stream event")
        if isinstance(event.get("item"), dict):
            check_item(event["item"], f"{kind}: item")
        try:
            model.model_validate({**event, "sequence_number": 0})
        except ValidationError as e:
            raise HarnessOutputError(f"{kind}: {_first(e)}") from None
        if kind == "response.output_item.done":
            self.add_item(event["item"])
        event.pop("sequence_number", None)
        return "event", event

    def add_item(self, item: dict[str, Any]) -> None:
        key = str(item.get("id") or f"#{len(self.order)}")
        if key not in self.items:
            self.order.append(key)
        self.items[key] = item

    def outcome(self) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None,
                               dict[str, Any] | None, dict[str, str]]:
        """(status, output, usage, error, incomplete_details, metadata) as the package left them. Raises
        HarnessOutputError when the terminal event's parts are not valid Responses values."""
        assert self.terminal is not None, "outcome() is read only after a terminal event"
        r = self.terminal.response
        output = r.get("output") if isinstance(r.get("output"), list) and r.get("output") else \
            [self.items[k] for k in self.order]
        for i, item in enumerate(output):
            check_item(item, f"output.{i}")
        try:
            output = [i.model_dump(mode="json", exclude_none=True) for i in ITEMS.validate_python(output)]
        except ValidationError as e:
            raise HarnessOutputError(f"output: {_first(e)}") from None
        usage = r.get("usage")
        if usage is not None:
            try:
                usage = USAGE.validate_python(usage).model_dump(mode="json")
            except ValidationError as e:
                raise HarnessOutputError(f"usage: {_first(e)}") from None
        error = r.get("error") if isinstance(r.get("error"), dict) else None
        incomplete = r.get("incomplete_details") if isinstance(r.get("incomplete_details"), dict) else None
        meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in meta.items()):
            raise HarnessOutputError("metadata maps strings to strings")
        return self.terminal.status, output, usage, error, incomplete, meta


def response(*, rid: str, body: dict[str, Any], created: float, status: str, output: list[dict[str, Any]] | None = None,
             usage: dict[str, Any] | None = None, error: dict[str, Any] | None = None,
             incomplete: dict[str, Any] | None = None, metadata: dict[str, str] | None = None,
             completed: float | None = None) -> dict[str, Any]:
    """The `Response` a client reads, validated by the SDK: the request's parameters echoed, the package's output."""
    meta = {**(body.get("metadata") or {}), **(metadata or {})}
    if error is not None and error.get("code") not in _SDK_ERROR_CODES:
        meta["hg_error_code"] = str(error.get("code", "harness_error"))[:64]
        error = {"code": "server_error", "message": str(error.get("message", ""))[:4000]}
    doc: dict[str, Any] = {k: body[k] for k in _RESPONSE_ECHO if k in body}
    doc.update({"id": rid, "object": "response", "created_at": int(created), "status": status,
                "model": str(body.get("model", "")), "output": output or [], "usage": usage, "error": error,
                "incomplete_details": incomplete, "metadata": meta,
                "completed_at": int(completed) if completed and status in FINAL_STATUSES else None})
    doc.setdefault("tools", [])
    doc.setdefault("tool_choice", "auto")
    doc.setdefault("parallel_tool_calls", True)
    try:
        return R.Response.model_validate(doc).model_dump(mode="json", exclude_none=True, warnings=False)
    except ValidationError as e:
        raise HarnessOutputError(f"the response does not validate: {_first(e)}") from None


def sse(event: dict[str, Any]) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()


def now() -> float:
    return time.time()
