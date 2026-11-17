"""Spec §9, §10.1, §14.1, §14.3: providers and environments as they are stored, changed and read."""

import base64
import dataclasses
from pathlib import Path

import pytest

from harnessgateway import packaging
from harnessgateway.domain.sealing import Sealer
from harnessgateway.errors import Conflict, Invalid, NotFound
from harnessgateway.store.db import Database
from harnessgateway.store.environments import Environments, Prepared
from harnessgateway.store.providers import Providers
from harnessgateway.stores import Stores
from tests.conftest import make_org
from tests.helpers import SHELL, variant

DEEPSEEK = {"name": "deepseek", "apis": {"anthropic": "https://api.deepseek.com/anthropic",
                                         "openai-chat": "https://api.deepseek.com/v1"},
            "models": ["deepseek-flash", "deepseek-v4-pro"], "api_key": "the-real-key"}


@pytest.fixture
def sealer() -> Sealer:
    return Sealer(bytes(range(32)))


@pytest.fixture
def providers(db: Database, sealer: Sealer) -> Providers:
    return Providers(db, sealer)


@pytest.fixture
def environments(db: Database, sealer: Sealer) -> Environments:
    return Environments(db, sealer)


# ── providers ───────────────────────────────────────────────────────────────────────────────────────

async def test_a_provider_is_created_and_read_without_its_key(providers: Providers, org: str) -> None:
    created = await providers.create(org, DEEPSEEK)
    assert created.has_key
    assert created.provider.models == ("deepseek-flash", "deepseek-v4-pro")
    assert await providers.get(org, "deepseek") == created
    assert "the-real-key" not in repr(created)
    assert await providers.key(org, "deepseek") == "the-real-key"


async def test_the_key_is_sealed_at_rest(providers: Providers, db: Database, org: str) -> None:
    await providers.create(org, DEEPSEEK)
    async with db.org(org) as conn:
        row = await (await conn.execute("SELECT key_sealed FROM providers WHERE org_id = %s", [org])).fetchone()
    assert row is not None
    assert b"the-real-key" not in bytes(row["key_sealed"])


async def test_a_provider_may_have_no_key(providers: Providers, org: str) -> None:
    created = await providers.create(org, {"name": "local", "apis": {"openai-chat": "http://ollama.lan:11434/v1"}})
    assert not created.has_key
    assert await providers.key(org, "local") is None


async def test_providers_are_listed_in_the_order_they_were_created(providers: Providers, org: str) -> None:
    for name in ("zeta", "alpha", "mid"):
        await providers.create(org, {**DEEPSEEK, "name": name})
    assert [record.provider.name for record in await providers.all(org)] == ["zeta", "alpha", "mid"]


async def test_a_second_provider_of_the_same_name_is_refused(providers: Providers, org: str) -> None:
    await providers.create(org, DEEPSEEK)
    with pytest.raises(Conflict, match="deepseek"):
        await providers.create(org, DEEPSEEK)


async def test_an_invalid_provider_is_refused_and_not_stored(providers: Providers, org: str) -> None:
    with pytest.raises(Invalid, match="apis"):
        await providers.create(org, {"name": "bad", "apis": {}})
    with pytest.raises(Invalid, match="api_key"):
        await providers.create(org, {**DEEPSEEK, "api_key": ""})
    with pytest.raises(Invalid, match="none is not a provider's name"):
        await providers.create(org, {**DEEPSEEK, "name": "none"})
    with pytest.raises(Invalid, match="listed twice"):
        await providers.create(org, {**DEEPSEEK, "models": ["m", "m"]})
    with pytest.raises(Invalid, match="models"):
        await providers.create(org, {**DEEPSEEK, "models": [f"m{n}" for n in range(101)]})
    with pytest.raises(Invalid, match="default_model"):
        await providers.create(org, {**DEEPSEEK, "default_model": "deepseek-flash"})
    assert await providers.all(org) == []


async def test_a_provider_is_changed_field_by_field(providers: Providers, org: str) -> None:
    await providers.create(org, DEEPSEEK)
    changed = await providers.update(org, "deepseek", {"models": ["deepseek-v4-pro"]})
    assert changed.provider.models == ("deepseek-v4-pro",)
    assert changed.provider.apis == (await providers.get(org, "deepseek")).provider.apis
    assert await providers.key(org, "deepseek") == "the-real-key"                  # untouched

    await providers.update(org, "deepseek", {"api_key": "a-new-key"})
    assert await providers.key(org, "deepseek") == "a-new-key"
    removed = await providers.update(org, "deepseek", {"api_key": None})
    assert not removed.has_key

    with pytest.raises(Invalid, match="name cannot be changed"):
        await providers.update(org, "deepseek", {"name": "other"})
    with pytest.raises(Invalid, match="apis"):
        await providers.update(org, "deepseek", {"apis": {"anthropic": "ftp://x"}})
    with pytest.raises(NotFound):
        await providers.update(org, "absent", {"models": ["x"]})


