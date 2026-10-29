import json

import pytest

from harnessgateway.domain.manifest import Manifest, compose
from harnessgateway.errors import Invalid

BASE = {"contract": 1, "name": "cli", "version": "1.0-1", "provider": {"apis": ["anthropic"]},
        "defaults": {"model": "m1"}, "install": {"egress": ["registry.npmjs.org"]}, "run": {"stop_grace_s": 5},
        "persist": [".cli/sessions"], "settings": {"LEVEL": {"default": "info"}},
        "secrets": {"GH_TOKEN": {"hosts": ["api.github.com"]}},
        "mcp": {"github": {"url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": "Bearer ${GH_TOKEN}"}}}}


def parse(doc):
    return Manifest.parse(json.dumps(doc))


def test_a_base_manifest_parses_and_implies_its_hosts():
    m = parse(BASE)
    m.check_references()
    assert m.run_entries() == ["api.github.com", "api.githubcopilot.com"]


@pytest.mark.parametrize("patch, message", [
    ({"contract": 2}, "contract"),
    ({"name": "Bad Name"}, "name"),
    ({"provider": {"apis": ["bedrock"]}}, "provider.apis"),
    ({"run": {"egress": ["*.com"]}}, "egress"),
    ({"persist": ["../etc"]}, "persist"),
    ({"settings": {"HG_X": {}}}, "settings"),
    ({"settings": {"PATH": {}}}, "settings"),
    ({"secrets": {"T": {"hosts": ["!x.com"]}}}, "hosts"),
    ({"mcp": {"x": {"url": "http://x.com/mcp"}}}, "url"),
    ({"surprise": 1}, "surprise"),
])
def test_bad_manifests_are_refused_naming_the_field(patch, message):
    with pytest.raises(Invalid, match=message):
        parse({**BASE, **patch})


def test_an_undeclared_secret_reference_is_refused():
    with pytest.raises(Invalid, match="undeclared"):
        parse({**BASE, "secrets": {}}).check_references()


def test_an_overlay_may_not_set_what_its_base_owns():
    for patch in ({"provider": {"apis": ["openai"]}}, {"install": {}}, {"run": {"timeout_s": 5}}):
        with pytest.raises(Invalid, match="overlay"):
            parse({"contract": 1, "name": "acme", "version": "1", "extends": "cli", **patch})


def test_an_overlay_composes_over_its_base():
    overlay = parse({"contract": 1, "name": "acme", "version": "acme-1", "extends": "cli", "defaults": {"model": "m2"},
                     "run": {"egress": ["git.acme.com", "!api.github.com"]}, "persist": [".cli/memory"],
                     "settings": {"TEAM": {}}, "secrets": {"JIRA": {"hosts": ["acme.atlassian.net"]}},
                     "mcp": {"jira": {"url": "https://acme.atlassian.net/mcp", "headers": {"X-Token": "${JIRA}"}}}})
    c = compose(parse(BASE), overlay)
    assert (c.name, c.extends, c.defaults.model, c.provider.apis) == ("acme", "cli", "m2", ["anthropic"])
    assert c.persist == [".cli/memory", ".cli/sessions"] and set(c.settings) == {"LEVEL", "TEAM"}
    assert set(c.mcp) == {"github", "jira"} and c.run.stop_grace_s == 5
    assert "!api.github.com" in c.run_entries() and "acme.atlassian.net" in c.run_entries()


def test_an_overlay_redefining_a_name_is_refused():
    overlay = parse({"contract": 1, "name": "acme", "version": "1", "extends": "cli", "settings": {"LEVEL": {}}})
    with pytest.raises(Invalid, match="already defined"):
        compose(parse(BASE), overlay)
