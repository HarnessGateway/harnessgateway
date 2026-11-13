"""Spec §6.5: the gap and the handover line."""

from harnessgateway.domain.handover import PastTurn, gap, handover_line

CLAUDE = PastTurn("claude-code", ".claude")
HERMES = PastTurn("hermes", ".hermes")
CODEX = PastTurn("codex", ".codex")
PI = PastTurn("pi", ".pi")
SHELL = PastTurn("shell", None)


def test_a_first_turn_has_no_handover() -> None:
    assert handover_line([], "claude-code") == ""


def test_a_harness_continuing_itself_has_no_handover() -> None:
    assert handover_line([CLAUDE, CLAUDE], "claude-code") == ""


def test_a_new_harness_is_handed_over_from_the_one_before() -> None:
    assert handover_line([CLAUDE], "hermes") == "Handover from claude-code. Review /mind/.claude."


def test_two_earlier_harnesses_are_named_most_recent_first() -> None:
    assert handover_line([CLAUDE, HERMES], "codex") == (
        "Handover from hermes. Before it: claude-code. Review /mind/.hermes and /mind/.claude.")


def test_three_earlier_harnesses() -> None:
    assert handover_line([CLAUDE, HERMES, CODEX], "pi") == (
        "Handover from codex. Before it: hermes and claude-code. "
        "Review /mind/.codex, /mind/.hermes and /mind/.claude.")


def test_a_returning_harness_hears_only_about_what_happened_since_its_own_turn() -> None:
    chain = [CLAUDE, HERMES, CODEX, PI]
    assert handover_line(chain, "claude-code") == (
        "Handover from pi. Before it: codex and hermes. Review /mind/.pi, /mind/.codex and /mind/.hermes.")
    assert handover_line(chain, "hermes") == "Handover from pi. Before it: codex. Review /mind/.pi and /mind/.codex."


def test_each_other_harness_is_named_once_however_many_turns_it_took() -> None:
    assert handover_line([CLAUDE, HERMES, CLAUDE, HERMES, CODEX, CODEX], "pi") == (
        "Handover from codex. Before it: hermes and claude-code. "
        "Review /mind/.codex, /mind/.hermes and /mind/.claude.")


def test_a_harness_that_keeps_no_session_is_named_but_has_nothing_to_review() -> None:
    assert handover_line([SHELL], "claude-code") == "Handover from shell."
    assert handover_line([CLAUDE, SHELL], "codex") == (
        "Handover from shell. Before it: claude-code. Review /mind/.claude.")


def test_the_gap_is_every_turn_after_the_harness_s_own_last_one() -> None:
    chain = [CLAUDE, HERMES, CLAUDE, CODEX, PI]
    assert gap(chain, "claude-code") == [CODEX, PI]
    assert gap(chain, "pi") == []
    assert gap(chain, "aider") == chain
