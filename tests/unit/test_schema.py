"""The Responses API as schemas: generated from the pinned SDK, kept in the repository, and never out of date."""

import json

from harnessgateway import shipped
from harnessgateway.domain import schema
from harnessgateway.domain.events import ENDINGS, EVENTS, ITEMS

CONTRACT = shipped.folder("contract")


def test_the_kept_schemas_are_what_the_pin_gives():
    """Change the pin, and this fails until `harnessgateway schema` has been run: the change to the API is then
    there to be read, in the same commit."""
    kept = {path.name: path.read_text() for path in CONTRACT.glob("*.schema.json")}
    assert kept == schema.files(), "run: harnessgateway schema"
    assert all(schema.SDK in json.loads(text)["$comment"] for text in kept.values())


def test_three_are_openais_own_and_one_is_the_platforms():
    request, response, stream = schema.request(), schema.response(), schema.stream()
    assert "required" not in request["$defs"]["ResponseCreateParamsNonStreaming"]      # as OpenAI has it: nothing added
    assert {"conversation", "prompt", "model", "input"} <= set(request["$defs"]["ResponseCreateParamsNonStreaming"]["properties"])
    assert {"id", "output", "status", "usage", "completed_at"} <= set(response["properties"]) and len(stream["anyOf"]) == 59
    lines = schema.lines()
    kinds = {lines["$defs"][option["$ref"].rpartition("/")[2]]["properties"]["type"]["const"] if "$ref" in option
             else option["properties"]["type"]["const"] for option in lines["anyOf"]}
    assert kinds == {*ITEMS, *EVENTS, *ENDINGS} and "response.created" not in kinds
    message = lines["$defs"]["ResponseOutputMessage"]             # what the platform fills in is not asked of a line
    assert "id" not in message["required"] and "id" in response["$defs"]["ResponseOutputMessage"]["required"]
