"""Conformance: a sealer seals a value to one record, refuses it for another, derives the same
secondary secret every time, and a second process opens what the first sealed. The local vault, and
AWS KMS through moto."""
import pytest

from harnessgateway.backends.kms.aws import AwsKms
from harnessgateway.backends.kms.local import open_local
from harnessgateway.backends.records.sqlite import SqliteRecords
from harnessgateway.kernel.vault import Vault


@pytest.fixture(params=["local", "aws"])
def sealers(request, tmp_path):
    """Two sealers that share the key: 'two processes'."""
    if request.param == "local":
        env = {"HG_MASTER_KEY": Vault.generate()}
        return open_local(env), open_local(env)
    client, key_id = request.getfixturevalue("kms_client")
    records = SqliteRecords(tmp_path / "r.db")
    return AwsKms(key_id, records, client=client), AwsKms(key_id, records, client=client)


def test_seal_open_derive_across_processes(sealers):
    a, b = sealers
    sealed = a.seal("sk-secret", "acme/providers/x")
    assert "sk-secret" not in sealed and b.open(sealed, "acme/providers/x") == "sk-secret"
    with pytest.raises(Exception):
        b.open(sealed, "acme/providers/y")                                  # bound to its record
    assert a.derive("blobs") == b.derive("blobs") and a.derive("blobs") != a.derive("accounts") and len(a.derive("x")) == 32
    assert a.seal("v", "k") != a.seal("v", "k")                             # a fresh nonce every time


def test_aws_keeps_one_wrapped_data_key(tmp_path, kms_client):
    client, key_id = kms_client
    records = SqliteRecords(tmp_path / "r.db")
    AwsKms(key_id, records, client=client)
    kept = records.get("kms", "", "data-key")
    assert kept and kept["key_id"] == key_id and kept["wrapped"]
    with pytest.raises(RuntimeError):
        AwsKms("", records, client=client)
