"""The Responses API as JSON Schemas, generated from the OpenAI SDK this platform pins.

Three are OpenAI's own, with nothing added and nothing removed: a request, the response a caller receives, and the
events of its stream. The fourth is the platform's: one line a harness package's `run` may print, which is an
output item or a stream event of those schemas with what the platform fills in left optional, or the stream's last
event with only what a harness knows of the response.

The files are kept in the repository's `contract/`, beside the contract in words, so that a change of the pin
shows as a change of them. `harnessgateway schema` writes them; a test fails when they are not what the pin gives.
"""

import json
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import Any

from openai.types.responses import Response, ResponseCreateParams, ResponseStreamEvent
from pydantic import TypeAdapter
from pydantic.json_schema import models_json_schema

from harnessgateway.domain.events import ENDINGS, EVENTS, FLOWS, INCOMPLETE, ITEMS, NUMBERS, SAID

SDK = f"openai {version('openai')}"            # the pin: what "the Responses API" means to this platform
# What a harness says of the response in the stream's last event.
_RESPONSE = {
    "usage": {"type": "object", "required": ["input_tokens", "output_tokens"],
              "properties": {"input_tokens": {"type": "integer"}, "output_tokens": {"type": "integer"}}},
    "incomplete_details": {"type": "object", "required": ["reason"],
                           "properties": {"reason": {"enum": list(INCOMPLETE)}}},
    "error": {"type": "object", "required": ["message"],
              "properties": {"message": {"type": "string"}, "code": {"type": "string"}}},
}


def request() -> dict[str, Any]:
    """A request, as OpenAI's SDK describes one."""
    return TypeAdapter(ResponseCreateParams).json_schema()


def response() -> dict[str, Any]:
    """The response a caller receives, as OpenAI's SDK describes it."""
    return Response.model_json_schema()


def stream() -> dict[str, Any]:
    """An event of a response's stream, as OpenAI's SDK describes them."""
    return TypeAdapter(ResponseStreamEvent).json_schema()


def _ending(kind: str) -> dict[str, Any]:
    said, needed = SAID[kind], [name for name in SAID[kind] if name != "usage"]
    return {"type": "object", "required": ["type", *(["response"] if needed else [])], "properties": {
        "type": {"const": kind},
        "response": {"type": "object", "required": needed, "properties": {name: _RESPONSE[name] for name in said}}}}


def lines() -> dict[str, Any]:
    """One line a `run` may print: an output item, a stream event, or the stream's last event. What the platform
    fills in is not required of a line: an item's id, an event's numbers, and the item a piece of text belongs to."""
    models = [*ITEMS.values(), *EVENTS.values()]
    named, made = models_json_schema([(model, "validation") for model in models])
    definitions = made["$defs"]

    def filled_in(model: type, *fields: str) -> None:
        definition = definitions[named[(model, "validation")]["$ref"].rpartition("/")[2]]
        definition["required"] = [name for name in definition.get("required", ()) if name not in fields]
    for model in ITEMS.values():
        filled_in(model, "id")
    for model in EVENTS.values():
        filled_in(model, *NUMBERS)
    for flow in FLOWS.values():
        filled_in(EVENTS[f"{flow.text}.delta"], "item_id", flow.index, *flow.extra)
    return {"anyOf": [*(named[(model, "validation")] for model in models), *(_ending(kind) for kind in ENDINGS)],
            "$defs": definitions}


# Each file of the contract's schemas: what it is, and what makes it.
FILES: dict[str, tuple[str, Callable[[], dict[str, Any]]]] = {
    "request.schema.json": ("A request to POST /v1/responses, as OpenAI's SDK describes it: unchanged.", request),
    "response.schema.json": ("The response a caller receives, as OpenAI's SDK describes it: unchanged.", response),
    "stream.schema.json": ("An event of a response's stream, as OpenAI's SDK describes them: unchanged.", stream),
    "lines.schema.json": ("One line a harness package's run may print. The platform's own rule: OpenAI's output "
                          "items and stream events with what the platform fills in left optional, or the stream's "
                          "last event with only what a harness knows of the response.", lines),
}


def files() -> dict[str, str]:
    """Every schema file, as it is kept: named for the pin it came from, and written one way, so that two
    generations from one pin are the same bytes."""
    return {name: json.dumps({"$comment": f"{what} Generated from {SDK} by `harnessgateway schema`.", **make()},
                             indent=1, sort_keys=True) + "\n" for name, (what, make) in FILES.items()}


def write(folder: Path) -> list[str]:
    """Write the schema files into a folder. The names of those that changed."""
    changed = []
    for name, text in files().items():
        path = folder / name
        if not path.is_file() or path.read_text() != text:
            path.write_text(text)
            changed.append(name)
    return changed
