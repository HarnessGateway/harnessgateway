"""What a harness may print, as a JSON Schema: the rules `events.py` reads lines by, in the form a package checks
its own output against, in whatever language it is written. The platform puts it beside every installed package.
"""

from functools import cache
from typing import Any

from pydantic.json_schema import models_json_schema

from harnessgateway.domain.events import ENDINGS, EVENTS, FLOWS, INCOMPLETE, ITEMS, NUMBERS, SAID

_NOTES = ("title", "description", "default")       # what a schema says for a reader, and a check has no use for
_WITHIN = ("anyOf", "items", "additionalProperties")
# What a harness says of the response in the stream's last event.
_RESPONSE = {
    "usage": {"type": "object", "required": ["input_tokens", "output_tokens"],
              "properties": {"input_tokens": {"type": "integer"}, "output_tokens": {"type": "integer"}}},
    "incomplete_details": {"type": "object", "required": ["reason"],
                           "properties": {"reason": {"enum": list(INCOMPLETE)}}},
    "error": {"type": "object", "required": ["message"],
              "properties": {"message": {"type": "string"}, "code": {"type": "string"}}},
}


def _bare(schema: Any) -> Any:
    """A schema with nothing but what a value is checked by."""
    if isinstance(schema, list):
        return [_bare(each) for each in schema]
    if not isinstance(schema, dict):
        return schema
    kept = {key: _bare(value) if key in _WITHIN else value for key, value in schema.items() if key not in _NOTES}
    if "properties" in schema:
        kept["properties"] = {name: _bare(each) for name, each in schema["properties"].items()}
    return kept


def _ending(kind: str) -> dict[str, Any]:
    said, needed = SAID[kind], [name for name in SAID[kind] if name != "usage"]
    return {"type": "object", "required": ["type", *(["response"] if needed else [])], "properties": {
        "type": {"const": kind},
        "response": {"type": "object", "required": needed, "properties": {name: _RESPONSE[name] for name in said}}}}


@cache
def lines() -> dict[str, Any]:
    """The schema of one printed line: an output item, a stream event, or the stream's last event. What the
    platform fills in is not required of a line: an item's id, an event's numbers, and the item a piece of text
    belongs to."""
    models = [*ITEMS.values(), *EVENTS.values()]
    named, made = models_json_schema([(model, "validation") for model in models])
    definitions = {name: _bare(schema) for name, schema in made["$defs"].items()}

    def filled_in(model: type, *fields: str) -> None:
        definition = definitions[named[(model, "validation")]["$ref"].rpartition("/")[2]]
        definition["required"] = [name for name in definition.get("required", ()) if name not in fields]
    for model in ITEMS.values():
        filled_in(model, "id")
    for model in EVENTS.values():
        filled_in(model, *NUMBERS)
    for flow in FLOWS.values():
        filled_in(EVENTS[f"{flow.text}.delta"], "item_id", flow.index, *flow.extra)
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "anyOf": [*(named[(model, "validation")] for model in models), *(_ending(kind) for kind in ENDINGS)],
            "$defs": definitions}
