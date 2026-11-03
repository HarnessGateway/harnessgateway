"""The Responses wire, read the way the platform reads it: each stdout line of a package is a stream event (checked
against the OpenAI SDK model its `type` names), a log line, or the terminal event (SPEC §4.2).

This is the one validator. The platform imports it to accept or refuse a package's output; a package imports it to
check its own output first (`Emitter(validate=True)`, `check(text)`, `python3 -m hgkit check`). Both run the same
pinned OpenAI SDK, so what passes here passes there."""
from __future__ import annotations

import json
import typing
from dataclasses import dataclass, field
from typing import Any

from openai.types import responses as R
from pydantic import BaseModel, TypeAdapter, ValidationError

ITEMS: TypeAdapter[Any] = TypeAdapter(list[R.ResponseOutputItem])
USAGE: TypeAdapter[Any] = TypeAdapter(R.ResponseUsage)
ENVELOPE = frozenset({"response.created", "response.in_progress", "response.queued"})
TERMINAL = {"response.completed": "completed", "response.incomplete": "incomplete", "response.failed": "failed"}


def _by_type(union: Any) -> dict[str, type[BaseModel]]:
    out: dict[str, type[BaseModel]] = {}
    for model in typing.get_args(typing.get_args(union)[0]):
        for literal in typing.get_args(model.model_fields["type"].annotation):
            out[literal] = model
    return out


EVENT_MODELS = _by_type(R.ResponseStreamEvent)
ITEM_MODELS = _by_type(R.ResponseOutputItem)
DELTAS = frozenset(k for k in EVENT_MODELS if k.endswith(".delta"))


def first_error(e: ValidationError) -> str:
    err = e.errors()[0]
    where = ".".join(str(p) for p in err.get("loc", ()))
    return f"{where or 'value'}: {err.get('msg', 'invalid')}"


class OutputError(Exception):
    """A package printed something the Responses schema refuses; the turn fails with this message."""


def check_item(item: Any, where: str) -> None:
    kind = item.get("type") if isinstance(item, dict) else None
    model = ITEM_MODELS.get(kind) if isinstance(kind, str) else None
    if model is None:
        raise OutputError(f"{where}: {kind!r} is not a Responses output item type")
    try:
        model.model_validate(item)
    except ValidationError as e:
        raise OutputError(f"{where} ({kind}).{first_error(e)}") from None


def check_event(event: dict[str, Any]) -> None:
    """One non-terminal, non-envelope event as a dict: OutputError names what is wrong."""
    kind = event.get("type")
    model = EVENT_MODELS.get(kind) if isinstance(kind, str) else None
    if model is None:
        raise OutputError(f"{kind} is not a Responses stream event")
    if isinstance(event.get("item"), dict):
        check_item(event["item"], f"{kind}: item")
    try:
        model.model_validate({**event, "sequence_number": 0})
    except ValidationError as e:
        raise OutputError(f"{kind}: {first_error(e)}") from None


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
    check_event(event)
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
        raise OutputError(f"output: {first_error(e)}") from None
    usage = r.get("usage")
    if usage is not None:
        try:
            usage = USAGE.validate_python(usage).model_dump(mode="json")
        except ValidationError as e:
            raise OutputError(f"usage: {first_error(e)}") from None
    meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in meta.items()):
        raise OutputError("metadata maps strings to strings")

    def _dict(v: Any) -> dict[str, Any] | None:
        return v if isinstance(v, dict) else None
    return Outcome(TERMINAL[terminal["type"]], output, usage, _dict(r.get("error")), _dict(r.get("incomplete_details")),
                   meta)


def check(text: str) -> tuple[list[dict[str, Any]], Outcome]:
    """A package's whole stdout, read as the platform reads it: its events and its outcome. OutputError says what the
    platform would refuse, naming the field. What package tests run on recorded output."""
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
