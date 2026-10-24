"""Layer 4: a sandbox's uid is its scope's: an organization's, or one team's within it, the same on
every node and different from every other scope's."""
from harnessgateway.backends.sandboxes import UID_BASE, UID_RANGE, scope_of, uid_for


def test_scopes_and_uids():
    assert scope_of("acme") == "acme" and scope_of("acme", "platform") == "acme/platform"
    org, platform, data = uid_for(scope_of("acme")), uid_for(scope_of("acme", "platform")), uid_for(scope_of("acme", "data"))
    assert len({org, platform, data, uid_for("globex")}) == 4
    assert all(UID_BASE <= u < UID_BASE + UID_RANGE for u in (org, platform, data))
    assert uid_for("acme/platform") == platform                      # the same everywhere
