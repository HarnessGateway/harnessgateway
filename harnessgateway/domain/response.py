"""The response object, and the stream events the platform itself writes (spec §13.4, §13.5)."""

from dataclasses import dataclass, field
from typing import Any

from openai.types.responses import Response

from harnessgateway.domain.outcome import Outcome

# What a caller sent that the response reports back, where the caller sent it.
_ECHOED = ("instructions", "previous_response_id", "background", "temperature", "top_p", "max_output_tokens",
           "max_tool_calls", "truncation", "user", "reasoning", "text", "service_tier")

_TERMINAL_EVENTS = {
    "completed": "response.completed",
    "failed": "response.failed",
    "incomplete": "response.incomplete",
    "cancelled": "response.incomplete",      # the stream has no event of its own for a cancelled turn
}


@dataclass(frozen=True)
class TurnView:
    """What is known of a turn at the moment its response object is built."""

    id: str
    session_id: str
    created_at: int                                   # seconds since the epoch
    status: str
    model: str                                        # the resolved model string
    request: dict[str, Any]
    output: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    outcome: Outcome | None = None                    # set once the turn is final


def response_object(turn: TurnView) -> dict[str, Any]:
    """The turn as an OpenAI response object. It always validates against the SDK's own type."""
    outcome, request = turn.outcome, turn.request
    metadata = {**request.get("metadata", {}), "hg_session": turn.session_id}
    if outcome and outcome.status != "completed" and outcome.reason:
        metadata["hg_reason"] = outcome.reason
    if outcome and outcome.code:
        metadata["hg_code"] = outcome.code
    body: dict[str, Any] = {
        "id": turn.id,
        "object": "response",
        "created_at": turn.created_at,
        "status": turn.status,
        "model": turn.model,
        "output": turn.output,
        "usage": turn.usage,
        "error": {"code": "server_error", "message": outcome.message} if outcome and outcome.status == "failed"
        else None,
        "incomplete_details": {"reason": outcome.incomplete_reason} if outcome and outcome.incomplete_reason else None,
        "metadata": metadata,
        "parallel_tool_calls": request.get("parallel_tool_calls", True),
        "tool_choice": request.get("tool_choice", "auto"),
        "tools": request.get("tools", []),
        **{name: request[name] for name in _ECHOED if name in request},
    }
    Response.model_validate(body)
    return body


def terminal_event_type(status: str) -> str:
    """The stream event that ends a turn with this final status."""
    return _TERMINAL_EVENTS[status]


def envelope_event(kind: str, response: dict[str, Any], *, sequence_number: int) -> dict[str, Any]:
    """One of the response-level events: created, in progress, and the one that ends the stream."""
    return {"type": kind, "response": response, "sequence_number": sequence_number}
