"""A handover's words: who worked on a session, and where each harness keeps its memory."""

from harnessgateway.domain.handover import Stint, handover, note_path, pointer, stints

PAST = [("resp_1", "first", "dev/first/p/m"), ("resp_2", "first", "dev/first/p/m"), ("resp_3", "second", "dev/second/p/m"),
        ("resp_4", "second", "dev/second/q/n"), ("resp_5", "first", "dev/first/p/m")]


def test_a_chain_is_its_stints_each_a_run_of_turns_on_one_model_line():
    assert stints([]) == []
    assert stints(PAST) == [
        Stint("first", "dev/first/p/m", ("resp_1", "resp_2")), Stint("second", "dev/second/p/m", ("resp_3",)),
        Stint("second", "dev/second/q/n", ("resp_4",)), Stint("first", "dev/first/p/m", ("resp_5",))]


def test_there_is_a_handover_only_when_the_turn_before_was_another_harnesss():
    assert handover([], "first", {}) == ""                                  # a session's first turn
    assert handover(stints(PAST), "first", {}) == ""                        # its own turn came last
    assert handover(stints(PAST[:4]), "second", {}) == ""                   # another model, the same harness
    assert handover(stints(PAST), "third", {}).startswith("Handover. ")


def test_a_handover_names_every_stint_and_where_each_harness_keeps_its_memory_and_repeats_nothing_said():
    said = handover(stints(PAST[:4]), "first", {"second": "/mind/.second/log.jsonl: a JSON line a message\nNewest last.\n"})
    assert "/mind, which is $HOME" in said and "(/work)" in said and "Nothing of what was said or done is repeated" in said
    assert said.endswith(
        "1. first, as dev/first/p/m: resp_1, resp_2\n   This is the harness you are: its memory is your own.\n"
        "2. second, as dev/second/p/m: resp_3\n   Its memory: /mind/.second/log.jsonl: a JSON line a message\n   Newest last.\n"
        "3. second, as dev/second/q/n: resp_4\n   Its memory: /mind/.second/log.jsonl: a JSON line a message\n   Newest last.\n")


def test_a_harness_that_left_no_note_is_said_to_and_a_note_too_long_to_repeat_is_pointed_at():
    assert note_path("second") == "/mind/.harnessgateway/second/memory"
    said = handover(stints(PAST[:3]), "third", {"first": None, "second": pointer("second")})
    assert "1. first, as dev/first/p/m: resp_1, resp_2\n   Its memory: it left no note of where; look in /mind.\n" in said
    assert "   Its memory: it says where in /mind/.harnessgateway/second/memory\n" in said
