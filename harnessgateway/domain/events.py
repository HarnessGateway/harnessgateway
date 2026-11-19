"""The lines a package's `run` prints, and how they are read, numbered and validated (spec §8.7)."""

import json
import re
import typing
from dataclasses import dataclass
from typing import Any, Literal

from openai.types.responses import ResponseStreamEvent, ResponseUsage
from pydantic import BaseModel, ValidationError

from harnessgateway.domain.definition import describe
from harnessgateway.domain.outcome import Terminal

LINE_MAX = 16 * 1024 * 1024
MESSAGE_MAX = 4000

_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_INCOMPLETE_REASONS = ("max_output_tokens", "content_filter")
_RESPONSE_LEVEL = frozenset({
    "response.created", "response.queued", "response.in_progress", "response.completed", "response.failed",
    "response.incomplete", "error",
})
_TERMINAL_KEYS = {
    "hg.done": {"type", "usage"},
    "hg.incomplete": {"type", "reason", "usage"},
    "hg.failed": {"type", "code", "message", "usage"},
}


def _event_models() -> dict[str, type[BaseModel]]:
    """The OpenAI SDK's own type for each stream event, by the event's name."""
    union = typing.get_args(ResponseStreamEvent)[0]
    return {typing.get_args(model.model_fields["type"].annotation)[0]: model for model in typing.get_args(union)}


def _usage_details() -> dict[str, dict[str, int]]:
    """Each detail object of the SDK's usage type, with every count it requires at zero."""
    details = {}
    for name, field in ResponseUsage.model_fields.items():
        model = field.annotation
        if isinstance(model, type) and issubclass(model, BaseModel):
            details[name] = {key: 0 for key, sub in model.model_fields.items() if sub.is_required()}
    return details


EVENT_MODELS = _event_models()
_USAGE_DETAILS = _usage_details()
OUTPUT_EVENTS = frozenset(EVENT_MODELS) - _RESPONSE_LEVEL


class LineError(Exception):
    """A printed line is not one the contract allows. The turn fails with `invalid_output`."""


@dataclass(frozen=True)
class Line:
    kind: Literal["event", "terminal", "log"]
    event: dict[str, Any] | None = None
    terminal: Terminal | None = None
    text: str | None = None


def normalize_usage(usage: object) -> dict[str, Any]:
    """`usage` in the full shape of the Responses usage object, with what was left out filled in."""
    if not isinstance(usage, dict):
        raise LineError("usage is an object")
    try:
        full = {
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "total_tokens": usage.get("total_tokens", usage["input_tokens"] + usage["output_tokens"]),
            **{name: {**zeros, **usage.get(name, {})} for name, zeros in _USAGE_DETAILS.items()},
        }
        ResponseUsage.model_validate(full, strict=True)
    except (KeyError, TypeError) as e:
        raise LineError(f"usage needs input_tokens and output_tokens as numbers: {e}") from None
    except ValidationError as e:
        raise LineError(f"usage: {describe(e)}") from None
    return full


def _terminal(kind: str, obj: dict[str, Any]) -> Terminal:
    extra = set(obj) - _TERMINAL_KEYS[kind]
    if extra:
        raise LineError(f"{kind} has no field {sorted(extra)[0]!r}")
    usage = normalize_usage(obj["usage"]) if "usage" in obj else None
    if kind == "hg.done":
        return Terminal(kind="done", usage=usage)
    if kind == "hg.incomplete":
        if obj.get("reason") not in _INCOMPLETE_REASONS:
            raise LineError(f"hg.incomplete has a reason, one of {', '.join(_INCOMPLETE_REASONS)}")
        return Terminal(kind="incomplete", reason=obj["reason"], usage=usage)
    code, message = obj.get("code"), obj.get("message")
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise LineError("hg.failed has a code: lower-case letters, digits and '_', starting with a letter")
    if not isinstance(message, str) or not message:
        raise LineError("hg.failed has a message that says what went wrong")
    return Terminal(kind="failed", code=code, message=message[:MESSAGE_MAX], usage=usage)


