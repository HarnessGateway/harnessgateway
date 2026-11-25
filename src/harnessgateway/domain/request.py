"""A Responses request: what is accepted, and what the platform reads out of it. Nothing is added to the API: a
body is checked against the OpenAI SDK's own description of the request, and reaches the harness as it was sent,
which makes of each field what its agent can.

What is wrong is refused: a field the API does not have, a value of the wrong kind. What is left out is not. The
SDK's types call fields required that OpenAI's server fills in when they are missing (an image's `detail`), so a
request OpenAI answers would be refused here for lacking one. Only `type`, which says what a thing is, is asked
of everything that has one.

An item a response held is taken back as it was. The SDK gives some output items a field their input form does not
have (a tool's output says its `status`), so a caller that keeps the conversation, and sends back what it was sent,
would be refused for the platform's own words."""

import copy
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from openai.types.responses.response_create_params import ResponseCreateParamsBase
from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic_core import ErrorDetails, SchemaValidator

from harnessgateway.domain.egress import BoundSecret, HostEntry
from harnessgateway.domain.events import ITEMS
from harnessgateway.errors import Invalid

METADATA_MAX = 16                              # pairs, as the Responses API allows
OURS = "hg_"                                   # metadata keys the platform writes in a response
# Fields of the API that name state kept by OpenAI, which there is none of here.
_NOT_HERE = {"conversation": "a session is continued with previous_response_id",
             "prompt": "send the text itself, as input and instructions"}


class _Body(ResponseCreateParamsBase, total=False):
    __pydantic_config__ = ConfigDict(extra="forbid")  # type: ignore[misc]
    stream: bool                               # the SDK describes it apart, once for each of its two answers


def _leaving_out_allowed(schema: Any) -> Any:
    """The same schema, with no field but `type` required of anything. A list the SDK would read lazily (it
    takes any iterable from a program) is read here in full: a body is JSON, and what is wrong inside it is found
    now, not by whoever reads it later."""
    if isinstance(schema, list):
        return [_leaving_out_allowed(each) for each in schema]
    if not isinstance(schema, dict):
        return schema
    if schema.get("type") == "generator":
        schema = {**schema, "type": "list"}
    if schema.get("type") == "typed-dict":
        for name, field in schema["fields"].items():
            field["required"] = name == "type" and field.get("required", schema.get("total", True))
    return {key: _leaving_out_allowed(value) for key, value in schema.items()}


_BODY = SchemaValidator(_leaving_out_allowed(copy.deepcopy(TypeAdapter(_Body).core_schema)))
_KINDS = frozenset({"str", "int", "float", "bool", "dict", "list", "none"})     # what a union calls its plain members


def _meant(errors: list[ErrorDetails]) -> list[ErrorDetails]:
    """A value that may be one of several things is reported once for each of them: those whose `type` is another's
    are not what the caller meant."""
    def about_type(error: ErrorDetails) -> bool:
        return error["loc"][-1:] == ("type",) and error["type"] in ("literal_error", "missing")
    others = {error["loc"][:-1] for error in errors if about_type(error) and len(error["loc"]) > 1}
    return [error for error in errors if not any(error["loc"][:len(other)] == other for other in others)]


def _wrong(body: dict[str, Any]) -> list[ErrorDetails]:
    """What is wrong with a body, as the SDK's description of a request has it: nothing, when it is one."""
    try:
        _BODY.validate_json(json.dumps(body), strict=True)     # as JSON, strictly: "yes" is not true
    except ValidationError as error:
        return error.errors()
    return []


def _as_printed(body: dict[str, Any], errors: list[ErrorDetails]) -> dict[str, Any] | None:
    """The body without the fields of its input items that only a response's item of that type has; None when no
    such field is what was wrong."""
    given, only = body.get("input"), set()
    for error in _meant(errors):
        place = error["loc"]
        if error["type"] == "extra_forbidden" and len(place) > 3 and place[0] == "input" and isinstance(place[-3], int):
            item = given[place[-3]] if isinstance(given, list) and place[-3] < len(given) else None
            if isinstance(item, dict) and item.get("type") in ITEMS and place[-1] in ITEMS[item["type"]].model_fields:
                only.add((place[-3], place[-1]))
    if not only or not isinstance(given, list):
        return None
    return {**body, "input": [{name: value for name, value in item.items() if (number, name) not in only}
                              if isinstance(item, dict) else item for number, item in enumerate(given)]}


