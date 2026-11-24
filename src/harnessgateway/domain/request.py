"""A Responses request: what is accepted, and what the platform reads out of it. Nothing is added to the API: a
body is checked against the OpenAI SDK's own description of the request, and reaches the harness as it was sent,
which makes of each field what its agent can.

What is wrong is refused: a field the API does not have, a value of the wrong kind. What is left out is not. The
SDK's types call fields required that OpenAI's server fills in when they are missing (an image's `detail`), so a
request OpenAI answers would be refused here for lacking one. Only `type`, which says what a thing is, is asked
of everything that has one."""

import copy
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from openai.types.responses.response_create_params import ResponseCreateParamsBase
from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic_core import ErrorDetails, SchemaValidator

from harnessgateway.domain.egress import BoundSecret, HostEntry
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


def _telling(errors: list[ErrorDetails]) -> tuple[str, ErrorDetails]:
    """Of everything wrong with a body, the one worth saying, and where it is. A value that may be one of several
    things is reported once for each of them: those whose `type` is another's are not what the caller meant, and
    of the rest the deepest is the most exact."""
    def about_type(error: ErrorDetails) -> bool:
        return error["loc"][-1:] == ("type",) and error["type"] in ("literal_error", "missing")
    others = {error["loc"][:-1] for error in errors if about_type(error) and len(error["loc"]) > 1}
    meant = [error for error in errors if not any(error["loc"][:len(other)] == other for other in others)] or errors
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
    try:
        _BODY.validate_json(json.dumps(body), strict=True)     # as JSON, strictly: "yes" is not true
    except ValidationError as error:
        where, found = _telling(error.errors())
        field = where.partition(".")[0] or None
        if found["type"] == "extra_forbidden":
            raise Invalid(f"{where} is not a field of the Responses API", param=field) from None
        raise Invalid(f"{where}: {found['msg']}", param=field) from None
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
