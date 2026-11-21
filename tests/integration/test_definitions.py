"""Providers, environments and harnesses, as they are written and kept."""

import json

import pytest

from harnessgateway.api.responses import accept
from harnessgateway.errors import Invalid, NotFound, Refused
from harnessgateway.platform import Platform
from tests.helpers import MANIFEST, package
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


async def test_a_harness_is_its_package_and_keeps_what_a_new_one_still_declares(platform: Platform):
    resources = platform.resources
    declared = {**MANIFEST, "name": "agent", "settings": {"MODE": {"default": "fast"}, "TEAM": {"required": True}},
                "secrets": {"TOKEN": {"hosts": ["tool.example.com"], "required": True}}}
    added = await resources.add_package(ORG, "agent", package(declared))
    assert added.shown()["missing"] == ["settings.TEAM", "secrets.TOKEN"]
    changed = await resources.apply(ORG, "harnesses", "agent", {"settings": {"TEAM": "payments"}, "secrets": {"TOKEN": "t"}})
    assert (changed.spec["settings"], sorted(changed.sealed), changed.shown()["missing"]) == ({"TEAM": "payments"}, ["TOKEN"], [])
    with pytest.raises(Invalid, match="OTHER is not declared"):
        await resources.apply(ORG, "harnesses", "agent", {"settings": {"OTHER": "x"}})
    again = await resources.add_package(ORG, "agent", package({**declared, "version": "2", "secrets": {}}))
    assert (again.spec["settings"], again.sealed) == ({"TEAM": "payments"}, {})
    with pytest.raises(Invalid, match="names 'agent', not 'renamed'"):
        await resources.add_package(ORG, "renamed", package(declared))
    await resources.apply(ORG, "environments", "default", {"image": "node:22", "harnesses": ["agent"]})
    await resources.delete(ORG, "harnesses", "agent")
    assert (await resources.get(ORG, "environments", "default")).spec["harnesses"] == []
    with pytest.raises(NotFound):
        await resources.delete(ORG, "harnesses", "agent")


async def test_a_line_that_cannot_run_is_refused_before_anything_is_queued(platform: Platform):
    await ready(platform)
    resources = platform.resources
    await resources.add_package(ORG, "agent", package({**MANIFEST, "name": "agent", "apis": ["anthropic"]}))
    await resources.apply(ORG, "providers", "chat", {"apis": {"openai-chat": "https://api.example.com/v1"}})
    for line, why in (("nowhere/echo/none/none", "no environment"), ("default/nothing/none/none", "no harness"),
                      ("default/agent/chat/m", "does not carry"), ("default/echo/chat/m", "calls no model"),
                      ("default/echo", "four parts")):
        with pytest.raises(Invalid, match=why) as refusal:
            await accept(platform, WHO, {"model": line, "input": "x"})
        assert refusal.value.param == "model"
    await resources.apply(ORG, "environments", "default", {"image": "node:22", "harnesses": ["echo", "agent"]})
    for line, why in (("default/agent/chat/m", "speaks openai-chat, but harness"), ("default/agent/none/none", "calls a model"),
                      ("default/agent/nobody/m", "no provider")):
        with pytest.raises(Invalid, match=why):
            await accept(platform, WHO, {"model": line, "input": "x"})
    with pytest.raises(Invalid, match="names no response"):
        await accept(platform, WHO, {"model": LINE, "input": "x", "previous_response_id": "resp_none"})
    assert await platform.db.all(None, "SELECT id FROM turns") == [] and platform.driver.plans == []