async def test_a_provider_is_deleted(providers: Providers, org: str) -> None:
    await providers.create(org, DEEPSEEK)
    await providers.delete(org, "deepseek")
    with pytest.raises(NotFound, match="deepseek"):
        await providers.get(org, "deepseek")
    with pytest.raises(NotFound):
        await providers.delete(org, "deepseek")


async def test_one_organization_s_providers_are_not_another_s(providers: Providers, db: Database, org: str) -> None:
    await providers.create(org, DEEPSEEK)
    other = await make_org(db)
    assert await providers.all(other) == []
    with pytest.raises(NotFound):
        await providers.key(other, "deepseek")


# ── environments ────────────────────────────────────────────────────────────────────────────────────

SECURE = {
    "name": "secure", "cpus": 1, "memory_mb": 1024, "tmp_mb": 256,
    "network": {"mode": "limited", "allow": ["api.example.com"]},
    "variables": {"TZ": "UTC"},
    "secrets": {"EXAMPLE_TOKEN": {"hosts": ["api.example.com"], "header": "Authorization", "prefix": "Bearer ",
                                  "value": "the-secret-value"}},
}


async def test_the_default_environment_is_made_once_and_names_no_image(environments: Environments, org: str) -> None:
    assert await environments.ensure_default(org)
    assert not await environments.ensure_default(org)
    [default] = await environments.all(org)
    assert (default.name, default.image, default.cpus, default.network.mode) == ("default", None, 2, "limited")
    assert (default.harnesses, default.setup.script) == ((), "")


async def test_an_environment_that_names_no_image_follows_the_platform_s_sandbox_image(
    stores: Stores, org: str
) -> None:
    await stores.environments.create(org, {"name": "follows"})
    pinned = await stores.environments.create(org, {"name": "pinned", "image": "acme/agents:1"})
    follows = await stores.environments.get(org, "follows")
    assert stores.image(follows) == stores.settings.sandbox_image
    assert stores.image(pinned) == "acme/agents:1"

    upgraded = Stores.of(dataclasses.replace(stores.settings, sandbox_image="sandbox:next"), stores.db, stores.blobs)
    assert upgraded.image(await upgraded.environments.get(org, "follows")) == "sandbox:next"    # a new version
    assert upgraded.image(await upgraded.environments.get(org, "pinned")) == "acme/agents:1"

    released = await stores.environments.update(org, "pinned", {"image": None})            # back to following
    assert (released.image, stores.image(released)) == (None, stores.settings.sandbox_image)


async def test_an_environment_is_created_and_its_secret_value_is_never_in_what_is_read(
    environments: Environments, db: Database, org: str
) -> None:
    created = await environments.create(org, SECURE)
    assert created.secrets["EXAMPLE_TOKEN"].header == "Authorization"
    assert await environments.get(org, "secure") == created
    assert "the-secret-value" not in str(created.dump())
    async with db.org(org) as conn:
        row = await (await conn.execute(
            "SELECT spec::text AS spec, secrets_sealed FROM environments WHERE org_id = %s", [org])).fetchone()
    assert row is not None
    assert "the-secret-value" not in row["spec"]
    assert b"the-secret-value" not in base64.b64decode(row["secrets_sealed"]["EXAMPLE_TOKEN"])
    assert await environments.secret_values(org, "secure") == {"EXAMPLE_TOKEN": "the-secret-value"}


async def test_a_secret_without_a_value_is_refused(environments: Environments, org: str) -> None:
    with pytest.raises(Invalid, match=r"secrets\.EXAMPLE_TOKEN\.value is required"):
        await environments.create(org, {**SECURE, "secrets": {"EXAMPLE_TOKEN": {"hosts": ["api.example.com"]}}})
    with pytest.raises(Invalid, match="disk_mb"):
        await environments.create(org, {"name": "bad", "disk_mb": 1})
    with pytest.raises(Invalid, match="nmae"):
        await environments.create(org, {"name": "bad", "nmae": 1})
    assert await environments.all(org) == []


async def test_changing_an_environment_replaces_the_fields_named_and_keeps_the_rest(
    environments: Environments, org: str
) -> None:
    await environments.create(org, SECURE)
    changed = await environments.update(org, "secure", {"memory_mb": 2048, "network": {"mode": "open"}})
    assert (changed.memory_mb, changed.cpus, changed.network.mode, changed.network.allow) == (2048, 1, "open", ())
    assert changed.variables == {"TZ": "UTC"}
    assert await environments.secret_values(org, "secure") == {"EXAMPLE_TOKEN": "the-secret-value"}


