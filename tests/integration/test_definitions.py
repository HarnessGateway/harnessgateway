"""Providers, environments and harnesses, as they are written and kept."""

import json

import pytest

from harnessgateway.api.responses import accept
from harnessgateway.errors import Invalid, NotFound, Refused
from harnessgateway.platform import Platform
from tests.helpers import package
from tests.integration.conftest import LINE, ORG, WHO, ready


async def test_a_secret_is_sealed_kept_when_not_sent_and_never_shown(platform: Platform):
    resources = platform.resources
    provider = await resources.apply(ORG, "providers", "deepseek", {
        "apis": {"openai-responses": "https://api.deepseek.com/v1/"}, "key": "sk-real"})
    assert provider.shown()["apis"] == {"openai-responses": "https://api.deepseek.com/v1"}
    assert provider.shown()["secrets_set"] == ["key"] and "sk-real" not in json.dumps(provider.shown())
    assert "sk-real" not in json.dumps(provider.sealed)
    assert await resources.secrets(ORG, "providers", "deepseek") == {"key": "sk-real"}
    kept = await resources.apply(ORG, "providers", "deepseek", {"apis": {"anthropic": "https://api.deepseek.com/anthropic"}})
    assert kept.shown()["secrets_set"] == ["key"]
    gone = await resources.apply(ORG, "providers", "deepseek", {**kept.spec, "key": None})
    assert gone.shown()["secrets_set"] == []


@pytest.mark.parametrize(("kind", "name", "sent", "param"), [
    ("providers", "none", {"apis": {"anthropic": "https://a.example.com"}}, "name"),
    ("providers", "Bad Name", {"apis": {"anthropic": "https://a.example.com"}}, "name"),
    ("providers", "p", {"apis": {"anthropic": "https://a.example.com"}, "models": ["m"]}, "models"),
    ("providers", "p", {"apis": {"anthropic": "ftp://a.example.com"}}, "apis.anthropic"),
    ("providers", "p", {"apis": {}}, "apis"),
    ("environments", "e", {"harnesses": []}, "image"),
    ("environments", "e", {"image": "x", "harnesses": ["ghost"]}, "harnesses"),
    ("environments", "e", {"image": "x", "memory_mb": "4096"}, "memory_mb"),
    ("environments", "e", {"image": "x", "variables": {"HG_TURN": "x"}}, ""),
    ("environments", "e", {"image": "x", "network": {"mode": "closed", "allow": ["a.example.com"]}}, "network"),
    ("environments", "e", {"image": "x", "network": {"allow": ["https://a.example.com/x"]}}, "network.allow"),
    ("environments", "e", {"image": "x", "secrets": {"TOKEN": {"hosts": ["a.example.com"]}}}, "secrets.TOKEN.value"),
    ("harnesses", "new", {"settings": {}}, None),
])
async def test_a_definition_that_cannot_work_is_refused_by_the_field_that_is_wrong(platform: Platform, kind, name, sent, param):
    with pytest.raises(Refused) as refusal:
        await platform.resources.apply(ORG, kind, name, sent)
    assert refusal.value.param == (param or None), refusal.value.message


async def test_a_harness_is_its_package_and_nothing_else(platform: Platform):
    resources = platform.resources
    added = await resources.add_package(ORG, "agent", package())
    assert added.spec == {"digest": added.spec["digest"]} and added.sealed == {}
    with pytest.raises(Invalid, match="a harness is its package"):                 # there is nothing to set on one
        await resources.apply(ORG, "harnesses", "agent", {"settings": {"TEAM": "payments"}})
    same = await resources.add_package(ORG, "twin", package())                     # the same files under another name
    assert same.spec["digest"] == added.spec["digest"]
    changed = await resources.add_package(ORG, "agent", package(**{"README.md": b"new"}))
    assert changed.spec["digest"] != added.spec["digest"]
    await resources.apply(ORG, "environments", "default", {"image": "node:22", "harnesses": ["agent"]})
    await resources.delete(ORG, "harnesses", "agent")
    assert (await resources.get(ORG, "environments", "default")).spec["harnesses"] == []
    with pytest.raises(NotFound):
        await resources.delete(ORG, "harnesses", "agent")


async def test_a_line_that_cannot_run_is_refused_before_anything_is_queued(platform: Platform):
    await ready(platform)
    resources = platform.resources
    await resources.add_package(ORG, "agent", package())
    await resources.apply(ORG, "providers", "chat", {"apis": {"openai-chat": "https://api.example.com/v1"}})
    for line, why in (("nowhere/echo/none/none", "no environment"), ("default/nothing/none/none", "no harness"),
                      ("default/agent/chat/m", "does not carry"), ("default/echo/chat/none", "name a provider and a model"),
                      ("default/echo", "four parts")):
        with pytest.raises(Invalid, match=why) as refusal:
            await accept(platform, WHO, {"model": line, "input": "x"})
        assert refusal.value.param == "model"
    await resources.apply(ORG, "environments", "default", {"image": "node:22", "harnesses": ["echo", "agent"]})
    with pytest.raises(Invalid, match="no provider"):
        await accept(platform, WHO, {"model": "default/agent/nobody/m", "input": "x"})
    with pytest.raises(Invalid, match="names no response"):
        await accept(platform, WHO, {"model": LINE, "input": "x", "previous_response_id": "resp_none"})
    assert await platform.db.all(None, "SELECT id FROM turns") == [] and platform.driver.plans == []
