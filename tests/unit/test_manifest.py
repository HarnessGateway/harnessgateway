import json

import pytest

from harnessgateway.domain.manifest import Manifest
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