async def test_a_secret_listed_again_without_a_value_keeps_the_one_it_had(
    environments: Environments, org: str
) -> None:
    await environments.create(org, SECURE)
    await environments.update(org, "secure", {"secrets": {
        "EXAMPLE_TOKEN": {"hosts": ["api.example.com", "other.example.com"]},
        "SECOND": {"hosts": ["second.example.com"], "value": "second-value"},
    }})
    assert await environments.secret_values(org, "secure") == {
        "EXAMPLE_TOKEN": "the-secret-value", "SECOND": "second-value"}

    await environments.update(org, "secure", {"secrets": {"SECOND": {"hosts": ["second.example.com"],
                                                                     "value": "changed"}}})
    assert await environments.secret_values(org, "secure") == {"SECOND": "changed"}
    with pytest.raises(Invalid, match=r"secrets\.THIRD\.value is required"):
        await environments.update(org, "secure", {"secrets": {"THIRD": {"hosts": ["x.example.com"]}}})


async def test_an_environment_s_name_cannot_be_changed_and_a_deleted_one_is_gone(
    environments: Environments, org: str
) -> None:
    await environments.ensure_default(org)
    await environments.create(org, SECURE)
    with pytest.raises(Invalid, match="name cannot be changed"):
        await environments.update(org, "secure", {"name": "other"})
    await environments.delete(org, "secure")
    with pytest.raises(NotFound):
        await environments.get(org, "secure")
    with pytest.raises(NotFound):
        await environments.update(org, "secure", {"cpus": 1})
    with pytest.raises(NotFound):
        await environments.delete(org, "secure")
    with pytest.raises(Conflict):
        await environments.create(org, {"name": "default"})


async def test_the_default_environment_is_deleted_like_any_other(environments: Environments, org: str) -> None:
    await environments.ensure_default(org)
    await environments.delete(org, "default")
    assert await environments.all(org) == []
    with pytest.raises(NotFound):
        await environments.get(org, "default")
    assert await environments.ensure_default(org)                     # made again only when asked


async def test_an_environment_carries_only_harnesses_the_organization_has(
    stores: Stores, org: str, tmp_path: Path
) -> None:
    await stores.harnesses.add(org, packaging.pack(SHELL))
    await stores.harnesses.add(org, variant(tmp_path, "second"))
    with pytest.raises(Invalid, match="there is no harness named 'absent'") as refused:
        await stores.environments.create(org, {"name": "gpu", "harnesses": ["shell", "absent"]})
    assert refused.value.param == "harnesses"
    with pytest.raises(Invalid, match="there is no harness named 'absent'") as refused:
        await stores.environments.update(org, "default", {"harnesses": ["absent"]})
    assert refused.value.param == "harnesses"
    with pytest.raises(Invalid, match="listed twice"):
        await stores.environments.create(org, {"name": "gpu", "harnesses": ["shell", "shell"]})
    assert [e.name for e in await stores.environments.all(org)] == ["default"]
    assert (await stores.environments.get(org, "default")).harnesses == ()

    created = await stores.environments.create(org, {"name": "gpu", "harnesses": ["shell", "second"]})
    assert created.harnesses == ("second", "shell")                    # kept sorted
    assert (await stores.environments.get(org, "gpu")).harnesses == ("second", "shell")


async def test_deleting_a_harness_takes_it_off_every_environment_that_carries_it(
    stores: Stores, org: str, tmp_path: Path
) -> None:
    await stores.harnesses.add(org, packaging.pack(SHELL))
    await stores.harnesses.add(org, variant(tmp_path, "second"))
    await stores.environments.update(org, "default", {"harnesses": ["second", "shell"]})
    await stores.environments.create(org, {"name": "gpu", "harnesses": ["shell"]})
    await stores.environments.create(org, {"name": "plain"})

    await stores.harnesses.delete(org, "shell")
    carried = {e.name: e.harnesses for e in await stores.environments.all(org)}
    assert carried == {"default": ("second",), "gpu": (), "plain": ()}
    await stores.environments.update(org, "gpu", {"cpus": 1})            # what is left is valid as it stands


async def test_what_an_environment_was_last_prepared_as_is_kept(stores: Stores, org: str) -> None:
    await stores.environments.create(org, {"name": "gpu"})
    assert await stores.environments.prepared(org) == {"default": Prepared(None, None), "gpu": Prepared(None, None)}

    await stores.environments.set_prepared(org, "gpu", Prepared("sha256:built", "bld_one"))
    assert await stores.environments.prepared(org) == {
        "default": Prepared(None, None), "gpu": Prepared("sha256:built", "bld_one")}
    await stores.environments.update(org, "gpu", {"cpus": 1})              # a change alone does not forget it
    assert (await stores.environments.prepared(org))["gpu"] == Prepared("sha256:built", "bld_one")

    await stores.environments.set_prepared(org, "gpu", Prepared(None, "bld_two"))     # its build failed
    assert (await stores.environments.prepared(org))["gpu"] == Prepared(None, "bld_two")
    await stores.environments.delete(org, "gpu")
    assert await stores.environments.prepared(org) == {"default": Prepared(None, None)}
