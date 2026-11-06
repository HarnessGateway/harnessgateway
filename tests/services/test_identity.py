"""Accounts, sign-ins, keys and the audit log (SPEC §9.2, §9.3, §9.5)."""
import pytest

from harnessgateway.domain.access import Principal
from harnessgateway.errors import Conflict, Forbidden, Invalid, Locked, NotFound, Unauthenticated

from .conftest import token_of

PASSWORD = "a-long-enough-password"


async def invite(plat, org, email, role="developer", **kw):
    a = await plat.accounts.create(org.id, org.admin.actor, email, "Someone", role, **kw)
    assert a["status"] == "invited" and a["invitation"].startswith("http://console.test:8800/setup?token=hgi_")
    assert a["sent"] is False                                   # no mail configured: the link is the way
    return a


async def test_an_invitation_activates_once_and_signs_in(plat, org):
    a = await invite(plat, org, f"one-{org.id[-6:]}@example.test")
    with pytest.raises(Invalid, match="12 characters"):
        await plat.accounts.accept(token_of(a["invitation"]), "short", "test")
    signed = await plat.accounts.accept(token_of(a["invitation"]), PASSWORD, "test")
    assert signed["token"].startswith("hgl_") and signed["account"]["status"] == "active"
    assert signed["memberships"] == [{"org_id": org.id, "role": "developer"}]
    with pytest.raises(Invalid, match="unknown, used or expired"):
        await plat.accounts.accept(token_of(a["invitation"]), PASSWORD, "test")
    found = await plat.accounts.authenticate(signed["token"])
    assert found["account"]["id"] == a["id"] and found["memberships"][0]["role"] == "developer"
    await plat.accounts.logout(signed["token"])
    assert await plat.accounts.authenticate(signed["token"]) is None


async def test_wrong_passwords_lock_the_account_and_a_reset_ends_every_sign_in(plat, org):
    email = f"lock-{org.id[-6:]}@example.test"
    a = await invite(plat, org, email)
    signed = await plat.accounts.accept(token_of(a["invitation"]), PASSWORD, "test")
    for _ in range(10):
        with pytest.raises(Unauthenticated):
            await plat.accounts.login(email, "wrong-password-xx", "test")
    with pytest.raises(Locked):
        await plat.accounts.login(email, PASSWORD, "test")          # the right password, while locked
    actions = [r["action"] for r in (await plat.audit.list(org.id, 100, None))["data"]]
    assert "auth.lock" in actions and "auth.locked" in actions and actions.count("auth.fail") == 9
    reset = await plat.accounts.reset(org.id, org.admin.actor, a["id"])
    assert reset["invitation"] != a["invitation"]
    assert await plat.accounts.authenticate(signed["token"]) is None   # the reset ended it
    again = await plat.accounts.accept(token_of(reset["invitation"]), PASSWORD + "2", "test")
    assert (await plat.accounts.login(email, PASSWORD + "2", "test"))["token"] != again["token"]


async def test_roles_and_the_last_admin(plat, org):
    first = await invite(plat, org, f"adm-{org.id[-6:]}@example.test", role="admin")
    await plat.accounts.accept(token_of(first["invitation"]), PASSWORD, "test")
    with pytest.raises(Conflict, match="at least one active admin"):
        await plat.accounts.update(org.id, org.admin.actor, first["id"], role="developer")
    second = await invite(plat, org, f"adm2-{org.id[-6:]}@example.test", role="admin")
    await plat.accounts.accept(token_of(second["invitation"]), PASSWORD, "test")
    demoted = await plat.accounts.update(org.id, org.admin.actor, first["id"], role="developer")
    assert demoted["role"] == "developer"
    with pytest.raises(Invalid, match="platform admin"):
        await plat.accounts.update(org.id, org.admin.actor, first["id"], platform_admin=True)
    granted = await plat.accounts.update(org.id, "account:platform", first["id"], platform_admin=True, by_platform_admin=True)
    assert granted["platform_admin"] is True
    with pytest.raises(Invalid):
        await plat.accounts.create(org.id, org.admin.actor, "not-an-email", "", "developer")
    with pytest.raises(Conflict, match="already a member"):
        await invite(plat, org, first["email"])
    with pytest.raises(NotFound):
        await plat.accounts.get(org.id, "acct_" + "0" * 32)


