"""The OpenAI Responses wire, both directions, by the OpenAI SDK's own types (SPEC §4). Nothing here is a HarnessGateway
schema.

In: a body is checked against `ResponseCreateParams`; the platform reads only what routes and secures a turn
(`Routing`). Out: each stdout line of a package is a Responses stream event, a log line or the terminal event; the
reader is hgkit's (`hgkit.validate`), the one validator packages and the platform share. The platform owns the envelope:
`response.created`, `response.in_progress`, sequence numbers and the final `Response`."""
from __future__ import annotations

import json
import typing
from dataclasses import dataclass
from typing import Any

from hgkit.validate import (  # noqa: F401  (re-exported: the services read package output through this module)
    DELTAS,
    ENVELOPE,
    EVENT_MODELS,
    ITEM_MODELS,
    TERMINAL,
    Line,
    Outcome,
    OutputError,
    check,
    check_item,
    first_error,
    outcome,
    read_line,
)
from openai.types import responses as R
from pydantic import TypeAdapter, ValidationError

from ..errors import Invalid
from ..limits import INPUT_FILES_MAX
from .names import NAME

PARAMS: TypeAdapter[Any] = TypeAdapter(R.ResponseCreateParams)
TERMINAL_EVENT = {"completed": "response.completed", "incomplete": "response.incomplete", "failed": "response.failed",
                  "cancelled": "response.failed"}
FINAL = frozenset({"completed", "incomplete", "failed", "cancelled"})
_ECHO = ("instructions", "temperature", "top_p", "tools", "tool_choice", "parallel_tool_calls", "text", "reasoning",
         "max_output_tokens", "max_tool_calls", "truncation", "store", "background", "previous_response_id", "user",
         "safety_identifier", "prompt_cache_key", "service_tier", "top_logprobs")
_ERROR_CODES = frozenset(typing.get_args(R.response_error.ResponseError.model_fields["code"].annotation))
METADATA_MAX = 16
_first = first_error
check_output = check              # a package's whole stdout, read as the platform reads it: what package tests call


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


def history_entry(rid: str, status: str, body: dict[str, Any], response: dict[str, Any] | None) -> dict[str, Any]:
    """One earlier turn as a package sees it in the session's history (SPEC §5.6): its input items with any file
    bytes left out, and the output items it produced, both in Responses form."""
    inputs = []
    for item in input_items(body):
        item = dict(item)
        if isinstance(item.get("content"), list):
            item["content"] = [_without_bytes(p) for p in item["content"]]
        inputs.append(item)
    return {"id": rid, "status": status, "input": inputs, "output": list((response or {}).get("output") or [])}


def _without_bytes(part: Any) -> Any:
    if isinstance(part, dict) and part.get("type") in ("input_file", "input_image"):
        return {k: v for k, v in part.items()
                if k != "file_data" and not (k == "image_url" and isinstance(v, str) and v.startswith("data:"))}
    return part


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
