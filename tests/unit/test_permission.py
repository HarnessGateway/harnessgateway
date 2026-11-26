"""Permissions: what a pattern allows, and what is no wider than something else. Rules of values alone."""

import pytest

from harnessgateway.domain.permission import ANYONE, allows, checked, within
from harnessgateway.errors import Invalid

EVERYTHING = ("*.*",)


@pytest.mark.parametrize(("patterns", "action", "may"), [
    (EVERYTHING, "providers.write", True), (EVERYTHING, "sessions.read_all", True),
    (("*.read",), "providers.read", True), (("*.read",), "providers.write", False),
    (("*.read",), "sessions.read_all", False),                      # reading everything is not reading everyone's
    (("providers.*",), "providers.write", True), (("providers.*",), "environments.read", False),
    (("responses.read", "keys.write"), "keys.write", True), (("responses.read", "keys.write"), "keys.read", False),
    ((), "providers.read", False), (("provider.*",), "providers.read", False), (("providers.rea",), "providers.read", False),
])
def test_an_action_is_allowed_when_one_pattern_matches_it(patterns, action: str, may: bool):
    assert allows(patterns, action) is may


def test_what_is_within_allows_nothing_the_other_does_not():
    assert within(("providers.read",), ("*.read",)) and within(("*.read",), ("*.read",)) and within(("providers.*", "keys.read"), EVERYTHING)
    assert within((), ("providers.read",))                              # nothing is within anything
    for patterns, mine in ((("*.read",), ("providers.read",)), (("providers.*",), ("providers.read",)), (EVERYTHING, ("*.read",)),
                           (("providers.read", "keys.write"), ("*.read",)), (("*.write",), ("providers.*",))):
        assert not within(patterns, mine), (patterns, mine)


def test_patterns_are_checked_when_they_are_written_and_the_refusal_names_what_they_were_for():
    assert checked(["providers.*", "*.read", "providers.*"], "scopes") == ("providers.*", "*.read")
    for wrong in ("providers.*", ["providers"], ["*"], ["Providers.read"], ["providers.read.more"], [5], ["providers. read"], ["a.b"] * 201):
        with pytest.raises(Invalid) as refusal:
            checked(wrong, "scopes")
        assert refusal.value.param == "scopes"
    assert ANYONE == "me.read"                                          # what nobody is ever limited away from
