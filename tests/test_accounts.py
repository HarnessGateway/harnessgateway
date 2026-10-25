"""Layer 3: accounts. Passwords are hashed, emails are one account, memberships name the member an
account is in each org, invitations are redeemed on arrival, and the tokens carry their purpose
and expiry."""
import time

import pytest

from domainfix import domain
from harnessgateway.domain.accounts import check_password, handle_of, hash_password
from harnessgateway.backends.blobs.dir import DirBlobs
from harnessgateway.errors import Conflict, Invalid, NotFound


@pytest.fixture
def accounts(records, tmp_path, vault):
    d = domain(records, DirBlobs(tmp_path / "blobs", "http://api.test", b"s"), vault, tmp_path / "git")
    d.orgs.create("acme")
    return d.accounts


def test_passwords_are_hashed_and_checked():
    h = hash_password("correct horse")
    assert h.startswith("scrypt$") and "correct horse" not in h and h != hash_password("correct horse")
    assert check_password(h, "correct horse") and not check_password(h, "Correct horse") and not check_password("junk", "x")
    with pytest.raises(Invalid):
        hash_password("short")


def test_handles():
    assert handle_of("Chase.Dovey+hg@example.com") == "chase.dovey-hg"
    assert handle_of("___@example.com") == "user"


def test_an_email_is_one_account(accounts):
    a = accounts.create("Chase@Example.com", "Chase", "correct horse")
    assert a.email == "chase@example.com" and a.memberships == []
    with pytest.raises(Conflict):
        accounts.create("chase@example.com", "", "correct horse")
    with pytest.raises(Invalid):
        accounts.create("not-an-email", "", "correct horse")
    with pytest.raises(Invalid):
        accounts.create("x@example.com", "", "")                 # a password is needed
    assert accounts.login("chase@example.com", "correct horse").id == a.id
    assert accounts.login("chase@example.com", "wrong") is None and accounts.login("nobody@example.com", "x") is None
    assert accounts.by_email("CHASE@example.com").id == a.id and accounts.by_email("junk") is None


def test_memberships_name_a_member_per_org(accounts):
    a = accounts.create("chase@example.com", "", "correct horse")
    m = accounts.join(a, "acme", "owner")
    assert m.name == "chase" and m.role == "owner"
    assert accounts.join(accounts.by_id(a.id), "acme", "member").name == "chase"     # already in: the same
    b = accounts.create("chase@other.com", "", "correct horse")
    assert accounts.join(b, "acme", "member").name == "chase-2"                     # the name was taken
    assert [x["org"] for x in accounts.memberships(accounts.by_id(b.id))] == ["acme"]
    assert accounts.member_of(accounts.by_id(b.id), "nope") is None
    with pytest.raises(NotFound):
        accounts.join(b, "nope", "member")
    accounts.create_org(accounts.by_id(b.id), "beta")
    assert sorted(x["org"] for x in accounts.memberships(accounts.by_id(b.id))) == ["acme", "beta"]
    assert accounts.free_org_name("acme") == "acme-2" and accounts.free_org_name("New Team!") == "new-team"


def test_invitations_are_redeemed_on_arrival(accounts):
    accounts.invite("acme", "Dev@Example.com", "member")
    assert [i["email"] for i in accounts.invites("acme")] == ["dev@example.com"]
    with pytest.raises(Invalid):
        accounts.invite("acme", "dev@example.com", "boss")
    a = accounts.create("dev@example.com", "", "correct horse")
    assert [m.name for m in accounts.redeem_invites(a)] == ["dev"] and accounts.invites("acme") == []
    assert accounts.redeem_invites(accounts.by_id(a.id)) == []
    accounts.invite("acme", "x@example.com", "member")
    accounts.revoke_invite("acme", "x@example.com")
    with pytest.raises(NotFound):
        accounts.revoke_invite("acme", "x@example.com")


def test_tokens_carry_purpose_and_expiry(accounts):
    a = accounts.create("chase@example.com", "", "correct horse")
    accounts.join(a, "acme", "owner")
    a = accounts.by_id(a.id)
    t = accounts.token("other", a.id, 60)
    assert accounts.open_token(t, "other")["a"] == a.id and accounts.open_token(t, "session") is None
    assert accounts.open_token(t + "x", "other") is None and accounts.open_token("", "other") is None
    assert accounts.open_token(accounts.token("other", a.id, -1), "other") is None
    s = accounts.session(a, "acme")
    got = accounts.resolve_session(s)
    assert got and got[0].id == a.id and got[1].name == "chase" and got[1].org == "acme"
    assert accounts.resolve_session(accounts.session(a, "nope")) is None
    accounts.orgs.update("acme", status="suspended")
    assert accounts.resolve_session(s) is None
    accounts.orgs.update("acme", status="active")
    assert time.time() < accounts.open_token(s, "session")["e"]
