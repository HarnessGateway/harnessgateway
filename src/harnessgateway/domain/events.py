"""What a harness's `run` prints, read a line at a time.

A line is one of three things: a Responses stream event (unnumbered, and never one of the response-level events,
which are the platform's to write), a line for the turn's log, or the one terminal line that says how the run
ended. Events are checked against the OpenAI SDK's own description of each, so what reaches a caller is always
the wire format, whatever the harness.
"""

import json
import re
import typing
from dataclasses import dataclass
from typing import Any

from openai.types.responses import ResponseStreamEvent, ResponseUsage
from pydantic import BaseModel, ValidationError

from harnessgateway.domain.outcome import Terminal

LINE_MAX = 16 * 1024 * 1024
MESSAGE_MAX = 4000
FIRST_NUMBER = 2                               # response.created is 0 and response.in_progress is 1
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_INCOMPLETE = ("max_output_tokens", "content_filter")
_PLATFORMS = frozenset({"response.created", "response.queued", "response.in_progress", "response.completed",
                        "response.failed", "response.incomplete", "error"})
_TERMINAL_FIELDS = {"hg.done": {"type", "usage"}, "hg.incomplete": {"type", "reason", "usage"},
                    "hg.failed": {"type", "code", "message", "usage"}}
# The SDK's model of each stream event, by the event's `type`.
_MODELS: dict[str, type[BaseModel]] = {
    typing.get_args(model.model_fields["type"].annotation)[0]: model
    for model in typing.get_args(typing.get_args(ResponseStreamEvent)[0])}
# Each detail object of the SDK's usage type, with every count it requires at zero: a harness reports the two
# totals, and the rest is filled in.
_USAGE_DETAILS = {name: {key: 0 for key, sub in field.annotation.model_fields.items() if sub.is_required()}
                  for name, field in ResponseUsage.model_fields.items()
                  if isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel)}
# Text arrives in pieces. If a run is cut short, what arrived is still the answer so far: where each kind of
# item keeps its text, and the part it is kept as.
_PIECES: dict[str, tuple[str, dict[str, Any]]] = {
    "response.output_text.delta": ("content", {"type": "output_text", "annotations": []}),
    "response.reasoning_summary_text.delta": ("summary", {"type": "summary_text"})}


class LineError(Exception):
    """A printed line is not one the contract allows."""


@dataclass(frozen=True)
class Event:
    data: dict[str, Any]                       # a stream event, numbered


@dataclass(frozen=True)
class Log:
    text: str


def storable(value: Any) -> Any:
    """The same value with no NUL character in any string: an agent that prints a binary file prints them, JSON
    carries them, and PostgreSQL holds neither text nor JSON with one."""
    if isinstance(value, str):
        return value.replace("\x00", "�")
    if isinstance(value, dict):
        return {storable(key): storable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [storable(item) for item in value]
    return value


def _usage(reported: object) -> dict[str, Any]:
    try:
        assert isinstance(reported, dict)
        full = {"input_tokens": reported["input_tokens"], "output_tokens": reported["output_tokens"],
                "total_tokens": reported.get("total_tokens", reported["input_tokens"] + reported["output_tokens"]),
                **{name: {**zeros, **reported.get(name, {})} for name, zeros in _USAGE_DETAILS.items()}}
        ResponseUsage.model_validate(full, strict=True)
    except (AssertionError, KeyError, TypeError, ValidationError):
        raise LineError("usage is an object with input_tokens and output_tokens as numbers") from None
    return full


def _terminal(kind: str, line: dict[str, Any]) -> Terminal:
    if extra := sorted(set(line) - _TERMINAL_FIELDS[kind]):
        raise LineError(f"{kind} has no field {extra[0]!r}")
    usage = _usage(line["usage"]) if "usage" in line else None
    if kind == "hg.done":
        return Terminal("done", usage=usage)
    if kind == "hg.incomplete":
        if line.get("reason") not in _INCOMPLETE:
            raise LineError(f"hg.incomplete has a reason, one of {', '.join(_INCOMPLETE)}")
        return Terminal("incomplete", reason=line["reason"], usage=usage)
    code, message = line.get("code"), line.get("message")
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise LineError("hg.failed has a code: lower-case letters, digits and '_', starting with a letter")
    if not isinstance(message, str) or not message:
        raise LineError("hg.failed has a message that says what went wrong")
    return Terminal("failed", code=code, message=message[:MESSAGE_MAX], usage=usage)


class RunOutput:
    """Everything one run prints: its events numbered and checked, its items gathered into the response's output."""

    def __init__(self) -> None:
        self.terminal: Terminal | None = None
        self.events = 0                            # how many events it has printed
        self._done: dict[int, dict[str, Any]] = {}                         # finished items, by place in the output
        self._open: dict[str, tuple[int, dict[str, Any]]] = {}             # unfinished ones, by id: place, item
        self._text: dict[str, tuple[str, dict[str, Any], list[str]]] = {}  # their text so far: field, part, pieces

    def read(self, text: str) -> Event | Log | Terminal | None:
        """What a printed line is. None once the terminal line has come: whatever follows it is ignored. A line
        the contract does not allow raises LineError."""
        if self.terminal is not None:
            return None
        if len(text) > LINE_MAX:
            raise LineError(f"a line is at most {LINE_MAX >> 20} MiB")
        try:
            line = storable(json.loads(text))
        except ValueError:
            raise LineError(f"not JSON: {text[:80]!r}") from None
        if not isinstance(line, dict) or not isinstance(line.get("type"), str):
            raise LineError(f"a line is a JSON object with a type: {text[:80]!r}")
        kind = line["type"]
        if kind in _TERMINAL_FIELDS:
            self.terminal = _terminal(kind, line)
            return self.terminal
        if kind == "hg.log":
            if set(line) != {"type", "text"} or not isinstance(line["text"], str):
                raise LineError("hg.log has a text, and nothing else")
            return Log(line["text"])
        if kind in _PLATFORMS:
            raise LineError(f"{kind} is written by the platform, not by a harness")
        if kind not in _MODELS:
            raise LineError(f"{kind!r} is not an event of the Responses stream")
        if "sequence_number" in line:
            raise LineError("a harness does not number its events: the platform does")
        numbered = {**line, "sequence_number": FIRST_NUMBER + self.events}
        try:
            _MODELS[kind].model_validate(numbered)
        except ValidationError as error:
            first = error.errors()[0]
            raise LineError(f"{kind}: {'.'.join(map(str, first['loc']))}: {first['msg']}") from None
        self.events += 1
        self._gather(line)
        return Event(numbered)

    def _gather(self, event: dict[str, Any]) -> None:
        kind = event["type"]
        if kind == "response.output_item.added":
            self._open[event["item"]["id"]] = (event["output_index"], event["item"])
        elif kind == "response.output_item.done":
            self._open.pop(event["item"]["id"], None)
            self._done[event["output_index"]] = event["item"]
        elif kind in _PIECES and event["item_id"] in self._open:
            field, part = _PIECES[kind]
            self._text.setdefault(event["item_id"], (field, part, []))[2].append(event["delta"])

    def output(self) -> list[dict[str, Any]]:
        """The response's output: its finished items in their places, and the text of any that a stop or a kill
        cut short, as an item that is incomplete."""
        items = dict(self._done)
        for item_id, (index, item) in self._open.items():
            if item_id in self._text:
                field, part, pieces = self._text[item_id]
                items[index] = {**item, "status": "incomplete", field: [{**part, "text": "".join(pieces)}]}
        return [items[index] for index in sorted(items)]
