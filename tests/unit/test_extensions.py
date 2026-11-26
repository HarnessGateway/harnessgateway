"""Extensions as a chain: what several of them together mean at each seam, with nothing but the extensions."""

from collections.abc import Callable
from typing import Any

import pytest

from harnessgateway.domain.egress import EgressPolicy
from harnessgateway.domain.principal import Principal
from harnessgateway.driver.base import Driver
from harnessgateway.extensions import Extension, Extensions
from harnessgateway.settings import Settings, SettingsError
from harnessgateway.store.blobs import BlobStore


class Named(Extension):
    def __init__(self, name: str, *, knows: str = "", **offers: Any) -> None:
        self.name, self.version, self._knows, self._offers = name, "1", knows, offers
        self.asked: list[str] = []

    def drivers(self) -> dict[str, Callable[[Settings], Driver]]:
        return self._offers.get("drivers", {})

    def blob_stores(self) -> dict[str, Callable[[str], BlobStore]]:
        return self._offers.get("blob_stores", {})

    def sealer(self, settings: Settings) -> Any:
        return self._offers.get("sealer")

    async def authenticate(self, request: Any) -> Principal | None:
        self.asked.append("authenticate")
        return Principal("org", self._knows) if self._knows else None

    async def authorize(self, who: Principal, action: str) -> None:
        self.asked.append(action)


@pytest.mark.parametrize(("names", "why"), [(("",), "an extension's name is 1 to 63 characters"), (("Not A Name",), "an extension's name"),
                                            (("core",), "the name 'core' is taken"), (("a", "b", "a"), "the name 'a' is taken")])
def test_an_extension_has_a_name_of_its_own(names: tuple[str, ...], why: str):
    with pytest.raises(SettingsError, match=why):
        Extensions([Named(name) for name in names])


def test_a_part_is_offered_once_under_a_name_and_the_cores_own_is_never_replaced():
    def make(_: Any) -> Any:
        return None
    one, two = Named("one", drivers={"cluster": make}, blob_stores={"s3": make}), Named("two", drivers={"other": make})
    both = Extensions([one, two])
    assert sorted(both.drivers({"docker": make})) == ["cluster", "docker", "other"] and sorted(both.blob_stores()) == ["s3"]
    for again, why in ((Named("three", drivers={"cluster": make}), "three offers the driver 'cluster', which is already offered"),
                       (Named("three", drivers={"docker": make}), "three offers the driver 'docker', which is already offered"),
                       (Named("three", blob_stores={"s3": make}), "three offers a blob store for 's3', which is already offered")):
        with pytest.raises(SettingsError, match=why):
            chain = Extensions([one, two, again])
            chain.drivers({"docker": make}), chain.blob_stores()
    assert Extensions([Named("a"), Named("b", sealer="b's"), Named("c", sealer="c's")]).sealer(None) == "b's"     # the first that gives one
    assert Extensions().sealer(None) is None and Extensions().described() == []
    assert both.described() == [{"name": "one", "version": "1"}, {"name": "two", "version": "1"}]
    assert both.named("two") is two and both.named("three") is None             # how one that stands on another finds it


async def test_the_first_extension_that_knows_the_caller_answers_and_every_one_is_asked_whether_they_may():
    first, second, third = Named("first"), Named("second", knows="pat"), Named("third", knows="sam")
    chain = Extensions([first, second, third])
    assert await chain.authenticate(None) == Principal("org", "pat")
    assert (first.asked, second.asked, third.asked) == (["authenticate"], ["authenticate"], [])       # the third is never asked
    await chain.authorize(Principal("org", "pat"), "keys.write")
    assert [each.asked[-1] for each in (first, second, third)] == ["keys.write"] * 3
    assert await Extensions([first]).authenticate(None) is None                                        # then an API key is asked for
    assert await Extensions().egress("org", EgressPolicy("open")) == EgressPolicy("open")              # the core alone: as it is
