"""How a turn ended, the response it becomes, and what a new harness is told."""

import pytest
from openai.types.responses import Response

from harnessgateway.domain.handover import handover_line, unseen
from harnessgateway.domain.outcome import Observed, Outcome, Terminal, decide
from harnessgateway.domain.response import envelope_event, response_object

DONE, FAILED = Terminal("completed"), Terminal("failed", code="no_auth", message="the key was refused")


@pytest.mark.parametrize(("seen", "status", "reason"), [
    (Observed(exit_code=0, terminal=DONE), "completed", None),
    (Observed(before="install_failed", terminal=DONE), "failed", "install_failed"),
    (Observed(started=False, terminal=DONE), "failed", "sandbox_failed"),
    (Observed(stopped="shutdown", exit_code=0, terminal=DONE), "failed", "sandbox_failed"),
    (Observed(stopped="cancelled", exit_code=0, terminal=DONE), "cancelled", "cancelled"),
    (Observed(stopped="timed_out", exit_code=137), "failed", "timed_out"),
    (Observed(stopped="out_of_disk", exit_code=137), "failed", "out_of_disk"),
    (Observed(exit_code=137, out_of_memory=True, terminal=DONE), "failed", "out_of_memory"),
    (Observed(exit_code=137), "failed", "out_of_memory"),
    (Observed(startup_failed=True, exit_code=78), "failed", "startup_failed"),
    (Observed(stopped="invalid_output", invalid_output="not JSON", exit_code=143), "failed", "invalid_output"),
    (Observed(exit_code=0, terminal=DONE, saved=False), "failed", "save_failed"),
    (Observed(exit_code=0, terminal=FAILED), "failed", "harness_failed"),
    (Observed(exit_code=0, terminal=Terminal("incomplete", reason="max_output_tokens")), "incomplete", "max_output_tokens"),
    (Observed(exit_code=2, terminal=DONE), "failed", "harness_error"),
    (Observed(exit_code=0), "failed", "harness_error"),
])
def test_what_the_platform_saw_outranks_what_the_harness_said(seen: Observed, status: str, reason: str | None):
    outcome = decide(seen)
    assert (outcome.status, outcome.reason) == (status, reason)
    assert decide(Observed(exit_code=0, terminal=FAILED)).code == "no_auth"


def test_a_turn_is_always_a_valid_response_object():
    request = {"model": "a/b/c/d", "input": "x", "metadata": {"mine": "1"}, "instructions": "be brief"}
    common = {"id": "resp_1", "session_id": "sess_1", "created_at": 1, "completed_at": None, "model": "a/b/c/d", "request": request,
              "output": [], "usage": None}
    queued = response_object(**common, status="queued", outcome=None)
    assert queued["metadata"] == {"mine": "1", "hg_session": "sess_1"} and queued["instructions"] == "be brief" and queued["error"] is None
    failed = response_object(**common, status="failed", outcome=Outcome("failed", "harness_failed", "the key was refused", "no_auth"))
    assert failed["metadata"] == {"mine": "1", "hg_session": "sess_1", "hg_reason": "harness_failed", "hg_code": "no_auth"}
    assert failed["error"] == {"code": "server_error", "message": "the key was refused"}     # its own code is in metadata
    limited = response_object(**common, status="failed", outcome=Outcome("failed", "harness_failed", "slow down", "rate_limit_exceeded"))
    assert limited["error"]["code"] == limited["metadata"]["hg_code"] == "rate_limit_exceeded"      # a code the API has
    cut = response_object(**common, status="incomplete", outcome=Outcome("incomplete", "max_output_tokens", "", incomplete_reason="max_output_tokens"))
    assert cut["incomplete_details"] == {"reason": "max_output_tokens"}
    for response in (queued, failed, limited, cut):
        Response.model_validate(response)
    unresolved = response_object(id="resp_1", session_id="sess_1", created_at=1, completed_at=9, status="completed", model="a/b/c/d",
                                 request={"image": {"context_compression": "auto"}, "user": "u"}, output=[], usage=None, outcome=None)
    assert "image" not in unresolved and unresolved["user"] == "u"      # what the API would report resolved is left out
    assert envelope_event(failed, 7)["type"] == "response.failed" and envelope_event(queued, 0, "response.created")["sequence_number"] == 0
    cancelled = response_object(**common, status="cancelled", outcome=Outcome("cancelled", "cancelled", "the turn was cancelled"))
    assert envelope_event(cancelled, 3)["type"] == "response.incomplete"


def test_a_response_reports_back_what_its_request_asked_for():
    """Every field the API's request and its response both have, where the caller sent it; and when it ended."""
    asked = {"instructions": "be brief", "previous_response_id": "resp_0", "background": False, "temperature": 0.2, "top_p": 0.9,
             "max_output_tokens": 500, "max_tool_calls": 3, "truncation": "auto", "user": "u", "safety_identifier": "s",
             "reasoning": {"effort": "low"}, "text": {"format": {"type": "json_schema", "name": "n", "schema": {"type": "object"}}, "verbosity": "low"},
             "service_tier": "flex", "top_logprobs": 2, "prompt_cache_key": "k", "prompt_cache_retention": "24h",
             "prompt_cache_options": {"mode": "explicit", "ttl": "30m"}, "image": {"context_compression": "enabled"},
             "access_programs": {"cyber": "standard"},
             "tools": [{"type": "function", "name": "f", "parameters": {}, "strict": True}], "tool_choice": "required", "parallel_tool_calls": False}
    response = response_object(id="resp_1", session_id="sess_1", created_at=1, completed_at=9, status="completed", model="a/b/c/d",
                               request={"model": "a/b/c/d", "input": "x", "stream": True, "store": True, **asked}, output=[], usage=None,
                               outcome=Outcome("completed"))
    assert {name: response[name] for name in asked} == asked and response["completed_at"] == 9
    assert "stream" not in response and "store" not in response and "input" not in response     # the request's alone
    Response.model_validate(response)


def test_a_harness_is_told_who_worked_on_the_session_since_it_last_did():
    pasts = [("codex", ".codex"), ("claude-code", ".claude"), ("goose", None), ("goose", None)]
    assert handover_line(pasts, "codex") == "Handover from goose. Before it: claude-code. Review /mind/.claude."
    assert (handover_line(pasts, "goose"), handover_line([], "codex")) == ("", "")
    assert handover_line(pasts[:1], "shell") == "Handover from codex. Review /mind/.codex."
    assert (unseen(pasts, "codex"), unseen(pasts, "goose"), unseen(pasts, "new")) == (3, 0, 4)
