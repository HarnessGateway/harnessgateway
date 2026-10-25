"""Layer 2: a sandbox's uid is its organization's, the same on every node and different from every
other organization's."""
from harnessgateway.backends.sandboxes import UID_BASE, UID_RANGE, uid_for


def test_uids():
    acme, globex = uid_for("acme"), uid_for("globex")
    assert acme != globex and all(UID_BASE <= u < UID_BASE + UID_RANGE for u in (acme, globex))
    assert uid_for("acme") == acme                                     # the same everywhere
