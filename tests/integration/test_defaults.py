"""First-time setup: the defaults are given once, with the first key, and are the organization's from then on."""

import os
from pathlib import Path

from harnessgateway import defaults
from harnessgateway.domain.line import Line
from harnessgateway.platform import Platform
from harnessgateway.serve import init, opened
from harnessgateway.store.db import Database
from tests.integration.conftest import ORG, run, settings_for


async def test_the_defaults_are_an_environment_and_the_two_harnesses_it_carries(platform: Platform):
    await defaults.give(platform, ORG)
    environments = await platform.resources.all(ORG, "environments")
    harnesses = {each.name: each.shown() for each in await platform.resources.all(ORG, "harnesses")}
    assert [each.name for each in environments] == ["default"] and sorted(harnesses) == ["hg-fde", "shell"]
    assert environments[0].shown()["harnesses"] == ["hg-fde", "shell"]
    assert environments[0].shown()["network"]["mode"] == "open"                          # the engineer reads a CLI's own pages
    assert all(set(harness) == {"name", "digest", "secrets_set", "created_at", "updated_at"} for harness in harnesses.values())
    assert (await platform.resources.resolve(ORG, Line.parse("default/shell/none/none"))).provider is None
    assert await platform.resources.all(ORG, "providers") == []            # a provider is the organization's to add
    turn = await run(platform, model="default/shell/none/none", input="anything")     # and a request can name them at once
    assert turn.status == "completed"


async def test_setup_gives_the_defaults_with_the_first_key_and_never_again(db: Database, tmp_path: Path):
    settings = settings_for(os.environ["HG_TEST_DATABASE_URL"], tmp_path)
    key = await init(settings)
    assert key is not None and key.startswith("hg_")
    async with opened(settings) as platform:
        assert len(await platform.resources.all(ORG, "harnesses")) == 2
        await platform.resources.delete(ORG, "environments", "default")
        await platform.resources.delete(ORG, "harnesses", "shell")         # they are the organization's: it may remove one
    assert await init(settings) is None                                    # already set up
    assert (await init(settings, new_key=True)) not in (None, key)         # another key, and nothing else
    async with opened(settings) as platform:
        assert [each.name for each in await platform.resources.all(ORG, "harnesses")] == ["hg-fde"]
        assert await platform.resources.all(ORG, "environments") == []
