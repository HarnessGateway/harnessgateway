import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))          # fakes, domainfix and test_api are importable from every suite

from harnessgateway.backends.blobs.dir import DirBlobs
from harnessgateway.backends.blobs.s3 import S3Blobs
from harnessgateway.backends.records.postgres import PostgresRecords
from harnessgateway.backends.records.sqlite import SqliteRecords
from harnessgateway.kernel.vault import Vault

POSTGRES = os.environ.get("HG_TEST_POSTGRES", "postgresql://postgres@/postgres?host=/var/lib/postgresql/hg&port=54329")


@pytest.fixture
def vault():
    return Vault(bytes.fromhex(Vault.generate()))


@pytest.fixture(params=["sqlite", "postgres"])
def records(request, tmp_path):
    if request.param == "sqlite":
        r = SqliteRecords(tmp_path / "records.db")
    else:
        try:
            r = PostgresRecords(POSTGRES)
        except Exception as e:                       # no Postgres here: the SQLite run still proves the statements
            pytest.skip(f"no Postgres at {POSTGRES}: {e}")
        r._run("DELETE FROM records")
    yield r
    r.close()


@pytest.fixture(scope="session")
def s3_endpoint():
    """A moto S3 server for the S3 backend's tests."""
    try:
        from moto.server import ThreadedMotoServer
    except ImportError:
        pytest.skip("moto[server] is not installed")
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server._server.server_address[:2]
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    yield f"http://127.0.0.1:{port}"
    server.stop()


@pytest.fixture
def kms_client(s3_endpoint):
    """A KMS client against the same moto server, with one key made."""
    import boto3
    client = boto3.client("kms", endpoint_url=s3_endpoint, region_name="us-east-1")
    key_id = client.create_key(Description="hg test")["KeyMetadata"]["KeyId"]
    return client, key_id


@pytest.fixture(params=["dir", "s3"])
def blobs(request, tmp_path):
    if request.param == "dir":
        return DirBlobs(tmp_path / "blobs", "http://api.test", b"secret")
    endpoint = request.getfixturevalue("s3_endpoint")
    import boto3
    boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1").create_bucket(Bucket="hg-test")
    b = S3Blobs("hg-test", endpoint, "us-east-1")
    b.delete("tenants")
    return b