def storable(value: Any) -> Any:
    """The same value with no NUL character in any of its strings: each becomes U+FFFD. An agent that prints a
    binary file prints them and JSON carries them, but the database holds neither text nor JSON with one."""
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd")
    if isinstance(value, dict):
        return {storable(key): storable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [storable(item) for item in value]
    return value


def read_line(text: str) -> Line:
    """What one printed line is; or a refusal that says why it is not allowed."""
    if len(text) > LINE_MAX:
        raise LineError(f"a line is at most {LINE_MAX >> 20} MiB")
    try:
        obj = storable(json.loads(text))
    except json.JSONDecodeError:
        raise LineError(f"not JSON: {text[:80]!r}") from None
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        raise LineError(f"a line is a JSON object with a type: {text[:80]!r}")
    kind = obj["type"]
    if kind in _TERMINAL_KEYS:
        return Line("terminal", terminal=_terminal(kind, obj))
    if kind == "hg.log":
        if set(obj) != {"type", "text"} or not isinstance(obj["text"], str):
            raise LineError("hg.log has a text, and nothing else")
        return Line("log", text=obj["text"])
    if kind in _RESPONSE_LEVEL:
        raise LineError(f"{kind} is written by the platform, not by a package")
    if kind not in OUTPUT_EVENTS:
        raise LineError(f"{kind!r} is not an event of the Responses stream")
    return Line("event", event=obj)


# An item's text arrives in pieces. The events that carry a piece, and where an item of each type keeps its text.
_PIECES = frozenset({"response.output_text.delta", "response.reasoning_summary_text.delta"})
_TEXT_PART: dict[str, tuple[str, dict[str, Any]]] = {
    "message": ("content", {"type": "output_text", "annotations": []}),
    "reasoning": ("summary", {"type": "summary_text"}),
}


class OutputEvents:
    """One turn's output events: numbered in order, validated, and gathered into the response's output."""

    def __init__(self, first_sequence_number: int) -> None:
        self.next_sequence_number = first_sequence_number
        self._done: list[tuple[int, dict[str, Any]]] = []
        self._begun: dict[str, tuple[int, dict[str, Any], list[str]]] = {}   # by id: place, item, pieces

    def accept(self, event: dict[str, Any]) -> dict[str, Any]:
        """The event with its sequence number, if the OpenAI SDK's type for it accepts it."""
        if "sequence_number" in event:
            raise LineError("a package does not set sequence_number: the platform numbers events")
        numbered = {**event, "sequence_number": self.next_sequence_number}
        try:
            EVENT_MODELS[event["type"]].model_validate(numbered)
        except ValidationError as e:
            raise LineError(f"{event['type']}: {describe(e)}") from None
        self.next_sequence_number += 1
        self._gather(event)
        return numbered

    def _gather(self, event: dict[str, Any]) -> None:
        kind = event["type"]
        if kind == "response.output_item.added":
            self._begun[event["item"]["id"]] = (event["output_index"], event["item"], [])
        elif kind == "response.output_item.done":
            self._begun.pop(event["item"]["id"], None)
            self._done.append((event["output_index"], event["item"]))
        elif kind in _PIECES and event["item_id"] in self._begun:
            self._begun[event["item_id"]][2].append(event["delta"])

    def output(self) -> list[dict[str, Any]]:
        """The finished items, in the order of their place in the output. Text that was cut short (the run was
        stopped or killed while printing it) is there too, as an item that is incomplete."""
        cut_short = []
        for index, item, pieces in self._begun.values():
            if pieces and item["type"] in _TEXT_PART:
                parts, part = _TEXT_PART[item["type"]]
                cut_short.append((index, {**item, "status": "incomplete", parts: [{**part, "text": "".join(pieces)}]}))
        return [item for _, item in sorted([*self._done, *cut_short], key=lambda pair: pair[0])]


class RunOutput:
    """Everything one run prints, read a line at a time (spec §8.7)."""

    def __init__(self, first_sequence_number: int) -> None:
        self._events = OutputEvents(first_sequence_number)
        self.terminal: Terminal | None = None

    @property
    def next_sequence_number(self) -> int:
        return self._events.next_sequence_number

    def read(self, text: str) -> Line | None:
        """What a printed line is, with an event numbered and validated. None once the terminal line has come:
        whatever is printed after it is ignored. A line that is not allowed raises LineError."""
        if self.terminal is not None:
            return None
        line = read_line(text)
        if line.terminal is not None:
            self.terminal = line.terminal
        if line.event is not None:
            return Line("event", event=self._events.accept(line.event))
        return line

    def output(self) -> list[dict[str, Any]]:
        return self._events.output()