def _telling(errors: list[ErrorDetails]) -> tuple[str, ErrorDetails]:
    """Of everything wrong with a body, the one worth saying, and where it is: of what the caller meant, the
    deepest is the most exact."""
    meant = _meant(errors) or errors
    found = max(meant, key=lambda error: len(error["loc"]))
    where = [part for part in found["loc"] if isinstance(part, int) or (part.islower() and "[" not in part
                                                                         and part not in _KINDS)]
    return ".".join(str(part) for part in where), found


def validate(body: object) -> dict[str, Any]:
    """The body, as it was sent, if it is a request the platform accepts; or a refusal naming the field."""
    if not isinstance(body, dict):
        raise Invalid("the request body is a JSON object")
    for name in ("model", "input"):
        if body.get(name) is None:
            raise Invalid(f"{name} is required", param=name)
    for name, instead in _NOT_HERE.items():
        if body.get(name) is not None:
            raise Invalid(f"{name} is not supported: {instead}", param=name)
    errors, checked = _wrong(body), body
    while errors and (printed := _as_printed(checked, errors)) is not None:        # an item a response held is
        errors, checked = _wrong(printed), printed                                 # taken back as it was
    if errors:
        where, found = _telling(errors)
        field = where.partition(".")[0] or None
        if found["type"] == "extra_forbidden":
            raise Invalid(f"{where} is not a field of the Responses API", param=field)
        raise Invalid(f"{where}: {found['msg']}", param=field)
    metadata = body.get("metadata") or {}
    if len(metadata) > METADATA_MAX or any(key.startswith(OURS) for key in metadata):
        raise Invalid(f"metadata is at most {METADATA_MAX} pairs, none with a key beginning {OURS}: those are "
                      "the platform's to write", param="metadata")
    if body.get("store") is False and body.get("background"):
        raise Invalid("a background response is fetched later, so it must be stored: store cannot be false",
                      param="store")
    return body


def without_credentials(body: dict[str, Any], new_placeholder: Callable[[], str],
                        ) -> tuple[dict[str, Any], list[BoundSecret]]:
    """The request with a placeholder in place of every credential it carries, and each credential bound to where
    the request says it goes. A request's credentials are those of the MCP servers it names: their `headers` and
    their `authorization`, which may go to that server's host and nowhere else. The request is kept, shown and
    given to its harness with the placeholders; only the egress proxy is told the values."""
    bound: list[BoundSecret] = []

    def hidden(value: str, host: str) -> str:
        placeholder = new_placeholder()
        if host:                                   # a credential with nowhere to go is only forgotten
            bound.append(BoundSecret((host,), value, placeholder))
        return placeholder

    def clean(tool: Any) -> Any:
        if not isinstance(tool, dict) or tool.get("type") != "mcp":
            return tool
        try:
            url = urlsplit(tool.get("server_url") or "")
            host = str(HostEntry(url.hostname, url.port)) if url.hostname else ""
        except ValueError:                         # not an address at all
            host = ""
        cleaned = dict(tool)
        if isinstance(tool.get("authorization"), str):
            cleaned["authorization"] = hidden(tool["authorization"], host)
        if isinstance(tool.get("headers"), dict):
            cleaned["headers"] = {name: hidden(value, host) for name, value in tool["headers"].items()}
        return cleaned
    if not isinstance(body.get("tools"), list):
        return body, bound
    return {**body, "tools": [clean(tool) for tool in body["tools"]]}, bound


def _content(item: object) -> list[Any]:
    content = item.get("content") if isinstance(item, dict) else None
    return content if isinstance(content, list) else []


def file_ids(body: dict[str, Any]) -> list[str]:
    """The ids of the uploaded files a request's input names, each once, in the order they appear."""
    given = body.get("input")
    found = [part["file_id"] for item in (given if isinstance(given, list) else []) for part in _content(item)
             if isinstance(part, dict) and part.get("type") in ("input_file", "input_image")
             and isinstance(part.get("file_id"), str)]
    return list(dict.fromkeys(found))


def input_items(response_id: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    """A request's input as the list of items the API lists for a response, each with an id."""
    given = body.get("input")
    items = given if isinstance(given, list) else [
        {"role": "user", "content": [{"type": "input_text", "text": given}]}]
    stem = response_id.partition("_")[2]
    return [{"type": "message", "id": f"msg_{stem}_{number}", **item} for number, item in enumerate(items)]
