"""Spec §9, §10.1, §14.1, §14.3: providers and environments as they are stored, changed and read."""

import base64

import pytest

from harnessgateway.domain.sealing import Sealer
from harnessgateway.errors import Conflict, Invalid, NotFound
from harnessgateway.store.db import Database
from harnessgateway.store.environments import Environments
from harnessgateway.store.providers import Providers
from tests.conftest import make_org

DEEPSEEK = {"name": "deepseek", "apis": {"anthropic": "https://api.deepseek.com/anthropic",
                                         "openai-chat": "https://api.deepseek.com/v1"},
            "default_model": "deepseek-flash", "api_key": "the-real-key"}


@pytest.fixture
def sealer() -> Sealer:
    return Sealer(bytes(range(32)))


@pytest.fixture
def providers(db: Database, sealer: Sealer) -> Providers:
    return Providers(db, sealer)


@pytest.fixture
def environments(db: Database, sealer: Sealer) -> Environments:
    return Environments(db, sealer, default_image="sandbox:test")


# ── providers ───────────────────────────────────────────────────────────────────────────────────────

async def test_a_provider_is_created_and_read_without_its_key(providers: Providers, org: str) -> None:
    created = await providers.create(org, DEEPSEEK)
    assert created.has_key
    assert created.provider.default_model == "deepseek-flash"
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
    assert await providers.all(org) == []


async def test_a_provider_is_changed_field_by_field(providers: Providers, org: str) -> None:
    await providers.create(org, DEEPSEEK)
    changed = await providers.update(org, "deepseek", {"default_model": "deepseek-v4-pro"})
    assert changed.provider.default_model == "deepseek-v4-pro"
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
        await providers.update(org, "absent", {"default_model": "x"})


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


async def test_the_default_environment_is_made_once_with_the_platform_s_image(
    environments: Environments, org: str
) -> None:
    await environments.ensure_default(org)
    await environments.ensure_default(org)
    [default] = await environments.all(org)
    assert (default.name, default.image, default.cpus, default.network.mode) == (
        "default", "sandbox:test", 2, "limited")


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


async def test_an_environment_s_name_cannot_be_changed_and_the_default_cannot_be_deleted(
    environments: Environments, org: str
) -> None:
    await environments.ensure_default(org)
    await environments.create(org, SECURE)
    with pytest.raises(Invalid, match="name cannot be changed"):
        await environments.update(org, "secure", {"name": "other"})
    with pytest.raises(Invalid, match="not deleted"):
        await environments.delete(org, "default")
    await environments.delete(org, "secure")
    with pytest.raises(NotFound):
        await environments.get(org, "secure")
    with pytest.raises(NotFound):
        await environments.update(org, "secure", {"cpus": 1})
    with pytest.raises(Conflict):
        await environments.create(org, {"name": "default"})
