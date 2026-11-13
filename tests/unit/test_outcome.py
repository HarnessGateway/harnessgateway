"""Spec §5.4: how a turn's final status and reason are decided. Outside observations win."""

import pytest

from harnessgateway.domain.outcome import Observed, Terminal, decide

DONE = Terminal(kind="done")


def test_a_package_that_reports_done_and_exits_cleanly_completes() -> None:
    o = decide(Observed(exit_code=0, terminal=DONE))
    assert (o.status, o.reason, o.message) == ("completed", None, None)


@pytest.mark.parametrize(
    ("observed", "status", "reason"),
    [
        (Observed(install_failed=True), "failed", "install_failed"),
        (Observed(restore_failed=True), "failed", "restore_failed"),
        (Observed(sandbox_failed=True), "failed", "sandbox_failed"),
        (Observed(cancelled=True, exit_code=137), "cancelled", "cancelled"),
        (Observed(timed_out=True, exit_code=137), "failed", "timed_out"),
        (Observed(out_of_disk=True, exit_code=137), "failed", "out_of_disk"),
        (Observed(out_of_memory=True, exit_code=137), "failed", "out_of_memory"),
        (Observed(invalid_output="line 3 is not JSON", exit_code=143), "failed", "invalid_output"),
        (Observed(save_failed=True, exit_code=0, terminal=DONE), "failed", "save_failed"),
        (Observed(exit_code=0, terminal=Terminal(kind="failed", code="provider_error", message="401")),
         "failed", "harness_failed"),
        (Observed(exit_code=0, terminal=Terminal(kind="incomplete", reason="max_output_tokens")), "incomplete",
         "max_output_tokens"),
        (Observed(exit_code=0), "failed", "harness_error"),
        (Observed(exit_code=3), "failed", "harness_error"),
        (Observed(exit_code=1, terminal=DONE), "failed", "harness_error"),
    ],
)
def test_each_row_of_the_table(observed: Observed, status: str, reason: str) -> None:
    o = decide(observed)
    assert (o.status, o.reason) == (status, reason)
    assert o.message                                   # every outcome but success says what happened


def test_rows_are_checked_in_order_so_outside_observations_win() -> None:
    everything = Observed(cancelled=True, timed_out=True, out_of_memory=True, save_failed=True, exit_code=0,
                          terminal=DONE)
    assert decide(everything).reason == "cancelled"
    assert decide(Observed(timed_out=True, out_of_memory=True, exit_code=137, terminal=DONE)).reason == "timed_out"
    assert decide(Observed(out_of_memory=True, save_failed=True, exit_code=137)).reason == "out_of_memory"


def test_a_package_failure_carries_its_code_and_its_own_words() -> None:
    said = Terminal(kind="failed", code="provider_error", message="401 Unauthorized")
    o = decide(Observed(exit_code=0, terminal=said))
    assert (o.code, o.message) == ("provider_error", "401 Unauthorized")


def test_an_incomplete_turn_carries_the_package_s_reason() -> None:
    o = decide(Observed(exit_code=0, terminal=Terminal(kind="incomplete", reason="content_filter")))
    assert (o.status, o.incomplete_reason) == ("incomplete", "content_filter")


def test_a_message_says_what_happened_in_plain_words() -> None:
    assert "time limit" in (decide(Observed(timed_out=True)).message or "")
    assert "memory" in (decide(Observed(out_of_memory=True)).message or "")
    assert "exited with code 3" in (decide(Observed(exit_code=3)).message or "")
