"""The OpenAI Responses wire, both directions, by the OpenAI SDK's own types (SPEC §4). Nothing here is a HarnessGateway
schema.

In: a body is checked against `ResponseCreateParams`; the platform reads only what routes and secures a turn
(`Routing`). Out: each stdout line of a package is a Responses stream event (checked against the SDK model its `type`
names) or a log line. The platform owns the envelope: `response.created`, `response.in_progress`, sequence numbers and
the final `Response`."""
from __future__ import annotations

import json
import typing
from dataclasses import dataclass, field
from typing import Any

from openai.types import responses as R
from pydantic import BaseModel, TypeAdapter, ValidationError

from ..errors import Invalid
from ..limits import INPUT_FILES_MAX
from .names import NAME

PARAMS: TypeAdapter[Any] = TypeAdapter(R.ResponseCreateParams)
ITEMS: TypeAdapter[Any] = TypeAdapter(list[R.ResponseOutputItem])
USAGE: TypeAdapter[Any] = TypeAdapter(R.ResponseUsage)
ENVELOPE = frozenset({"response.created", "response.in_progress", "response.queued"})
TERMINAL = {"response.completed": "completed", "response.incomplete": "incomplete", "response.failed": "failed"}
TERMINAL_EVENT = {"completed": "response.completed", "incomplete": "response.incomplete", "failed": "response.failed",
                  "cancelled": "response.failed"}
FINAL = frozenset({"completed", "incomplete", "failed", "cancelled"})
_ECHO = ("instructions", "temperature", "top_p", "tools", "tool_choice", "parallel_tool_calls", "text", "reasoning",
         "max_output_tokens", "max_tool_calls", "truncation", "store", "background", "previous_response_id", "user",
         "safety_identifier", "prompt_cache_key", "service_tier", "top_logprobs")
_ERROR_CODES = frozenset(typing.get_args(R.response_error.ResponseError.model_fields["code"].annotation))
METADATA_MAX = 16


def _by_type(union: Any) -> dict[str, type[BaseModel]]:
    out: dict[str, type[BaseModel]] = {}
    for model in typing.get_args(typing.get_args(union)[0]):
        for literal in typing.get_args(model.model_fields["type"].annotation):
            out[literal] = model
    return out


EVENT_MODELS = _by_type(R.ResponseStreamEvent)
ITEM_MODELS = _by_type(R.ResponseOutputItem)
DELTAS = frozenset(k for k in EVENT_MODELS if k.endswith(".delta"))


def _first(e: ValidationError) -> str:
    err = e.errors()[0]
    where = ".".join(str(p) for p in err.get("loc", ()))
    return f"{where or 'value'}: {err.get('msg', 'invalid')}"


class OutputError(Exception):
    """A package printed something the Responses schema refuses; the turn fails with this message."""


# ── in ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ModelPath:
    """`model` as harness[/provider[/model]]; the model part may hold slashes of its own."""
    harness: str
    provider: str = ""
    model: str = ""

    @classmethod
    def parse(cls, value: object) -> ModelPath:
        parts = value.split("/", 2) if isinstance(value, str) else []
        ok = bool(parts) and NAME.fullmatch(parts[0]) and (len(parts) < 2 or NAME.fullmatch(parts[1])) and \
            (len(parts) < 3 or 0 < len(parts[2]) <= 200)
        if not ok:
            raise Invalid("model is harness, harness/provider or harness/provider/model", param="model")
        return cls(parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) == 3 else "")


@dataclass(frozen=True)
class Routing:
    """What the platform reads from a request; the rest goes to the package untouched."""
    path: ModelPath
    previous_response_id: str | None
    stream: bool
    background: bool
    store: bool
    file_ids: tuple[str, ...]
    mcp_tools: tuple[dict[str, Any], ...]

    @classmethod
    def parse(cls, body: object) -> Routing:
        if not isinstance(body, dict):
            raise Invalid("the request body is a JSON object")
        try:
            PARAMS.validate_python(body)
        except ValidationError as e:
            raise Invalid(f"not a valid Responses request: {_first(e)}") from None
        if body.get("conversation") is not None:
            raise Invalid("conversations are not supported: continue a session with previous_response_id",
                          param="conversation")
        prev = body.get("previous_response_id")
        if prev is not None and not (isinstance(prev, str) and prev.startswith("resp_")):
            raise Invalid("previous_response_id names a response this platform returned", param="previous_response_id")
        tools = tuple(t for t in body.get("tools") or [] if isinstance(t, dict) and t.get("type") == "mcp")
        return cls(ModelPath.parse(body.get("model")), prev, body.get("stream") is True, body.get("background") is True,
                   body.get("store", True) is not False, tuple(file_ids(body.get("input"))), tools)


def _parts(given: Any) -> list[Any]:
    parts: list[Any] = []
    for item in given if isinstance(given, list) else []:
        if isinstance(item, dict):
            parts.append(item)
            if isinstance(item.get("content"), list):
                parts.extend(item["content"])
    return parts


def file_ids(given: Any) -> list[str]:
    """Every `file_id` the input's content parts reference, in order, once each."""
    seen: list[str] = []
    for p in _parts(given):
        ref = isinstance(p, dict) and p.get("type") in ("input_file", "input_image") and isinstance(p.get("file_id"), str)
        if ref and p["file_id"] not in seen:
            seen.append(p["file_id"])
    if len(seen) > INPUT_FILES_MAX:
        raise Invalid(f"a request references at most {INPUT_FILES_MAX} files", param="input")
    return seen


