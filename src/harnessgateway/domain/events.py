"""What a harness's `run` prints, read a line at a time.

A line is a piece of an OpenAI response, as the Responses API itself writes one: an output item, whole; a stream
event, for what arrives in pieces; or the stream's last event, which says how the run ended. There is no other
vocabulary. The bookkeeping is the platform's: it numbers the events, places the items, names an item that came
without an id, and keeps the item that text arriving by itself belongs to. Everything is checked against the
OpenAI SDK's own description of it, so what reaches a caller is always the wire format, whatever the harness.
"""

import json
import re
import typing
from dataclasses import dataclass, field
from typing import Any

from openai.types.responses import ResponseOutputItem, ResponseStreamEvent, ResponseUsage
from openai.types.responses.response import IncompleteDetails
from pydantic import BaseModel, ValidationError

from harnessgateway.domain.outcome import Terminal
from harnessgateway.ids import new_id

LINE_MAX = 16 * 1024 * 1024
MESSAGE_MAX = 4000
FIRST_NUMBER = 2                               # response.created is 0 and response.in_progress is 1
NUMBERS = ("sequence_number", "output_index")  # what the platform counts: whatever a harness gives for them is replaced
ADDED, DONE = "response.output_item.added", "response.output_item.done"
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _by_type(union: Any) -> dict[str, type[BaseModel]]:
    """The SDK's models of a union's members, by each one's `type`."""
    return {typing.get_args(model.model_fields["type"].annotation)[0]: model
            for model in typing.get_args(typing.get_args(union)[0])}


# The last event of a stream, and what a harness says in it of the response it ends.
SAID = {"response.completed": ("usage",), "response.incomplete": ("usage", "incomplete_details"),
        "response.failed": ("usage", "error")}
ENDINGS = tuple(SAID)
INCOMPLETE: tuple[str, ...] = typing.get_args(typing.get_args(IncompleteDetails.model_fields["reason"].annotation)[0])
_PLATFORMS = frozenset({"response.created", "response.queued", "response.in_progress", "error"})
ITEMS = _by_type(ResponseOutputItem)
EVENTS = {kind: model for kind, model in _by_type(ResponseStreamEvent).items() if kind not in {*ENDINGS, *_PLATFORMS}}
# Each detail object of the SDK's usage type, with every count it requires at zero: a harness reports the two
# totals and whatever else it knows, and the rest is filled in.
_USAGE_DETAILS = {name: {key: 0 for key, sub in found.annotation.model_fields.items() if sub.is_required()}
                  for name, found in ResponseUsage.model_fields.items()
                  if isinstance(found.annotation, type) and issubclass(found.annotation, BaseModel)}


@dataclass(frozen=True)
class Flow:
    """Text that arrives in pieces: the kind of item it is kept in, and the events that carry it."""

    item: dict[str, Any]                       # the item, as it begins
    ended: dict[str, Any]                      # what is different about it once it is finished
    field: str                                 # where the item keeps its parts
    part: dict[str, Any]                       # a part, with no text yet
    parts: str                                 # the events that begin and end a part: <parts>.added, <parts>.done
    text: str                                  # the events that carry a part's text: <text>.delta, <text>.done
    index: str                                 # what those events call a part's place in its item
    extra: dict[str, Any]                      # what else the text's events carry


FLOWS = {
    "message": Flow({"type": "message", "role": "assistant", "status": "in_progress"}, {"status": "completed"},
                    "content", {"type": "output_text", "text": "", "annotations": []},
                    "response.content_part", "response.output_text", "content_index", {"logprobs": []}),
    "reasoning": Flow({"type": "reasoning"}, {}, "summary", {"type": "summary_text", "text": ""},
                      "response.reasoning_summary_part", "response.reasoning_summary_text", "summary_index", {}),
}
PIECES = {f"{flow.text}.delta": flow for flow in FLOWS.values()}       # each event that is a piece of text


class LineError(Exception):
    """A printed line is not one the contract allows."""


@dataclass
class _Placed:
    """An item of the response's output, at its place in it."""

    index: int
    item: dict[str, Any]
    done: bool = False
    flow: Flow | None = None                   # the kind of text that has arrived for it,
    pieces: list[str] = field(default_factory=list)                    # and the text


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


