"""Spec §10.4, §10.5, §16: sealing secrets, placeholders, and keyed hashes."""

import re

import pytest

from harnessgateway.domain.sealing import Sealer, SealError, keyed_hash, new_placeholder, substitute

KEY = bytes(range(32))


def test_a_sealed_value_opens_with_the_same_key_and_binding() -> None:
    sealer = Sealer(KEY)
    sealed = sealer.seal("sk-secret", org="org_default", kind="provider", name="deepseek")
    assert b"sk-secret" not in sealed
    assert sealer.open(sealed, org="org_default", kind="provider", name="deepseek") == "sk-secret"


def test_sealing_the_same_value_twice_gives_different_bytes() -> None:
    sealer = Sealer(KEY)
    a = sealer.seal("v", org="o", kind="k", name="n")
    b = sealer.seal("v", org="o", kind="k", name="n")
    assert a != b


@pytest.mark.parametrize("binding", [
    {"org": "org_other", "kind": "provider", "name": "deepseek"},
    {"org": "org_default", "kind": "environment", "name": "deepseek"},
    {"org": "org_default", "kind": "provider", "name": "openai"},
])
def test_a_sealed_value_moved_to_another_place_does_not_open(binding: dict[str, str]) -> None:
    sealer = Sealer(KEY)
    sealed = sealer.seal("sk-secret", org="org_default", kind="provider", name="deepseek")
    with pytest.raises(SealError):
        sealer.open(sealed, **binding)


def test_a_tampered_or_truncated_value_does_not_open() -> None:
    sealer = Sealer(KEY)
    sealed = sealer.seal("sk-secret", org="o", kind="k", name="n")
    with pytest.raises(SealError):
        sealer.open(sealed[:-1] + bytes([sealed[-1] ^ 1]), org="o", kind="k", name="n")
    with pytest.raises(SealError):
        sealer.open(sealed[:5], org="o", kind="k", name="n")


def test_another_key_does_not_open_it() -> None:
    sealed = Sealer(KEY).seal("sk-secret", org="o", kind="k", name="n")
    with pytest.raises(SealError):
        Sealer(bytes(32)).open(sealed, org="o", kind="k", name="n")


@pytest.mark.parametrize("key", [b"", b"short", bytes(31), bytes(33)])
def test_the_key_is_exactly_32_bytes(key: bytes) -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        Sealer(key)


def test_a_placeholder_is_hgs_and_32_hex_characters_and_never_repeats() -> None:
    made = {new_placeholder() for _ in range(100)}
    assert len(made) == 100
    assert all(re.fullmatch(r"hgs_[0-9a-f]{32}", p) for p in made)


def test_substitution_replaces_only_the_placeholders_it_is_given() -> None:
    a, b, other = new_placeholder(), new_placeholder(), new_placeholder()
    text = f"Bearer {a}; x-api-key: {b}; unknown: {other}"
    assert substitute(text, {a: "real-a", b: "real-b"}) == f"Bearer real-a; x-api-key: real-b; unknown: {other}"
    assert substitute("nothing here", {a: "real-a"}) == "nothing here"


def test_a_keyed_hash_is_stable_and_depends_on_the_key() -> None:
    assert keyed_hash(KEY, "hg_abc") == keyed_hash(KEY, "hg_abc")
    assert keyed_hash(KEY, "hg_abc") != keyed_hash(bytes(32), "hg_abc")
    assert keyed_hash(KEY, "hg_abc") != keyed_hash(KEY, "hg_abd")
    assert re.fullmatch(r"[0-9a-f]{64}", keyed_hash(KEY, "hg_abc"))
