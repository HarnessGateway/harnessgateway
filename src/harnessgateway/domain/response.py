"""A turn as the Responses API returns it: the response object, and the response-level events of its stream."""

from typing import Any

from openai.types.responses import Response

from harnessgateway.domain.outcome import Outcome
from harnessgateway.domain.request import OURS

# What a caller sent that a response reports back, where the caller sent it.
_ECHOED = ("instructions", "previous_response_id", "background", "temperature", "top_p", "max_output_tokens",
           "max_tool_calls", "truncation", "user", "reasoning", "text", "service_tier")
_LAST_EVENT = {"completed": "response.completed", "failed": "response.failed", "incomplete": "response.incomplete",
               "cancelled": "response.incomplete"}       # the stream has no event of its own for a cancelled turn
LAST_EVENTS = frozenset(_LAST_EVENT.values())


def response_object(*, id: str, session_id: str, created_at: int, status: str, model: str, request: dict[str, Any],
                    output: list[dict[str, Any]], usage: dict[str, Any] | None, outcome: Outcome | None,
                    ) -> dict[str, Any]:
    """A turn as an OpenAI response object. What the platform has to say that the object has no field for goes
    in `metadata`, under keys of its own: the session, and why a turn did not complete."""
    ours = {"session": session_id, "reason": outcome.reason if outcome and outcome.status != "completed" else None,
            "code": outcome.code if outcome else None}
    body: dict[str, Any] = {
        "id": id, "object": "response", "created_at": created_at, "status": status, "model": model,
        "output": output, "usage": usage,
        "metadata": {**(request.get("metadata") or {}), **{OURS + key: value for key, value in ours.items() if value}},
        "error": {"code": "server_error", "message": outcome.message}
        if outcome and outcome.status == "failed" else None,
        "incomplete_details": {"reason": outcome.incomplete_reason} if outcome and outcome.incomplete_reason else None,
        "parallel_tool_calls": request.get("parallel_tool_calls", True),
        "tool_choice": request.get("tool_choice", "auto"),
        "tools": request.get("tools", []),
        **{name: request[name] for name in _ECHOED if name in request},
    }
    Response.model_validate(body)              # it is the wire format, or it is a fault of the platform's
    return body


def envelope_event(response: dict[str, Any], sequence_number: int, kind: str | None = None) -> dict[str, Any]:
    """A response-level event: the one named, or the one that ends the stream of a response with this status."""
    return {"type": kind or _LAST_EVENT[response["status"]], "response": response, "sequence_number": sequence_number}