def _check(model: type[BaseModel], value: object, what: str) -> None:
    try:
        model.model_validate(value)
    except ValidationError as error:
        first = error.errors()[0]
        raise LineError(f"{what}: {'.'.join(map(str, first['loc'])) or 'it'}: {first['msg']}") from None


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


def _ending(kind: str, line: dict[str, Any]) -> Terminal:
    """How a run says it ended: the stream's last event, with only what the harness knows of the response."""
    said = line.get("response", {})
    if set(line) - {"type", "response"} or not isinstance(said, dict):
        raise LineError(f"{kind} carries a response, and nothing else")
    if extra := sorted(set(said) - set(SAID[kind])):
        raise LineError(f"{kind}: response.{extra[0]} is the platform's to write: a harness says "
                        f"{' and '.join(SAID[kind])}")
    usage = _usage(said["usage"]) if said.get("usage") is not None else None
    if kind == "response.completed":
        return Terminal("completed", usage=usage)
    if kind == "response.incomplete":
        details = said.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        if reason not in INCOMPLETE:
            raise LineError(f"{kind}: response.incomplete_details.reason is one of {', '.join(INCOMPLETE)}")
        return Terminal("incomplete", reason=reason, usage=usage)
    error = said.get("error")
    code, message = (error.get("code"), error.get("message")) if isinstance(error, dict) else (None, None)
    if not isinstance(message, str) or not message:
        raise LineError(f"{kind}: response.error.message says what went wrong")
    if code is not None and not (isinstance(code, str) and _CODE.fullmatch(code)):
        raise LineError(f"{kind}: response.error.code is lower-case letters, digits and '_', starting with a letter")
    return Terminal("failed", code=code, message=message[:MESSAGE_MAX], usage=usage)


