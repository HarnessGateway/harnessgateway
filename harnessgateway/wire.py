"""Layer 2: the OpenAI Responses wire, in both directions, by the SDK's own types. Nothing here is a
HarnessGateway schema.

In: a client's request body is validated as `ResponseCreateParams`. The platform reads the fields that
route and sequence a turn (`model`, `previous_response_id`, `stream`, `background`, `store`, `metadata`)
and decodes `input_file` parts into bytes for the sandbox; the body itself goes to the package whole.

Out: a package prints Responses stream events as it runs and, optionally, one JSON object from
`response.sh` shaping the final response. Both are validated against the SDK's `ResponseOutputItem`
and `ResponseUsage` before the platform builds the `Response` a client reads; a package that prints
something the schema refuses fails the turn with the reason, so a package author sees it at once.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable

from openai.types import responses as R
from pydantic import TypeAdapter, ValidationError

from .errors import Invalid

PARAMS = TypeAdapter(R.ResponseCreateParams)
ITEM = TypeAdapter(R.ResponseOutputItem)
USAGE = TypeAdapter(R.ResponseUsage)
MODEL_FORM = "model is a harness name, harness/provider, or harness/provider/model"
ENVELOPE = {"response.created", "response.in_progress", "response.queued", "response.completed", "response.failed", "response.incomplete"}
FINAL = {"completed": "response.completed", "failed": "response.failed", "incomplete": "response.incomplete", "cancelled": "response.incomplete"}
STATUSES = ("completed", "failed", "incomplete", "cancelled")
ERROR_CODES = {"server_error", "rate_limit_exceeded", "invalid_prompt"}          # the codes a package may use as they are
INCOMPLETE_REASONS = {"max_output_tokens", "max_messages", "content_filter", "steered"}
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")


def refusal(e: ValidationError, what: str) -> Invalid:
    first = e.errors()[0]
    where = ".".join(str(p) for p in first.get("loc", ()) if not isinstance(p, int) or True)
    return Invalid(f"{what}: {where or 'body'}: {first.get('msg', 'invalid')}")


def parse_model(model: str) -> tuple[str, str, str]:
    """`model` as (harness, provider, model): a bare harness name takes the package's defaults;
    harness/provider takes the package's default model; the full path names all three, the model
    last so it may hold slashes of its own."""
    parts = (model or "").split("/", 2)
    if len(parts) == 1 and NAME.fullmatch(parts[0] or ""):
        return parts[0], "", ""
    if len(parts) == 2 and NAME.fullmatch(parts[0]) and NAME.fullmatch(parts[1]):
        return parts[0], parts[1], ""
    if len(parts) == 3 and NAME.fullmatch(parts[0]) and NAME.fullmatch(parts[1]) and parts[2]:
        return parts[0], parts[1], parts[2]
    raise Invalid(MODEL_FORM)


def model_path(harness: str, provider: str, model: str) -> str:
    return f"{harness}/{provider}/{model}" if provider and model else f"{harness}/{provider}" if provider else harness


def decode_file(file_data: str) -> bytes:
    if not file_data.startswith("data:"):
        return file_data.encode()
    head, comma, data = file_data.partition(",")
    if not comma or not head.endswith(";base64"):
        raise Invalid("a data URL in file_data is data:<type>;base64,<data>")
    try:
        return base64.b64decode(data, validate=True)
    except binascii.Error:
        raise Invalid("file_data is not valid base64") from None


def safe_filename(name: str) -> str:
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    if not name or name in (".", "..") or "\0" in name:
        raise Invalid(f"an input_file needs a plain filename, not {name!r}")
    return name


@dataclass(frozen=True)
class Request:
    """A validated request: the body as the client sent it, and what the platform reads from it."""
    body: dict[str, Any]
    harness: str
    provider: str
    model: str
    previous: str | None
    stream: bool
    background: bool
    store: bool
    metadata: dict[str, str]
    text: str                                        # the input's text, for the trace and the console
    files: dict[str, bytes] = field(default_factory=dict)

    @classmethod
    def parse(cls, body: Any) -> Request:
        if not isinstance(body, dict):
            raise Invalid("the request body is a JSON object")
        try:
            PARAMS.validate_python(body)
        except ValidationError as e:
            raise refusal(e, "not a valid Responses request") from None
        if "model" not in body or not isinstance(body["model"], str):
            raise Invalid(MODEL_FORM)
        harness, provider, model = parse_model(body["model"])
        if "input" not in body or body["input"] in ("", [], None):
            raise Invalid("input is required: the text (and files) the harness works on")
        previous = body.get("previous_response_id")
        if previous is not None and not (isinstance(previous, str) and previous.startswith("resp_")):
            raise Invalid("previous_response_id names a response this platform returned (resp_…)")
        metadata = body.get("metadata") or {}
        if not isinstance(metadata, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()):
            raise Invalid("metadata maps strings to strings")
        texts, files = [], {}
        for part in input_parts(body["input"]):
            kind = part.get("type")
            if kind == "input_text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif kind == "input_file":
                if part.get("file_data"):
                    files[safe_filename(part.get("filename", ""))] = decode_file(part["file_data"])
                elif part.get("file_id") or part.get("file_url"):
                    raise Invalid("input_file carries its bytes as file_data (a data URL); file ids and URLs are not stored here")
            elif kind == "input_image" and isinstance(part.get("image_url"), str) and part["image_url"].startswith("data:"):
                files[safe_filename(part.get("filename", "") or f"image-{len(files) + 1}.{image_ext(part['image_url'])}")] = decode_file(part["image_url"])
        if not "".join(texts).strip():
            raise Invalid("input carries no text")
        return cls(body, harness, provider, model, previous, bool(body.get("stream", False)), bool(body.get("background", False)),
                   body.get("store", True) is not False, dict(metadata), "\n".join(texts), files)

    def for_trace(self) -> dict[str, Any]:
        """The body with every file's bytes replaced by its size: what the trace keeps."""
        def strip(part: Any) -> Any:
            if isinstance(part, dict) and part.get("type") == "input_file" and part.get("file_data"):
                return {**part, "file_data": f"<{len(part['file_data'])} characters>"}
            if isinstance(part, dict) and part.get("type") == "input_image" and str(part.get("image_url", "")).startswith("data:"):
                return {**part, "image_url": f"<{len(part['image_url'])} characters>"}
            return part
        body = dict(self.body)
        if isinstance(body.get("input"), list):
            body["input"] = [{**item, "content": [strip(p) for p in item["content"]]} if isinstance(item, dict) and isinstance(item.get("content"), list)
                             else strip(item) for item in body["input"]]
        return body


