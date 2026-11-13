"""Spec §4: names and identifiers."""

import pytest

from harnessgateway.domain.names import (
    check_id,
    check_name,
    container_id,
    session_of_container,
)
from harnessgateway.errors import Invalid
from harnessgateway.ids import new_id


@pytest.mark.parametrize("name", ["a", "claude-code", "pi", "9lives", "a" * 63])
def test_a_valid_name_is_returned_unchanged(name: str) -> None:
    assert check_name(name, "harness") == name


@pytest.mark.parametrize("name", ["", "-a", "Claude", "a_b", "a b", "a" * 64, "a/b", "a:b", None, 7])
def test_an_invalid_name_is_refused_and_named(name: object) -> None:
    with pytest.raises(Invalid) as e:
        check_name(name, "harness")
    assert "harness" in str(e.value)


def test_a_new_id_has_its_prefix_and_26_base32_characters() -> None:
    value = new_id("turn")
    prefix, _, body = value.partition("_")
    assert prefix == "resp"
    assert len(body) == 26
    assert set(body) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    assert check_id(value, "turn") == value


def test_new_ids_are_distinct_and_sort_by_creation() -> None:
    ids = [new_id("session") for _ in range(200)]
    assert len(set(ids)) == 200
    assert ids[0][:15] <= ids[-1][:15]          # the time part never goes backwards


@pytest.mark.parametrize("value", ["resp_123", "sess_" + "0" * 26, "resp_" + "I" * 26, "", None])
def test_an_id_of_the_wrong_kind_or_shape_is_refused(value: object) -> None:
    with pytest.raises(Invalid):
        check_id(value, "turn")


def test_an_unknown_kind_of_id_is_a_programming_error() -> None:
    with pytest.raises(KeyError):
        new_id("banana")


def test_a_session_and_its_container_id_map_both_ways() -> None:
    session = new_id("session")
    container = container_id(session)
    assert container == "cntr_" + session[5:]
    assert session_of_container(container) == session
    with pytest.raises(Invalid):
        session_of_container(session)
