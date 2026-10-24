"""Layer 4: intake and placement. An Idempotency-Key makes a retried request one turn, for its
sender, for a day; the organization's rate ceiling refuses the request past it with a 429-kind
refusal; a queued turn waits a moment for the node that holds its session's trees, then goes to any
live node."""
import time

import pytest

from fakes import archive
from harnessgateway.backends.blobs.dir import DirBlobs
from harnessgateway.backends.records.sqlite import SqliteRecords
from harnessgateway.contract import wire
from harnessgateway.errors import Invalid, TooMany
from harnessgateway.services import intake as intake_module
from harnessgateway.services import scheduler as scheduler_module
from domainfix import domain
from test_sessions import finish


@pytest.fixture
def d(tmp_path, vault):
    d = domain(SqliteRecords(tmp_path / "r.db"), DirBlobs(tmp_path / "b", "http://api.test", b"s"), vault, tmp_path / "git")
    d.orgs.create("acme", {"max_runs": 4, "requests_per_minute": 3})
    d.members.create("acme", "chase", "owner")
    d.members.create("acme", "dev", "member")
    d.providers.put("acme", "prov", "https://api.example/v1", "openai-chat", "sk-real-secret")
    d.harnesses.create_from_archive("acme", "cc", archive(), "chase")
    return d


def req(text="hello", **body):
    return wire.Request.parse({"model": "cc", "input": text, **body})


def later(monkeypatch, seconds: float) -> None:
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + seconds)


def test_an_idempotency_key_makes_one_turn_per_sender(d, monkeypatch):
    d.orgs.update("acme", limits={"requests_per_minute": 1000})
    dev, chase = d.members.get("acme", "dev"), d.members.get("acme", "chase")
    first = d.intake.create(dev, req(), "k-1")
    again = d.intake.create(dev, req("something else"), "k-1")
    assert again.id == first.id and again.text == "hello"                 # the first request's turn, whatever the retry says
    assert d.intake.create(chase, req(), "k-1").id != first.id            # another member's key is another key
    assert d.intake.create(dev, req(), "k-2").id != first.id
    assert d.intake.create(dev, req()).id != d.intake.create(dev, req()).id   # without a key every request is a turn
    for bad in ("x" * 201, "has space", "a/b"):
        with pytest.raises(Invalid, match="Idempotency-Key"):
            d.intake.create(dev, req(), bad)
    # the key expires after a day
    later(monkeypatch, intake_module.IDEMPOTENCY_TTL_S + 1)
    assert d.intake.create(dev, req(), "k-1").id != first.id


def test_the_rate_ceiling_is_the_organizations_per_minute(d, monkeypatch):
    dev = d.members.get("acme", "dev")
    for _ in range(3):
        d.intake.create(dev, req())
    with pytest.raises(TooMany, match="3 requests per minute"):
        d.intake.create(dev, req())
    with pytest.raises(TooMany):
        d.intake.create(dev, req(), "a-new-key")                            # a new key is a new turn: refused as well
    # a retry with a key already seen is not a new turn, so it passes the ceiling
    r = d.records.get("response", "acme", d.records.list("response", "acme")[0]["id"])
    d.records.put("idem", "acme", "dev:seen", {"response": r["id"], "expires": time.time() + 100})
    assert d.intake.create(dev, req(), "seen").id == r["id"]
    # the next minute starts fresh; the ceiling is the deployer's to raise
    later(monkeypatch, 61)
    assert d.intake.create(dev, req())
    d.orgs.update("acme", limits={"requests_per_minute": 1000})
    for _ in range(10):
        d.intake.create(dev, req())


def test_a_queued_turn_waits_briefly_for_the_node_that_holds_its_session(d, monkeypatch):
    dev = d.members.get("acme", "dev")
    w1, _ = d.nodes.register("w1", 4, "t")
    w2, _ = d.nodes.register("w2", 4, "t")
    first = d.sessions.create(dev, req())
    assert d.scheduler.claim(w1)["id"] == first.id
    finish(d, w1, first.id)
    assert d.sessions.session_by_id(first.session).node == w1.id
    # the continuation prefers w1 while w1 lives and the wait is short
    second = d.sessions.create(dev, req("more", previous_response_id=first.id))
    assert d.scheduler.claim(w2) is None
    job = d.scheduler.claim(w1)
    assert job["id"] == second.id and job["snapshot"] == first.id
    finish(d, w1, second.id)
    # past the wait, any node takes it
    third = d.sessions.create(dev, req("more", previous_response_id=second.id))
    later(monkeypatch, scheduler_module.AFFINITY_S + 1)
    assert d.scheduler.claim(w2)["id"] == third.id
    monkeypatch.undo()
    finish(d, w2, third.id)
    assert d.sessions.session_by_id(first.session).node == w2.id
    # a node that stopped heartbeating holds nothing: another takes the turn at once
    fourth = d.sessions.create(dev, req("more", previous_response_id=third.id))
    d.records.put("worker", "", w2.id, {**d.records.get("worker", "", w2.id), "last_seen": time.time() - 999})
    assert d.scheduler.claim(w1)["id"] == fourth.id
    # a new session has no node to wait for
    fresh = d.sessions.create(dev, req("new"))
    assert d.scheduler.claim(w2)["id"] == fresh.id
