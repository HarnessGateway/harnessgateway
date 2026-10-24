"""Layer 4: the dataset. Only organizations that share now, only sessions created after they began
sharing, never an enterprise's; a row carries the request, the response, the steers and the provider
captures, with identities pseudonymized the same way every time and differently per deployment."""
import json

import pytest

from fakes import archive
from harnessgateway.backends.blobs.dir import DirBlobs
from harnessgateway.backends.records.sqlite import SqliteRecords
from harnessgateway.contract import wire
from harnessgateway.kernel.vault import Vault
from harnessgateway.services.dataset import CAPTURE_MAX, Dataset, cut
from domainfix import domain
from test_sessions import DONE, MSG


@pytest.fixture
def d(tmp_path, vault):
    d = domain(SqliteRecords(tmp_path / "r.db"), DirBlobs(tmp_path / "b", "http://api.test", b"s"), vault, tmp_path / "git")
    for org, plan, sharing in (("acme", "free", True), ("ent", "enterprise", True), ("quiet", "team", False)):
        d.orgs.create(org, plan=plan, trace_sharing=sharing)
        d.members.create(org, "dev", "member")
        d.providers.put(org, "prov", "https://api.example/v1", "openai-chat", "sk-real-secret")
        d.harnesses.create_from_archive(org, "cc", archive(), "dev")
    return d


def run(d, org, text="hello", previous=None, steer=None):
    who = d.members.get(org, "dev")
    body = {"model": "cc", "input": text, **({"previous_response_id": previous} if previous else {})}
    r = d.sessions.create(who, wire.Request.parse(body))
    if not hasattr(d, "node"):
        d.node, _ = d.nodes.register("w1", 4, "t")
    w = d.node
    assert d.scheduler.claim(w)["id"] == r.id
    d.scheduler.started(w, r.id)
    if steer:
        d.sessions.interrupt(who, r.session, steer)
    key = f"tenants/{org}/sessions/{r.session}/turns/{r.id}/provider/0001.json"
    d.blobs.put(key, json.dumps({"at": 1.0, "method": "POST", "path": "/v1/chat/completions", "status": 200,
                                 "request": '{"messages": [{"role": "user", "content": "%s"}]}' % text, "response": "x" * (CAPTURE_MAX + 5)}).encode())
    d.records.put("usage", org, r.id, {"calls": 1, "bytes_in": 10, "bytes_out": 20})
    return d.scheduler.result(w, r.id, {"outcome": "exited", "exit_code": 0, "stdout": f"{MSG}\n{DONE}\n", "stderr": "", "seconds": 1.0, "snapshot": True})


def test_only_sharing_orgs_sessions_after_the_date_are_exported(d):
    before = run(d, "acme", "early")                  # created before sharing is turned off and on again
    d.orgs.update("acme", trace_sharing=False)
    d.orgs.update("acme", trace_sharing=True)
    first = run(d, "acme", "first", steer="faster")
    second = run(d, "acme", "second", previous=first.id)
    run(d, "ent", "secret")
    run(d, "quiet", "private")
    summary = d.dataset.summary()
    assert summary == {"orgs": 1, "sessions": 1, "turns": 2, "since": 0, "until": summary["until"]}
    rows = list(d.dataset.rows())
    assert len(rows) == 1 and rows[0]["session"] == first.session and before.session not in {r["session"] for r in rows}
    row = rows[0]
    assert row["org"].startswith("org_") and row["member"].startswith("member_") and row["team"] == "" and row["harness"] == "cc" and row["plan"] == "free"
    assert "acme" not in json.dumps(row) and "dev" not in (row["org"], row["member"])
    t1, t2 = row["turns"]
    assert t1["response"] == first.id and t1["turn"] == 1 and t1["status"] == "completed" and t1["provider_api"] == "openai-chat"
    assert t1["request"]["input"] == "first" and t1["response_object"]["output"][0]["content"][0]["text"] == "the answer"
    assert [e["kind"] for e in t1["events"]] == ["interrupt", "response"] and t1["events"][0]["body"] == {"input": "faster"}
    assert t1["usage"]["calls"] == 1 and t1["usage"]["input_tokens"] == 2 and "member" not in t1["usage"] and "status" not in t1["usage"]
    assert t1["captures"][0]["name"] == "0001.json" and t1["captures"][0]["request"]["text"].endswith('"first"}]}')
    assert t1["captures"][0]["response"] == {"text": "x" * CAPTURE_MAX, "cut": 5} and cut("short") == {"text": "short"}
    assert t2["response"] == second.id and t2["request"]["previous_response_id"] == first.id and t2["turn"] == 2
    # a window
    assert list(d.dataset.rows(since=first.created + 1)) == [] and d.dataset.summary(until=row["created"])["sessions"] == 0
    # turning sharing off removes the organization at once; the enterprise never appears
    d.orgs.update("acme", trace_sharing=False)
    assert list(d.dataset.rows()) == []
    d.orgs.update("ent", trace_sharing=True)
    assert list(d.dataset.rows()) == []


def test_pseudonyms_are_stable_here_and_different_elsewhere(d, tmp_path):
    a = d.dataset.pseudonym("org", "acme")
    assert a == d.dataset.pseudonym("org", "acme") and a != d.dataset.pseudonym("org", "acmf") and a != d.dataset.pseudonym("member", "acme")
    other = Dataset(d.records, d.blobs, d.orgs, Vault(bytes.fromhex(Vault.generate())))
    assert other.pseudonym("org", "acme") != a and d.dataset.pseudonym("team", "") == ""