def image_ext(data_url: str) -> str:
    mime = data_url[5:].split(";", 1)[0].split("/")
    return mime[1] if len(mime) == 2 and mime[1].isalnum() else "bin"


def input_parts(given: Any) -> Iterable[dict[str, Any]]:
    """Every content part of the input: a string is one text part; message items yield their parts."""
    if isinstance(given, str):
        yield {"type": "input_text", "text": given}
        return
    for item in given or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("input_text", "input_file", "input_image"):
            yield item
        elif item.get("type", "message") == "message" and item.get("role") in ("user", "system", "developer"):
            content = item.get("content")
            if isinstance(content, str):
                yield {"type": "input_text", "text": content}
            else:
                yield from (p for p in content or [] if isinstance(p, dict))


# what the package prints
def events_of(text: str) -> tuple[list[dict[str, Any]], str]:
    """The Responses stream events among a package's stdout lines, and the other lines as a log."""
    events, log = [], []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("{") and '"type"' in s:
            try:
                d = json.loads(s)
            except ValueError:
                log.append(line)
                continue
            if isinstance(d, dict) and isinstance(d.get("type"), str) and d["type"].startswith("response."):
                events.append(d)
                continue
        log.append(line)
    return events, "\n".join(log)


@dataclass
class Shape:
    """What a package said the response is: from response.sh, or assembled from its events."""
    output: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    status: str = ""                                 # "" lets the exit code decide
    error: dict[str, str] | None = None
    incomplete: dict[str, str] | None = None
    metadata: dict[str, str] = field(default_factory=dict)


