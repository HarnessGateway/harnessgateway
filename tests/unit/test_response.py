"""Spec §13.4, §13.5: the response object and the events the platform itself writes."""

from typing import Any

import pytest
from openai.types.responses import Response

from harnessgateway.domain.outcome import Outcome
from harnessgateway.domain.response import TurnView, envelope_event, response_object, terminal_event_type

SESSION, TURN = "sess_" + "S" * 26, "resp_" + "T" * 26
MESSAGE = {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
           "content": [{"type": "output_text", "text": "Hi", "annotations": []}]}
USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
         "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
         "output_tokens_details": {"reasoning_tokens": 0}}


def view(**changes: Any) -> TurnView:
    base: dict[str, Any] = {
        "id": TURN, "session_id": SESSION, "created_at": 1_794_522_753, "status": "in_progress",
        "model": "default:codex/deepseek/deepseek-flash",
        "request": {"model": "codex", "input": "Hello"},
    }
    return TurnView(**{**base, **changes})


def test_a_turn_in_progress() -> None:
    body = response_object(view())
    Response.model_validate(body)
    assert (body["id"], body["object"], body["status"], body["created_at"]) == (TURN, "response", "in_progress",
                                                                              1_794_522_753)
    assert body["model"] == "default:codex/deepseek/deepseek-flash"
    assert (body["output"], body["usage"], body["error"], body["incomplete_details"]) == ([], None, None, None)
    assert body["metadata"] == {"hg_session": SESSION}


def test_a_completed_turn_carries_its_output_and_usage() -> None:
    body = response_object(view(status="completed", output=[MESSAGE], usage=USAGE, outcome=Outcome("completed")))
    parsed = Response.model_validate(body)
    assert parsed.output_text == "Hi"
    assert body["usage"]["total_tokens"] == 15
    assert body["metadata"] == {"hg_session": SESSION}


def test_a_failed_turn_says_why_in_the_places_the_schema_provides() -> None:
    outcome = Outcome("failed", "timed_out", "the turn passed its environment's time limit and was stopped")
    body = response_object(view(status="failed", outcome=outcome))
    Response.model_validate(body)
    assert body["error"] == {"code": "server_error", "message": outcome.message}
    assert body["metadata"] == {"hg_session": SESSION, "hg_reason": "timed_out"}


def test_a_package_failure_reports_the_package_s_code() -> None:
    outcome = Outcome("failed", "harness_failed", "401 Unauthorized", code="provider_error")
    body = response_object(view(status="failed", outcome=outcome))
    assert body["metadata"] == {"hg_session": SESSION, "hg_reason": "harness_failed", "hg_code": "provider_error"}


def test_an_incomplete_turn_has_incomplete_details() -> None:
    outcome = Outcome("incomplete", "max_output_tokens", "stopped early", incomplete_reason="max_output_tokens")
    body = response_object(view(status="incomplete", outcome=outcome, output=[MESSAGE]))
    Response.model_validate(body)
    assert body["incomplete_details"] == {"reason": "max_output_tokens"}
    assert body["error"] is None


def test_a_cancelled_turn() -> None:
    cancelled = Outcome("cancelled", "cancelled", "the turn was cancelled")
    body = response_object(view(status="cancelled", outcome=cancelled))
    Response.model_validate(body)
    assert (body["status"], body["error"]) == ("cancelled", None)
    assert body["metadata"]["hg_reason"] == "cancelled"


def test_what_the_caller_sent_is_echoed_and_their_metadata_kept() -> None:
    request = {
        "model": "codex", "input": "Hello", "instructions": "Be brief.", "previous_response_id": "resp_" + "P" * 26,
        "metadata": {"ticket": "OPS-1"}, "background": True, "temperature": 0.2, "max_output_tokens": 500,
        "store": False,
    }
    body = response_object(view(request=request, status="queued"))
    Response.model_validate(body)
    assert body["instructions"] == "Be brief."
    assert body["previous_response_id"] == "resp_" + "P" * 26
    assert body["metadata"] == {"ticket": "OPS-1", "hg_session": SESSION}
    assert (body["background"], body["temperature"], body["max_output_tokens"]) == (True, 0.2, 500)


@pytest.mark.parametrize(("status", "event"), [
    ("completed", "response.completed"),
    ("failed", "response.failed"),
    ("incomplete", "response.incomplete"),
    ("cancelled", "response.incomplete"),
])
def test_the_event_that_ends_a_stream(status: str, event: str) -> None:
    assert terminal_event_type(status) == event


def test_a_status_that_is_not_final_ends_no_stream() -> None:
    with pytest.raises(KeyError):
        terminal_event_type("in_progress")


@pytest.mark.parametrize("kind", ["response.created", "response.in_progress", "response.completed",
                                  "response.failed", "response.incomplete"])
def test_the_platform_s_own_events_are_valid_and_numbered(kind: str) -> None:
    from harnessgateway.domain.events import EVENT_MODELS

    event = envelope_event(kind, response_object(view()), sequence_number=4)
    EVENT_MODELS[kind].model_validate(event)
    assert (event["type"], event["sequence_number"], event["response"]["id"]) == (kind, 4, TURN)
