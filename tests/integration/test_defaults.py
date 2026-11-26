"""What ships: the packages are the platform's and follow it, and the environment is given once, with the first key,
and is the organization's from then on."""

import os
from pathlib import Path

from harnessgateway import defaults
from harnessgateway.domain.line import Line
from harnessgateway.platform import Platform
from harnessgateway.serve import init, opened
from harnessgateway.store.db import Database
from harnessgateway.testing import package, settings_for
from tests.integration.conftest import BY, ORG, run

SHIPS = ["harness-package-builder", "shell"]


async def test_the_defaults_are_an_environment_and_the_two_harnesses_it_carries(platform: Platform):
    await defaults.give(platform, ORG, by=BY)
    environments = await platform.resources.all(ORG, "environments")
    harnesses = {each.name: each.shown() for each in await platform.resources.all(ORG, "harnesses")}
    assert [each.name for each in environments] == ["default"] and sorted(harnesses) == SHIPS == defaults.names()
    assert environments[0].shown()["harnesses"] == SHIPS
    assert environments[0].shown()["network"]["mode"] == "open"                          # the builder reads a CLI's own pages
    assert all(set(harness) == {"name", "digest", "secrets_set", "created_at", "updated_at"} for harness in harnesses.values())
    assert (await platform.resources.resolve(ORG, Line.parse("default/shell/none/none"))).provider is None
    assert await platform.resources.all(ORG, "providers") == []            # a provider is the organization's to add
    turn = await run(platform, model="default/shell/none/none", input="anything")     # and a request can name them at once
    assert turn.status == "completed"


async def test_the_packages_that_ship_follow_the_platform_and_one_that_is_unchanged_is_left_alone(platform: Platform):
    """Every start puts each package that ships at this version's: an update of the platform updates them."""
    resources = platform.resources
    await defaults.packages(platform, ORG, by=BY)
    given = {each.name: each for each in await resources.all(ORG, "harnesses")}
    assert sorted(given) == SHIPS
    await defaults.packages(platform, ORG, by=BY)                                 # the same version again: nothing is touched
    assert {each.name: each.updated_at for each in await resources.all(ORG, "harnesses")} == {name: each.updated_at for name, each in given.items()}
    await resources.add_package(ORG, "shell", package(), by=BY)                   # as an earlier version of the platform left it
    await resources.delete(ORG, "harnesses", "harness-package-builder", by=BY)
    await defaults.packages(platform, ORG, by=BY)
    again = {each.name: each.spec["digest"] for each in await resources.all(ORG, "harnesses")}
    assert again == {name: each.spec["digest"] for name, each in given.items()}        # each is this version's again


async def test_setup_gives_the_environment_with_the_first_key_and_never_again(db: Database, tmp_path: Path):
    settings = settings_for(os.environ["HG_TEST_DATABASE_URL"], tmp_path)
    key = await init(settings)
    assert key is not None and key.startswith("hg_")
    async with opened(settings) as platform:
        assert [each.name for each in await platform.resources.all(ORG, "harnesses")] == SHIPS
        await platform.resources.delete(ORG, "environments", "default", by=BY)    # it is the organization's: it may remove it
    assert await init(settings) is None                                    # already set up
    assert (await init(settings, new_key=True)) not in (None, key)         # another key, and nothing else
    async with opened(settings) as platform:
        assert await platform.resources.all(ORG, "environments") == []     # an environment is given once
        assert await platform.orgs.all() == [ORG]
