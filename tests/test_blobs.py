"""Layer 3: blobs on both backends: put, get, exists, list, delete by prefix; signed URLs for the directory."""
import pytest

from harnessgateway.backends.blobs.dir import DirBlobs
from harnessgateway.errors import Invalid, NotFound


def test_put_get_list_delete(blobs):
    blobs.put("tenants/acme/a/x.txt", b"x")
    blobs.put("tenants/acme/a/y.txt", b"y")
    blobs.put("tenants/acme/b.txt", b"b")
    assert blobs.get("tenants/acme/a/x.txt") == b"x" and blobs.exists("tenants/acme/b.txt")
    assert blobs.list("tenants/acme") == ["tenants/acme/a/x.txt", "tenants/acme/a/y.txt", "tenants/acme/b.txt"]
    blobs.delete("tenants/acme/a")
    assert blobs.list("tenants/acme") == ["tenants/acme/b.txt"] and not blobs.exists("tenants/acme/a/x.txt")
    with pytest.raises(NotFound):
        blobs.get("tenants/acme/a/x.txt")
    with pytest.raises(Invalid):
        blobs.put("../escape", b"")
    assert blobs.presign_get("tenants/acme/b.txt").startswith("http")


def test_dir_signed_urls(tmp_path):
    b = DirBlobs(tmp_path, "http://api.test", b"secret")
    url = b.presign_put("tenants/acme/s.tgz", ttl_s=60)
    token = url.rsplit("/", 1)[1]
    assert b.verify(token, "PUT") == "tenants/acme/s.tgz"
    with pytest.raises(Invalid):
        b.verify(token, "GET")
    with pytest.raises(Invalid):
        DirBlobs(tmp_path, "http://api.test", b"other").verify(token, "PUT")
    with pytest.raises(Invalid):
        b.verify(b.sign("GET", "k", ttl_s=-1), "GET")
