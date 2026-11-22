"""A turn as the Responses API returns it: the response object, and the response-level events of its stream."""

from typing import Any, get_args

from openai.types.responses import Response, ResponseError
from openai.types.responses.response_create_params import ResponseCreateParamsBase
from pydantic import ValidationError

from harnessgateway.domain.outcome import Outcome
from harnessgateway.domain.request import OURS

# What a caller sent that a response reports back, where the caller sent it: every field the API's request and its
# response both have, but the ones the platform says for itself.
_OURS = {"model", "metadata", "tools", "tool_choice", "parallel_tool_calls"}
_ECHOED = sorted((set(ResponseCreateParamsBase.__annotations__) & set(Response.model_fields)) - _OURS)
_LAST_EVENT = {"completed": "response.completed", "failed": "response.failed", "incomplete": "response.incomplete",
               "cancelled": "response.incomplete"}       # the stream has no event of its own for a cancelled turn
LAST_EVENTS = frozenset(_LAST_EVENT.values())
_ERROR_CODES = get_args(ResponseError.model_fields["code"].annotation)     # the codes that API has


def response_object(*, id: str, session_id: str, created_at: int, completed_at: int | None, status: str, model: str,
                    request: dict[str, Any], output: list[dict[str, Any]], usage: dict[str, Any] | None,
                    outcome: Outcome | None) -> dict[str, Any]:
    """A turn as an OpenAI response object. What the platform has to say that the object has no field for goes
    in `metadata`, under keys of its own: the session, and why a turn did not complete. A harness's own code
    for a failure is the error's code when it is one the API has, and is in `metadata` either way."""
    ours = {"session": session_id, "reason": outcome.reason if outcome and outcome.status != "completed" else None,
            "code": outcome.code if outcome else None}
    body: dict[str, Any] = {
        "id": id, "object": "response", "created_at": created_at, "completed_at": completed_at, "status": status,
        "model": model,
        "output": output, "usage": usage,
        "metadata": {**(request.get("metadata") or {}), **{OURS + key: value for key, value in ours.items() if value}},
        "error": {"code": outcome.code if outcome.code in _ERROR_CODES else "server_error", "message": outcome.message}
        if outcome and outcome.status == "failed" else None,
        "incomplete_details": {"reason": outcome.incomplete_reason} if outcome and outcome.incomplete_reason else None,
        "parallel_tool_calls": request.get("parallel_tool_calls", True),
        "tool_choice": request.get("tool_choice", "auto"),
        "tools": request.get("tools", []),
        **{name: request[name] for name in _ECHOED if name in request},
    }
    try:
        Response.model_validate(body)
    except ValidationError as error:
        # A few fields the API reports as it resolved them (a default filled in), which a caller's own words need
        # not be: those are left out. Anything else wrong is a fault of the platform's.
        for name in {str(wrong["loc"][0]) for wrong in error.errors()} & set(_ECHOED):
            body.pop(name, None)
        Response.model_validate(body)
    return body


def envelope_event(response: dict[str, Any], sequence_number: int, kind: str | None = None) -> dict[str, Any]:
    """A response-level event: the one named, or the one that ends the stream of a response with this status."""
    return {"type": kind or _LAST_EVENT[response["status"]], "response": response, "sequence_number": sequence_number}
