"""Spec §16: API keys and the organization."""

import pytest

from harnessgateway.domain.principal import Principal
from harnessgateway.errors import Forbidden, Invalid, NotFound
from harnessgateway.stores import Stores
from tests.conftest import make_org


async def test_a_key_is_shown_once_and_only_its_hash_is_kept(stores: Stores, org: str) -> None:
    key, text = await stores.keys.create(org, name="the app", role="runner")
    assert text.startswith("hg_")
    assert len(text) == 43
    assert (key.name, key.role, key.revoked_at, key.last_used_at) == ("the app", "runner", None, None)
    assert text not in repr(key)
    async with stores.db.org(org) as conn:
        row = await (await conn.execute("SELECT * FROM keys WHERE id = %s", [key.id])).fetchone()
    assert row is not None
    assert text not in str(row)


async def test_a_key_authenticates_as_its_organization_and_role(stores: Stores, org: str) -> None:
    key, text = await stores.keys.create(org, name="ops", role="admin")
    assert await stores.keys.authenticate(text) == Principal(org, key.id, "admin")
    [listed] = await stores.keys.all(org)
    assert listed.last_used_at is not None
    assert await stores.keys.authenticate("hg_" + "x" * 40) is None
    assert await stores.keys.authenticate("sk-something-else") is None
    assert await stores.keys.authenticate("") is None


async def test_a_revoked_key_no_longer_authenticates(stores: Stores, org: str) -> None:
    key, text = await stores.keys.create(org, name="old", role="developer")
    await stores.keys.revoke(org, key.id)
    assert await stores.keys.authenticate(text) is None
    assert (await stores.keys.all(org))[0].revoked_at is not None
    with pytest.raises(NotFound):
        await stores.keys.revoke(org, key.id)
    with pytest.raises(NotFound):
        await stores.keys.revoke(await make_org(stores.db), key.id)


async def test_a_key_needs_a_name_and_a_known_role(stores: Stores, org: str) -> None:
    with pytest.raises(Invalid, match="role is one of runner, developer, admin"):
        await stores.keys.create(org, name="x", role="owner")
    with pytest.raises(Invalid, match="name"):
        await stores.keys.create(org, name="", role="runner")


def test_each_role_may_do_what_the_ones_below_it_may() -> None:
    admin, developer, runner = (Principal("org", "key", role) for role in ("admin", "developer", "runner"))
    admin.require("admin")
    admin.require("runner")
    developer.require("developer")
    runner.require("runner")
    with pytest.raises(Forbidden, match="needs a key with the role admin; this key's role is developer"):
        developer.require("admin")
    with pytest.raises(Forbidden):
        runner.require("developer")


async def test_the_organization_is_made_once_and_its_retention_can_be_set(stores: Stores) -> None:
    await stores.orgs.ensure("org_made_here", "Acme")
    await stores.orgs.ensure("org_made_here", "Another name")
    org = await stores.orgs.get("org_made_here")
    assert (org.name, org.retention_days) == ("Acme", 0)
    assert (await stores.orgs.set_retention(org.id, 30)).retention_days == 30
    with pytest.raises(Invalid, match="retention_days"):
        await stores.orgs.set_retention(org.id, -1)
    with pytest.raises(NotFound):
        await stores.orgs.get("org_absent")
    async with stores.db.system() as conn:
        await conn.execute("DELETE FROM orgs WHERE id = 'org_made_here'")