async def test_disabling_an_account_revokes_its_sign_ins_and_keys(plat, org):
    a = await invite(plat, org, f"dis-{org.id[-6:]}@example.test")
    signed = await plat.accounts.accept(token_of(a["invitation"]), PASSWORD, "test")
    me = Principal(org.id, f"account:{a['id']}", "developer", account_id=a["id"])
    key = await plat.keys.create(me, "my-app", "developer", True, None)
    assert (await plat.keys.authenticate(key["key"])).account_id == a["id"]
    disabled = await plat.accounts.update(org.id, org.admin.actor, a["id"], status="disabled")
    assert disabled["status"] == "disabled"
    assert await plat.accounts.authenticate(signed["token"]) is None
    assert await plat.keys.authenticate(key["key"]) is None
    with pytest.raises(Unauthenticated):
        await plat.accounts.login(a["email"], PASSWORD, "test")
    enabled = await plat.accounts.update(org.id, org.admin.actor, a["id"], status="active")
    assert enabled["status"] == "active"


async def test_keys_are_named_after_their_app_and_scoped_by_role(plat, org):
    with pytest.raises(Invalid, match="label"):
        await plat.keys.create(org.admin, "", "developer", True, None)
    with pytest.raises(Invalid, match="run_only"):
        await plat.keys.create(org.admin, "x", "admin", True, None)
    with pytest.raises(Forbidden):
        await plat.keys.create(org.developer, "x", "admin", False, None)
    a = await invite(plat, org, f"dev-{org.id[-6:]}@example.test")
    dev = Principal(org.id, f"account:{a['id']}", "developer", account_id=a["id"])
    mine = await plat.keys.create(dev, "market-desk", "developer", True, 30)
    theirs = await plat.keys.create(org.admin, "init", "admin", False, None)
    assert mine["run_only"] and mine["expires_at"] and mine["created_by"] == a["id"]
    assert [k["id"] for k in await plat.keys.list(dev)] == [mine["id"]]
    assert {k["id"] for k in await plat.keys.list(org.admin)} >= {mine["id"], theirs["id"]}
    p = await plat.keys.authenticate(mine["key"])
    assert p.run_only and p.owns_only and p.name == "market-desk" and p.actor == f"key:{mine['id']}"
    with pytest.raises(Forbidden):
        await plat.keys.revoke(dev, theirs["id"])
    await plat.keys.revoke(dev, mine["id"])
    assert await plat.keys.authenticate(mine["key"]) is None
    async with plat.db.platform() as tx:
        await tx.run("UPDATE api_keys SET expires_at = now() - interval '1 minute' WHERE id = %s", theirs["id"])
    assert await plat.keys.authenticate(theirs["key"]) is None       # expired
    assert await plat.keys.authenticate("hg_" + "x" * 40) is None and await plat.keys.authenticate("nonsense") is None


async def test_every_change_of_standing_is_audited_in_its_organization(plat, org):
    a = await invite(plat, org, f"aud-{org.id[-6:]}@example.test")
    await plat.accounts.accept(token_of(a["invitation"]), PASSWORD, "test")
    await plat.keys.create(org.admin, "app", "developer", True, None)
    rows = (await plat.audit.list(org.id, 100, None))["data"]
    assert [r["action"] for r in rows][:3] == ["key.create", "account.activate", "account.create"]   # newest first
    assert all(r["org_id"] == org.id for r in rows) and PASSWORD not in str(rows)
    everything = (await plat.audit.list(None, 100, None))["data"]
    assert {r["id"] for r in rows} <= {r["id"] for r in everything}
    page = await plat.audit.list(org.id, 2, None)
    assert page["has_more"] and len(page["data"]) == 2
    rest = await plat.audit.list(org.id, 100, page["last_id"])
    assert rows[2]["id"] == rest["data"][0]["id"]


async def test_init_checks_the_email_before_it_creates_anything(settings, scratch_database):
    """`init` on an empty platform: a bad email creates nothing; a good one creates the organization, the builder, the
    admin's invitation and a key, once; a rerun changes nothing and returns neither secret."""
    import dataclasses

    from harnessgateway.services.platform import Platform

    plat = Platform(dataclasses.replace(settings, database_url=scratch_database()))
    await plat.open()
    try:
        await plat.migrate()
        with pytest.raises(Invalid, match="email"):
            await plat.init("default", "")
        assert await plat.orgs.first() is None
        out = await plat.init("default", "Deployer@Example.test")
        assert out["admin"]["email"] == "deployer@example.test" and out["admin"]["platform_admin"] is True
        assert out["admin"]["invitation"].startswith("http://console.test:8800/setup?token=hgi_")
        assert out["admin_key"].startswith("hg_") and (await plat.keys.authenticate(out["admin_key"])).role == "admin"
        assert [h["name"] for h in await plat.harnesses.list(await plat.keys.authenticate(out["admin_key"]))] == \
            ["harness-package-builder"]
        again = await plat.init("default", "someone-else@example.test")
        assert "admin" not in again and "already initialized" in again["note"]
    finally:
        await plat.close()
