"""Sealing and the door CA (SPEC §8.3)."""
import pytest
from cryptography import x509

from harnessgateway.adapters.keys_local import LocalKeys
from harnessgateway.crypto import DataKeys, DoorCA, derive, seal, unseal

SECRET = b"a" * 32


def test_a_sealed_value_opens_only_with_its_record():
    keys = DataKeys(LocalKeys(derive(SECRET, "harnessgateway/master")))
    wrapped = keys.new("org_1")
    key = keys.open("org_1", wrapped)
    sealed = seal(key, "the value", "org_1/provider/prv_1")
    assert unseal(key, sealed, "org_1/provider/prv_1") == "the value"
    with pytest.raises(Exception):
        unseal(key, sealed, "org_1/provider/prv_2")
    with pytest.raises(Exception):
        keys.open("org_2", wrapped)


def test_the_door_ca_is_the_same_everywhere_it_is_derived():
    a, b = DoorCA(SECRET), DoorCA(SECRET)
    assert a.key_pem == b.key_pem                                   # the key is derived, so every process has it
    cert, other = x509.load_pem_x509_certificate(a.cert_pem), x509.load_pem_x509_certificate(b.cert_pem)
    assert cert.public_key().public_numbers() == other.public_key().public_numbers()   # (the self-signature differs)
    assert DoorCA(b"b" * 32).key_pem != a.key_pem
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