class RunOutput:
    """Everything one run prints: read into the events of its response's stream, and gathered into its output."""

    def __init__(self) -> None:
        self.terminal: Terminal | None = None
        self.events = 0                            # how many events have been made of what it printed
        self._placed: dict[str, _Placed] = {}      # the output's items by id, in their places
        self._flowing: tuple[Flow, str] | None = None      # text arriving by itself: its kind, and the item kept for it

    def read(self, text: str) -> list[dict[str, Any]]:
        """The events a printed line becomes, numbered. None once the run has said how it ended: whatever
        follows that is ignored. A line the contract does not allow raises LineError."""
        if self.terminal is not None:
            return []
        if len(text) > LINE_MAX:
            raise LineError(f"a line is at most {LINE_MAX >> 20} MiB")
        try:
            line = storable(json.loads(text))
        except ValueError:
            raise LineError(f"not JSON: {text[:80]!r}") from None
        if not isinstance(line, dict) or not isinstance(line.get("type"), str):
            raise LineError(f"a line is a JSON object with a type: {text[:80]!r}")
        kind = line["type"]
        if kind in _PLATFORMS:
            raise LineError(f"{kind} is written by the platform, not by a harness")
        if kind in PIECES and "item_id" not in line:
            return self._piece(PIECES[kind], line) if line.get("delta") != "" else []     # an empty piece is nothing
        events = self._settle()
        if kind in ENDINGS:
            self.terminal = _ending(kind, line)
        elif kind in ITEMS:
            events += self._whole(line)
        elif kind in EVENTS:
            events.append(self._event(kind, line))
        else:
            raise LineError(f"{kind!r} is neither an output item nor a stream event of the Responses API")
        return events

    def _piece(self, flow: Flow, line: dict[str, Any]) -> list[dict[str, Any]]:
        """A piece of text that names no item: it goes on the item being written, which begins here if there
        is none of its kind."""
        events = self._settle() if self._flowing and self._flowing[0] is not flow else []
        if self._flowing is None:
            item_id = new_id("item")
            self._flowing = (flow, item_id)
            events += [self._event(ADDED, {"item": {**flow.item, "id": item_id, flow.field: []}}),
                       self._event(f"{flow.parts}.added", {"item_id": item_id, flow.index: 0, "part": flow.part})]
        return [*events, self._event(line["type"], {**flow.extra, **line, "item_id": self._flowing[1], flow.index: 0})]

    def _settle(self) -> list[dict[str, Any]]:
        """End the item kept for text arriving by itself, if there is one: something else is being printed."""
        if self._flowing is None:
            return []
        (flow, item_id), self._flowing = self._flowing, None
        placed = self._placed[item_id]
        part = {**flow.part, "text": "".join(placed.pieces)}
        return [*self._ended(flow, item_id, 0, part),
                self._event(DONE, {"item": {**placed.item, **flow.ended, flow.field: [part]}})]

    def _ended(self, flow: Flow, item_id: str, place: int, part: dict[str, Any]) -> list[dict[str, Any]]:
        at = {"item_id": item_id, flow.index: place}
        return [self._event(f"{flow.text}.done", {**flow.extra, **at, "text": part["text"]}),
                self._event(f"{flow.parts}.done", {**at, "part": part})]

    def _whole(self, line: dict[str, Any]) -> list[dict[str, Any]]:
        """An item printed whole: it begins and ends here. The text of a message or of a reasoning is carried
        as a stream carries text, so that a caller reading the text's events reads it too."""
        item = {"id": new_id("item"), **line}
        _check(ITEMS[item["type"]], item, item["type"])
        flow = FLOWS.get(item["type"])
        if flow is None:
            return [self._event(ADDED, {"item": item}), self._event(DONE, {"item": item})]
        events = [self._event(ADDED, {"item": {**item, **flow.item, flow.field: []}})]
        for place, part in enumerate(item[flow.field]):
            if part["type"] == flow.part["type"]:
                at = {"item_id": item["id"], flow.index: place}
                events += [self._event(f"{flow.parts}.added", {**at, "part": {**part, "text": ""}}),
                           self._event(f"{flow.text}.delta", {**flow.extra, **at, "delta": part["text"]}),
                           *self._ended(flow, item["id"], place, part)]
        return [*events, self._event(DONE, {"item": item})]

    def _event(self, kind: str, said: dict[str, Any]) -> dict[str, Any]:
        """One event of the stream: placed, numbered, checked, and gathered into the output."""
        event = {**{name: value for name, value in said.items() if name not in NUMBERS}, "type": kind}
        placed = self._place(kind, event)
        if "output_index" in EVENTS[kind].model_fields:
            if placed is None:
                raise LineError(f"{kind} comes after the item it is about has been added")
            event["output_index"] = placed.index
        event["sequence_number"] = FIRST_NUMBER + self.events
        _check(EVENTS[kind], event, kind)
        self.events += 1
        if kind in PIECES and placed is not None:
            placed.flow = PIECES[kind]
            placed.pieces.append(event["delta"])
        return event

    def _place(self, kind: str, event: dict[str, Any]) -> _Placed | None:
        """The item an event is about: the one it carries or names, or else the one added last."""
        if kind in (ADDED, DONE):
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") not in ITEMS or not isinstance(item.get("id"), str):
                raise LineError(f"{kind} carries an output item, with its id")
            _check(ITEMS[item["type"]], item, item["type"])
            placed = self._placed.get(item["id"])
            if (placed is None) != (kind == ADDED) or (placed is not None and placed.done):
                raise LineError(f"{kind}: item {item['id']} is added once, and then done once")
            if placed is None:
                placed = self._placed[item["id"]] = _Placed(len(self._placed), item)
            else:
                placed.item, placed.done = item, True
            return placed
        if "item_id" in event:
            if event["item_id"] not in self._placed:
                raise LineError(f"{kind} names an item that has not been added: {str(event['item_id'])[:80]!r}")
            return self._placed[event["item_id"]]
        return next(reversed(self._placed.values()), None)

    def output(self) -> list[dict[str, Any]]:
        """The response's output: its finished items in their places, and the text of any that a stop or a kill
        cut short, as an item that is incomplete."""
        items = []
        for placed in self._placed.values():           # in the order they were added, which is their places
            if placed.done:
                items.append(placed.item)
            elif placed.flow is not None:
                part = {**placed.flow.part, "text": "".join(placed.pieces)}
                items.append({**placed.item, "status": "incomplete", placed.flow.field: [part]})
        return items
