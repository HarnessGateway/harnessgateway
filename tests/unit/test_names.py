"""Names, identifiers and the model line."""

import pytest

from harnessgateway.domain.line import Line
from harnessgateway.errors import Invalid
from harnessgateway.ids import check_name, container_of, fingerprint, is_id, new_id, new_placeholder, session_of


def test_a_model_line_is_four_parts_and_the_model_may_hold_slashes():
    line = Line.parse("dev/codex/openrouter/anthropic/claude-sonnet")
    assert (line.environment, line.harness, line.provider, line.model) == ("dev", "codex", "openrouter", "anthropic/claude-sonnet")
    assert str(line) == "dev/codex/openrouter/anthropic/claude-sonnet"
    for wrong in ("dev/codex", "dev//p/m", "Dev/codex/p/m", "dev/codex/p/" + "m" * 201, None, 4):
        with pytest.raises(Invalid) as refusal:
            Line.parse(wrong)
        assert refusal.value.param == "model" and "environment/harness/provider/model" in refusal.value.message


def test_ids_sort_by_when_they_were_made_and_are_told_apart_by_kind():
    first, second = new_id("response"), new_id("response")
    assert first < second or first[:15] == second[:15]         # the same millisecond sorts either way
    assert is_id(first, "response") and not is_id(first, "session") and not is_id("resp_short", "response") and not is_id(7, "response")
    session = new_id("session")
    assert container_of(session) == "cntr_" + session[5:] and session_of(container_of(session)) == session
    assert check_name("my-agent-2", "name") == "my-agent-2"
    for wrong in ("", "-a", "A", "a_b", "a" * 64, None):
        with pytest.raises(Invalid):
            check_name(wrong, "name")
    assert new_placeholder().startswith("hgs_") and fingerprint("k") == fingerprint("k") != fingerprint("j")
