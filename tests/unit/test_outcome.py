"""How a turn ended, the response it becomes, and what a new harness is told."""

import pytest
from openai.types.responses import Response

from harnessgateway.domain.handover import handover_line, unseen
from harnessgateway.domain.outcome import Observed, Outcome, Terminal, decide
from harnessgateway.domain.response import envelope_event, response_object

DONE, FAILED = Terminal("done"), Terminal("failed", code="no_auth", message="the key was refused")


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
    common = {"id": "resp_1", "session_id": "sess_1", "created_at": 1, "model": "a/b/c/d", "request": request, "output": [], "usage": None}
    queued = response_object(**common, status="queued", outcome=None)
    assert queued["metadata"] == {"mine": "1", "hg_session": "sess_1"} and queued["instructions"] == "be brief" and queued["error"] is None
    failed = response_object(**common, status="failed", outcome=Outcome("failed", "harness_failed", "the key was refused", "no_auth"))
    assert failed["metadata"] == {"mine": "1", "hg_session": "sess_1", "hg_reason": "harness_failed", "hg_code": "no_auth"}
    assert failed["error"] == {"code": "server_error", "message": "the key was refused"}
    cut = response_object(**common, status="incomplete", outcome=Outcome("incomplete", "max_output_tokens", "", incomplete_reason="max_output_tokens"))
    assert cut["incomplete_details"] == {"reason": "max_output_tokens"}
    for response in (queued, failed, cut):
        Response.model_validate(response)
    assert envelope_event(failed, 7)["type"] == "response.failed" and envelope_event(queued, 0, "response.created")["sequence_number"] == 0
    cancelled = response_object(**common, status="cancelled", outcome=Outcome("cancelled", "cancelled", "the turn was cancelled"))
    assert envelope_event(cancelled, 3)["type"] == "response.incomplete"


def test_a_harness_is_told_who_worked_on_the_session_since_it_last_did():
    pasts = [("codex", ".codex"), ("claude-code", ".claude"), ("goose", None), ("goose", None)]
    assert handover_line(pasts, "codex") == "Handover from goose. Before it: claude-code. Review /mind/.claude."
    assert (handover_line(pasts, "goose"), handover_line([], "codex")) == ("", "")
    assert handover_line(pasts[:1], "shell") == "Handover from codex. Review /mind/.codex."
    assert (unseen(pasts, "codex"), unseen(pasts, "goose"), unseen(pasts, "new")) == (3, 0, 4)