def check_items(items: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise Invalid(f"{where}: output is a list of Responses output items")
    out = []
    for i, item in enumerate(items):
        try:
            model = ITEM.validate_python(item)
        except ValidationError as e:
            raise refusal(e, f"{where}: output[{i}] is not a Responses output item") from None
        out.append(model.model_dump(mode="json", exclude_none=True))
    return out


def check_usage(usage: Any, where: str) -> dict[str, Any] | None:
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise Invalid(f"{where}: usage is an object")
    given = dict(usage)
    given.setdefault("input_tokens_details", {})
    given.setdefault("output_tokens_details", {})
    given["input_tokens_details"] = {"cached_tokens": 0, "cache_write_tokens": 0, **given["input_tokens_details"]}
    given["output_tokens_details"] = {"reasoning_tokens": 0, **given["output_tokens_details"]}
    given.setdefault("total_tokens", int(given.get("input_tokens", 0)) + int(given.get("output_tokens", 0)))
    try:
        return USAGE.validate_python(given).model_dump(mode="json")
    except ValidationError as e:
        raise refusal(e, f"{where}: usage is not Responses usage") from None


def from_events(events: list[dict[str, Any]]) -> Shape:
    """The items the package completed, in order, and the usage of a completion event if it sent one."""
    items: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    usage = None
    for n, e in enumerate(events):
        if e["type"] == "response.output_item.done" and isinstance(e.get("item"), dict):
            key = str(e["item"].get("id") or f"#{n}")
            if key not in items:
                order.append(key)
            items[key] = e["item"]
        elif e["type"] in ("response.completed", "response.incomplete") and isinstance(e.get("response"), dict) and e["response"].get("usage"):
            usage = e["response"]["usage"]
    return Shape(output=check_items([items[k] for k in order], "the stream"), usage=check_usage(usage, "the stream"))


def from_response_sh(text: str) -> Shape:
    """response.sh's one JSON object, validated."""
    try:
        d = json.loads(text)
    except ValueError as e:
        raise Invalid(f"response.sh did not print JSON: {e}") from None
    if not isinstance(d, dict):
        raise Invalid("response.sh prints one JSON object")
    if unknown := sorted(set(d) - {"output", "usage", "status", "error", "incomplete_details", "metadata"}):
        raise Invalid(f"response.sh: unknown keys {', '.join(unknown)}")
    status = d.get("status", "")
    if status and status not in STATUSES:
        raise Invalid(f"response.sh: status is one of {', '.join(STATUSES)}")
    error = d.get("error")
    if error is not None and not (isinstance(error, dict) and isinstance(error.get("message"), str)):
        raise Invalid("response.sh: error is {code, message}")
    incomplete = d.get("incomplete_details")
    if incomplete is not None and not (isinstance(incomplete, dict) and isinstance(incomplete.get("reason"), str)):
        raise Invalid("response.sh: incomplete_details is {reason}")
    metadata = d.get("metadata") or {}
    if not isinstance(metadata, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()):
        raise Invalid("response.sh: metadata maps strings to strings")
    return Shape(check_items(d.get("output", []), "response.sh"), check_usage(d.get("usage"), "response.sh"), status,
                 {"code": str(error.get("code", "harness_error")), "message": error["message"]} if error else None,
                 {"reason": incomplete["reason"]} if incomplete else None, dict(metadata))


def message_item(rid: str, text: str, done: bool = True) -> dict[str, Any]:
    return {"type": "message", "id": f"msg_{rid[5:29]}", "role": "assistant", "status": "completed" if done else "in_progress",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


# the response object
def response(*, rid: str, created: float, status: str, model: str, previous: str | None, store: bool,
             instructions: Any, metadata: dict[str, str], output: list[dict[str, Any]], usage: dict[str, Any] | None,
             error: dict[str, str] | None, incomplete: dict[str, str] | None, completed: float | None = None) -> dict[str, Any]:
    """The Response, validated against the SDK: the standard shape, the package's items as its output."""
    metadata = dict(metadata)
    if error and error.get("code") not in ERROR_CODES:            # the SDK knows a fixed list; the package's own code rides in metadata
        metadata["error_code"] = str(error.get("code", "harness_error"))
        error = {"code": "server_error", "message": error.get("message", "")}
    if incomplete and incomplete.get("reason") not in INCOMPLETE_REASONS:
        metadata["incomplete_reason"] = str(incomplete.get("reason", ""))
        incomplete = None
    doc = {"id": rid, "object": "response", "created_at": int(created), "status": status, "model": model,
           "previous_response_id": previous, "store": store, "instructions": instructions if isinstance(instructions, (str, list)) else None,
           "metadata": metadata, "output": output, "usage": usage, "error": error, "incomplete_details": incomplete,
           "parallel_tool_calls": False, "tool_choice": "none", "tools": [], "temperature": None, "top_p": None,
           "truncation": "disabled", "text": {"format": {"type": "text"}},
           "completed_at": int(completed) if completed and status in STATUSES else None}
    try:
        return R.Response.model_validate(doc).model_dump(mode="json", exclude_none=True, warnings=False)
    except ValidationError as e:
        raise Invalid(f"the response did not validate: {e.errors()[0].get('msg')}") from None


def sse(kind: str, n: int, **data: Any) -> str:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, 'sequence_number': n, **data}, ensure_ascii=False)}\n\n"


async def stream(partial: dict[str, Any], events: AsyncIterator[dict[str, Any]], ended: Callable[[], Awaitable[dict[str, Any]]]) -> AsyncIterator[str]:
    """The standard event stream: the response created and in progress, every event the package
    printed with its sequence number, then the final event with the response as it ended. A package's
    own envelope events are dropped: the platform owns the envelope."""
    n = 0

    def event(kind: str, **data: Any) -> str:
        nonlocal n
        n += 1
        return sse(kind, n, **data)

    yield event("response.created", response=partial)
    yield event("response.in_progress", response=partial)
    async for e in events:
        kind = e.get("type", "")
        if kind in ENVELOPE or not kind.startswith("response."):
            continue
        n += 1
        yield f"event: {kind}\ndata: {json.dumps({**e, 'sequence_number': n}, ensure_ascii=False)}\n\n"
    final = await ended()
    yield event(FINAL.get(final.get("status", "failed"), "response.failed"), response=final)


def now() -> float:
    return time.time()
