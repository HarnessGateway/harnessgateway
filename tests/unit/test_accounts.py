"""Passwords and sign-in rules (SPEC §9.3), without a database."""
import pytest

from harnessgateway.errors import Invalid
from harnessgateway.services.accounts import PASSWORD_MIN, check_new_password, check_password, hash_password


def test_a_password_hash_is_salted_scrypt_and_checks_only_itself():
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("scrypt$") and stored != hash_password("correct horse battery staple")   # a fresh salt
    assert check_password("correct horse battery staple", stored)
    assert not check_password("correct horse battery stable", stored)
    assert not check_password("anything", None)                      # an account without a password never matches


def test_a_new_password_has_a_floor():
    assert check_new_password("x" * PASSWORD_MIN) == "x" * PASSWORD_MIN
    with pytest.raises(Invalid):
        check_new_password("x" * (PASSWORD_MIN - 1))
    with pytest.raises(Invalid):
        check_new_password(None)
