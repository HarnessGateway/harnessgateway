"""Row-level security: one organization never sees another's rows, through the services or through a scoped
transaction, whatever the query says (SPEC §8.4)."""
import pytest

from harnessgateway.errors import NotFound

from .conftest import SHELL, Org, pack


async def test_harnesses_sessions_and_keys_stay_in_their_organization(plat, org, shell):
    other = Org(str((await plat.orgs.create("other-" + org.id[-6:]))["id"]))
    assert [h["name"] for h in await plat.harnesses.list(org.admin)] == ["shell"]
    assert await plat.harnesses.list(other.admin) == []
    with pytest.raises(NotFound):
        await plat.harnesses.get(other.admin, "shell")
    turn = await plat.turns.create(org.admin, {"model": "shell", "input": "echo hi"})
    assert turn["status"] == "queued"
    assert [c["id"] for c in (await plat.containers.list(org.admin, 10, None, "desc"))["data"]] == [turn["session_id"]]
    assert (await plat.containers.list(other.admin, 10, None, "desc"))["data"] == []
    with pytest.raises(NotFound):
        await plat.turns.row(other.admin, turn["id"])
    key = await plat.keys.create(org.admin, "app", "developer", True, None)
    assert [k["id"] for k in await plat.keys.list(other.admin)] == []
    assert (await plat.keys.authenticate(key["key"])).org_id == org.id


async def test_a_scoped_transaction_cannot_read_across_even_with_a_broad_query(plat, org, shell):
    other = str((await plat.orgs.create("other2-" + org.id[-6:]))["id"])
    async with plat.db.tenant(other) as tx:
        assert await tx.value("SELECT count(*) FROM harnesses") == 0
        assert await tx.value("SELECT count(*) FROM harnesses WHERE org_id = %s", org.id) == 0
        assert await tx.run("DELETE FROM harnesses") == 0                      # nothing to delete from here
    async with plat.db.tenant(org.id) as tx:
        assert await tx.value("SELECT count(*) FROM harnesses") == 1
    async with plat.db.platform() as tx:
        assert await tx.value("SELECT count(*) FROM harnesses WHERE org_id = %s", org.id) == 1


async def test_a_write_into_another_organization_is_refused_by_the_database(plat, org):
    other = str((await plat.orgs.create("other3-" + org.id[-6:]))["id"])
    async with plat.db.tenant(org.id) as tx:
        with pytest.raises(Exception, match="row-level security"):
            await tx.run("INSERT INTO api_keys (id, org_id, digest, role, label, hint) VALUES "
                         "('key_' || repeat('1', 32), %s, 'd', 'admin', 'x', 'abcd')", other)


async def test_the_same_package_is_stored_once_but_installed_per_organization(plat, org, shell):
    other = Org(str((await plat.orgs.create("other4-" + org.id[-6:]))["id"]))
    again = await plat.harnesses.install(other.admin, pack(SHELL))
    assert again["digest"] == shell["digest"]
    async with plat.db.platform() as tx:
        assert await tx.value("SELECT count(*) FROM packages WHERE digest = %s", shell["digest"]) == 1
        assert await tx.value("SELECT count(*) FROM installs WHERE digest = %s AND org_id = ANY(%s)", shell["digest"],
                              [org.id, other.id]) == 2
