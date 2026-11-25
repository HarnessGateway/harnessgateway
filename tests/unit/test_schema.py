"""The Responses API as schemas: generated from the pinned SDK, kept in the repository, and never out of date."""

import json

from harnessgateway import shipped
from harnessgateway.domain import schema

CONTRACT = shipped.contract()


def test_the_kept_schemas_are_what_the_pin_gives():
    """Change the pin, and this fails until `harnessgateway schema` has been run: the change to the API is then
    there to be read, in the same commit."""
    kept = {path.name: path.read_text() for path in CONTRACT.glob("*.schema.json")}
    assert kept == schema.files(), "run: harnessgateway schema"
    assert all(schema.SDK in json.loads(text)["$comment"] for text in kept.values())


def test_the_three_are_openais_own_with_nothing_added():
    request, response, stream = schema.request(), schema.response(), schema.stream()
    assert "required" not in request["$defs"]["ResponseCreateParamsNonStreaming"]      # as OpenAI has it: nothing added
    assert {"conversation", "prompt", "model", "input"} <= set(request["$defs"]["ResponseCreateParamsNonStreaming"]["properties"])
    assert {"id", "output", "status", "usage", "completed_at"} <= set(response["properties"]) and len(stream["anyOf"]) == 59
