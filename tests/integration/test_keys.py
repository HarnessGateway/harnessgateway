"""Spec §16: API keys and the organization."""

import dataclasses
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from harnessgateway.catalog import Catalog
from harnessgateway.domain.names import DEFAULT_ORG
from harnessgateway.domain.principal import Principal
from harnessgateway.errors import Forbidden, Invalid, NotFound, Refused
from harnessgateway.serve import init
from harnessgateway.settings import Settings
from harnessgateway.stores import Stores
from tests.conftest import make_org
from tests.helpers import SHELL


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


@pytest.fixture
async def no_default_org(stores: Stores) -> AsyncIterator[None]:
    """Setup makes the organization `org_default`, which no other test uses: it starts without it, and it is
    removed afterwards."""
    async def remove() -> None:
        async with stores.db.system() as conn:
            await conn.execute("DELETE FROM orgs WHERE id = %s", [DEFAULT_ORG])
    await remove()
    yield
    await remove()


async def test_setup_makes_the_first_admin_key_once_and_a_lost_one_is_replaced_from_the_host(
    stores: Stores, settings: Settings, no_default_org: None,
) -> None:
    first = await init(settings)
    assert first is not None
    assert await init(settings) is None                                 # run again, it changes nothing
    another = await init(settings, new_admin_key=True)                  # whoever can run this owns the platform
    assert another is not None
    assert another != first
    for text in (first, another):
        principal = await stores.keys.authenticate(text)
        assert principal is not None
        assert (principal.org_id, principal.role) == ("org_default", "admin")
    assert [key.name for key in await stores.keys.all("org_default")] == [
        "first admin key", "admin key made on the host"]


async def test_setup_adds_the_packages_that_ship_and_a_default_environment_that_carries_them_once(
    stores: Stores, settings: Settings, no_default_org: None,
) -> None:
    shipped = dataclasses.replace(settings, catalog=str(SHELL.parent))
    names = [manifest.name for manifest in Catalog(SHELL.parent).entries()]
    assert "shell" in names
    assert await init(shipped) is not None
    assert [harness.name for harness in await stores.harnesses.all(DEFAULT_ORG)] == names
    [default] = await stores.environments.all(DEFAULT_ORG)
    assert (default.name, default.harnesses) == ("default", tuple(names))

    for name in names:
        await stores.harnesses.delete(DEFAULT_ORG, name)
    await stores.environments.delete(DEFAULT_ORG, "default")
    assert await init(shipped) is None                     # what was set up once is the organization's to change
    assert await stores.harnesses.all(DEFAULT_ORG) == []
    assert await stores.environments.all(DEFAULT_ORG) == []


async def test_the_default_environment_is_set_up_as_the_catalog_says_its_packages_need(
    stores: Stores, settings: Settings, no_default_org: None, tmp_path: Path,
) -> None:
    shutil.copytree(SHELL, tmp_path / "shell")
    (tmp_path / "default-environment.json").write_text('{"setup": {"egress": ["downloads.example"]}, "disk_mb": 20480}')
    assert await init(dataclasses.replace(settings, catalog=str(tmp_path))) is not None
    [default] = await stores.environments.all(DEFAULT_ORG)
    assert (default.name, default.harnesses) == ("default", ("shell",))
    assert ([entry.host for entry in default.setup.egress], default.disk_mb) == (["downloads.example"], 20480)
    assert default.network.allow == ()                     # what the file does not name is as any new environment's


@pytest.mark.parametrize("said", ['{"name": "other"}', '{"harnesses": []}', '["shell"]', "not json"])
def test_a_catalog_says_only_how_the_default_environment_is_set_up(tmp_path: Path, said: str) -> None:
    (tmp_path / "default-environment.json").write_text(said)
    with pytest.raises(Refused, match=r"default-environment\.json"):
        Catalog(tmp_path).default_environment()
    assert Catalog(tmp_path / "absent").default_environment() == {}