def input_items(body: dict[str, Any]) -> list[dict[str, Any]]:
    given = body.get("input")
    if isinstance(given, str):
        return [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": given}]}]
    return [i for i in given or [] if isinstance(i, dict)]


# ── out ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def check_item(item: Any, where: str) -> None:
    kind = item.get("type") if isinstance(item, dict) else None
    model = ITEM_MODELS.get(kind) if isinstance(kind, str) else None
    if model is None:
        raise OutputError(f"{where}: {kind!r} is not a Responses output item type")
    try:
        model.model_validate(item)
    except ValidationError as e:
        raise OutputError(f"{where} ({kind}).{_first(e)}") from None


@dataclass(frozen=True)
class Line:
    """One stdout line, read: an event to deliver, the terminal event, an envelope event to drop, or a log line."""
    kind: str                                   # event | terminal | envelope | log
    event: dict[str, Any] = field(default_factory=dict)
    text: str = ""

    @property
    def status(self) -> str:
        return TERMINAL[self.event["type"]]


def read_line(text: str) -> Line:
    s = text.strip()
    if not s.startswith("{"):
        return Line("log", text=text)
    try:
        event = json.loads(s)
    except ValueError:
        return Line("log", text=text)
    kind = event.get("type") if isinstance(event, dict) else None
    if not isinstance(kind, str) or not kind.startswith("response."):
        return Line("log", text=text)
    if kind in ENVELOPE:
        return Line("envelope", event)
    if kind in TERMINAL:
        if not isinstance(event.get("response"), dict):
            event["response"] = {}
        return Line("terminal", event)
    model = EVENT_MODELS.get(kind)
    if model is None:
        raise OutputError(f"{kind} is not a Responses stream event")
    if isinstance(event.get("item"), dict):
        check_item(event["item"], f"{kind}: item")
    try:
        model.model_validate({**event, "sequence_number": 0})
    except ValidationError as e:
        raise OutputError(f"{kind}: {_first(e)}") from None
    event.pop("sequence_number", None)
    return Line("event", event)


@dataclass(frozen=True)
class Outcome:
    status: str
    output: list[dict[str, Any]]
    usage: dict[str, Any] | None
    error: dict[str, Any] | None
    incomplete: dict[str, Any] | None
    metadata: dict[str, str]


def outcome(terminal: dict[str, Any], items: list[dict[str, Any]]) -> Outcome:
    """The terminal event's parts, validated; `items` (the completed output items) stand in for an absent output."""
    r = terminal["response"]
    given = r.get("output")
    output_items: list[Any] = given if isinstance(given, list) and given else items
    for i, item in enumerate(output_items):
        check_item(item, f"output.{i}")
    try:
        output = [i.model_dump(mode="json", exclude_none=True) for i in ITEMS.validate_python(output_items)]
    except ValidationError as e:
        raise OutputError(f"output: {_first(e)}") from None
    usage = r.get("usage")
    if usage is not None:
        try:
            usage = USAGE.validate_python(usage).model_dump(mode="json")
        except ValidationError as e:
            raise OutputError(f"usage: {_first(e)}") from None
    meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in meta.items()):
        raise OutputError("metadata maps strings to strings")
    def _dict(v: Any) -> dict[str, Any] | None:
        return v if isinstance(v, dict) else None
    return Outcome(TERMINAL[terminal["type"]], output, usage, _dict(r.get("error")), _dict(r.get("incomplete_details")),
                   meta)


def check_output(text: str) -> tuple[list[dict[str, Any]], Outcome]:
    """A package's whole stdout read as the platform reads it: its events and its outcome (OutputError when invalid).
    What package tests use to hold a mapper to the contract."""
    events: list[dict[str, Any]] = []
    items: dict[str, dict[str, Any]] = {}
    ended: Outcome | None = None
    for raw in text.splitlines():
        line = read_line(raw)
        if line.kind == "log" or line.kind == "envelope":
            continue
        if ended is not None:
            raise OutputError(f"{line.event['type']} after the terminal event")
        if line.kind == "terminal":
            ended = outcome(line.event, list(items.values()))
        elif line.event["type"] == "response.output_item.done":
            items[str(line.event["item"].get("id") or len(items))] = line.event["item"]
        events.append(line.event)
    if ended is None:
        raise OutputError("no terminal event")
    return events, ended


def response(*, rid: str, body: dict[str, Any], created: float, status: str, output: list[dict[str, Any]] | None = None,
             usage: dict[str, Any] | None = None, error: dict[str, Any] | None = None,
             incomplete: dict[str, Any] | None = None, metadata: dict[str, str] | None = None,
             completed: float | None = None) -> dict[str, Any]:
    """The `Response` a caller reads, validated by the SDK: the request's parameters echoed, the package's output."""
    meta = {**(body.get("metadata") or {}), **(metadata or {})}
    if error is not None and error.get("code") not in _ERROR_CODES:
        meta["hg_error_code"] = str(error.get("code", "harness_error"))[:64]
        error = {"code": "server_error", "message": str(error.get("message", ""))[:4000]}
    meta = dict(list({k[:64]: str(v)[:512] for k, v in meta.items()}.items())[-METADATA_MAX:])
    doc: dict[str, Any] = {k: body[k] for k in _ECHO if k in body}
    doc.update({"id": rid, "object": "response", "created_at": int(created), "status": status,
                "model": str(body.get("model", "")), "output": output or [], "usage": usage, "error": error,
                "incomplete_details": incomplete, "metadata": meta,
                "completed_at": int(completed) if completed and status in FINAL else None})
    doc.setdefault("tools", [])
    doc.setdefault("tool_choice", "auto")
    doc.setdefault("parallel_tool_calls", True)
    try:
        return R.Response.model_validate(doc).model_dump(mode="json", exclude_none=True, warnings=False)  # type: ignore[no-any-return]
    except ValidationError as e:
        raise OutputError(f"the response does not validate: {_first(e)}") from None


def sse(event: dict[str, Any]) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()
