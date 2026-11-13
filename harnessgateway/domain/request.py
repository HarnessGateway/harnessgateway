"""A Responses request body: what is accepted, and what the platform reads out of it (spec §13.3, §13.8)."""

import json
from typing import Any

from openai.types.responses.response_create_params import ResponseCreateParamsBase
from pydantic import ConfigDict, TypeAdapter, ValidationError

from harnessgateway.errors import Invalid

METADATA_MAX = 16
_REFUSED = {
    "conversation": "sessions are chosen with previous_response_id",
    "prompt": "send the text as input and instructions",
}


class _Body(ResponseCreateParamsBase, total=False):
    """The OpenAI SDK's own request type, with nothing unknown let through."""

    __pydantic_config__ = ConfigDict(extra="forbid")  # type: ignore[misc]
    stream: bool


_BODY = TypeAdapter(_Body)


def _metadata(metadata: object) -> None:
    def refuse(why: str) -> Invalid:
        return Invalid(f"metadata {why}", param="metadata")
    if not isinstance(metadata, dict):
        raise refuse("is an object of text to text")
    if len(metadata) > METADATA_MAX:
        raise refuse(f"holds at most {METADATA_MAX} pairs, not {len(metadata)}")
    for key, value in metadata.items():
        if not isinstance(value, str) or len(key) > 64 or len(value) > 512:
            raise refuse("keys are up to 64 characters and values are text of up to 512 characters")
        if key.startswith("hg_"):
            raise refuse(f"keys beginning hg_ are the platform's own: {key}")


def validate(body: object) -> dict[str, Any]:
    """The body, exactly as it was sent, if it is a request the platform accepts; or a refusal naming the field."""
    if not isinstance(body, dict):
        raise Invalid("the request body is a JSON object")
    for name in ("model", "input"):
        if name not in body:
            raise Invalid(f"{name} is required", param=name)
    for name, instead in _REFUSED.items():
        if body.get(name) is not None:
            raise Invalid(f"{name} is not supported: {instead}", param=name)
    try:
        _BODY.validate_json(json.dumps(body), strict=True)         # as JSON: "yes" is not true, "1" is not 1
    except ValidationError as e:
        first = e.errors()[0]
        field = str(first["loc"][0]) if first["loc"] else None
        if first["type"] == "extra_forbidden":
            raise Invalid(f"{field} is not a field of the Responses API", param=field) from None
        where = ".".join(str(part) for part in first["loc"])
        raise Invalid(f"{where}: {first['msg']}", param=field) from None
    if any(not isinstance(tool, dict) or tool.get("type") != "mcp" for tool in body.get("tools") or []):
        raise Invalid("only tools of type mcp are supported: the platform cannot hand a tool call back to the "
                      "caller in the middle of a turn", param="tools")
    if body.get("metadata") is not None:
        _metadata(body["metadata"])
    return body


def file_ids(body: dict[str, Any]) -> list[str]:
    """The ids of the uploaded files a request's input refers to, each once, in the order they appear."""
    found: dict[str, None] = {}
    given = body.get("input")
    for item in given if isinstance(given, list) else []:
        content = item.get("content") if isinstance(item, dict) else None
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get("type") in ("input_file", "input_image") \
                    and isinstance(part.get("file_id"), str):
                found[part["file_id"]] = None
    return list(found)
