import json

import pytest

from harnessgateway.domain.egress import Policy
from harnessgateway.domain.manifest import Manifest
from harnessgateway.domain.mcp import resolve
from harnessgateway.errors import Invalid

CATALOG = Manifest.parse(json.dumps({
    "contract": 1, "name": "cli", "version": "1", "secrets": {"T": {"hosts": ["a.example.com"]}},
    "mcp": {"docs": {"url": "https://docs.example.com/mcp", "allowed_tools": ["search", "read"]},
            "issues": {"url": "https://a.example.com/mcp", "headers": {"Authorization": "Bearer ${T}"}}}})).mcp
POLICY = Policy.of(["*.example.com"])


def labels(r):
    return [s["label"] for s in r.servers]


def test_without_mcp_tools_the_whole_catalog_is_active():
    r = resolve(CATALOG, {"input": "x"}, POLICY)
    assert labels(r) == ["docs", "issues"] and r.bindings == []
    assert r.servers[1]["headers"] == {"Authorization": "Bearer ${T}"}


def test_selecting_a_catalog_server_narrows_to_it_and_its_tools():
    r = resolve(CATALOG, {"tools": [{"type": "mcp", "server_label": "docs", "allowed_tools": ["read", "write"]}]}, POLICY)
    assert labels(r) == ["docs"] and r.servers[0]["allowed_tools"] == ["read"]


def test_an_added_server_has_its_credentials_replaced_by_placeholders():
    body = {"tools": [{"type": "mcp", "server_label": "mine", "server_url": "https://mcp.example.com/x",
                       "authorization": "secret-token", "headers": {"X-Key": "k2"}}]}
    r = resolve(CATALOG, body, POLICY)
    assert labels(r) == ["docs", "issues", "mine"]
    text = json.dumps(r.body) + json.dumps(r.servers)
    assert "secret-token" not in text and "k2" not in text
    assert {b.value for b in r.bindings} == {"secret-token", "k2"}
    assert all(b.hosts == ("mcp.example.com",) and b.placeholder.startswith("hgs_") for b in r.bindings)
    assert r.servers[2]["headers"]["Authorization"] == f"Bearer {r.body['tools'][0]['authorization']}"
    assert body["tools"][0]["authorization"] == "secret-token"          # the caller's dict is not changed


@pytest.mark.parametrize("tool, code", [
    ({"server_label": "nope"}, "mcp_server_not_found"),
    ({"server_label": "docs", "server_url": "https://docs.example.com/mcp"}, "mcp_label_conflict"),
    ({"server_label": "x", "server_url": "https://elsewhere.org/mcp"}, "mcp_server_not_allowed"),
])
def test_unknown_conflicting_or_unreachable_servers_are_refused(tool, code):
    with pytest.raises(Invalid) as e:
        resolve(CATALOG, {"tools": [{"type": "mcp", **tool}]}, POLICY)
    assert e.value.code == code
