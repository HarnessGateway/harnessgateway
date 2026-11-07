import pytest

from harnessgateway import ids
from harnessgateway.errors import NotFound


def test_ids_carry_their_kind():
    rid = ids.new("turn")
    assert rid.startswith("resp_") and ids.check(rid, "turn") == rid
    with pytest.raises(NotFound):
        ids.check(rid, "session")
    with pytest.raises(NotFound):
        ids.check("resp_short", "turn")


def test_secrets_are_kept_as_digests():
    box = ids.new_secret("box")
    assert box.startswith("hgb_") and len(box) == 44
    assert ids.digest(box, b"p") != ids.digest(box, b"q") and len(ids.digest(box, b"p")) == 64
