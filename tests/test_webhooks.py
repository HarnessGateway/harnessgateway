"""Layer 4: webhooks. An owner registers a URL and gets the secret once; every final response is
posted there under an event envelope, signed over the timestamp and the body, retried with a pause
when the receiver fails, and the webhook's record shows how the last delivery went."""
import json
import time

import httpx
import pytest

from fakes import archive
from harnessgateway.backends.blobs.dir import DirBlobs
from harnessgateway.backends.records.sqlite import SqliteRecords
from harnessgateway.contract import wire
from harnessgateway.errors import Invalid, NotFound
from harnessgateway.services.webhooks import DELIVERY, EVENT, SIGNATURE, TIMESTAMP, sign, verify
from domainfix import domain
from test_sessions import finish


class Receiver:
    def __init__(self, fail_first: int = 0):
        self.calls: list[httpx.Request] = []
        self.fail_first = fail_first

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if len(self.calls) <= self.fail_first:
            return httpx.Response(503, text="later")
        return httpx.Response(200, text="ok")


@pytest.fixture
def setup(tmp_path, vault):
    def make(receiver):
        d = domain(SqliteRecords(tmp_path / "r.db"), DirBlobs(tmp_path / "b", "http://api.test", b"s"), vault, tmp_path / "git",
                   webhook_transport=httpx.MockTransport(receiver.handler))
        d.orgs.create("acme")
        d.members.create("acme", "chase", "owner")
        d.providers.put("acme", "prov", "https://api.example/v1", "openai-chat", "sk-real-secret")
        d.harnesses.create_from_archive("acme", "cc", archive(), "chase")
        return d
    return make


def run_turn(d):
    who = d.members.get("acme", "chase")
    r = d.sessions.create(who, wire.Request.parse({"model": "cc", "input": "go"}))
    w, _ = d.nodes.register("w1", 4, "t")
    d.scheduler.claim(w)
    d.scheduler.started(w, r.id)
    return finish(d, w, r.id)


def test_registration_and_the_secret_shown_once(setup):
    d = setup(Receiver())
    w, secret = d.webhooks.create("acme", "https://hooks.example/hg", "ci")
    assert secret.startswith("whsec_") and w.public() == {"id": w.id, "org": "acme", "url": "https://hooks.example/hg", "label": "ci",
                                                           "created": w.created, "last_status": 0, "last_at": 0, "failures": 0}
    assert "secret" not in w.public() and d.webhooks.secret_of(w) == secret and d.webhooks.list("acme") == [w]
    for bad in ("ftp://x", "hooks.example/hg", "", "https://" + "x" * 2000):
        with pytest.raises(Invalid):
            d.webhooks.create("acme", bad)
    with pytest.raises(NotFound):
        d.webhooks.get("acme", "nope")
    d.webhooks.delete("acme", w.id)
    assert d.webhooks.list("acme") == []
    # a webhook is the organization's: deleting the organization removes it
    d.webhooks.create("acme", "https://hooks.example/hg")
    d.orgs.delete("acme")
    assert d.records.list("webhook", "acme") == []


def test_a_final_response_is_posted_and_signed(setup):
    receiver = Receiver()
    d = setup(receiver)
    w, secret = d.webhooks.create("acme", "https://hooks.example/hg")
    r = run_turn(d)
    d.notifier.drain()
    assert len(receiver.calls) == 1
    call = receiver.calls[0]
    body = json.loads(call.content)
    assert body["type"] == "response.completed" and body["data"]["id"] == r.id and body["data"]["output"][0]["content"][0]["text"] == "the answer"
    assert body["id"].startswith("evt_") and call.headers[EVENT] == "response.completed" and call.headers[DELIVERY] == body["id"]
    assert verify(secret, call.headers[TIMESTAMP], call.content, call.headers[SIGNATURE])
    assert not verify("whsec_other", call.headers[TIMESTAMP], call.content, call.headers[SIGNATURE])
    assert not verify(secret, str(int(time.time()) - 1000), call.content, sign(secret, str(int(time.time()) - 1000), call.content))
    assert not verify(secret, "not-a-time", call.content, call.headers[SIGNATURE])
    got = d.webhooks.get("acme", w.id)
    assert got.last_status == 200 and got.failures == 0 and got.last_at > 0
    # a ping on demand
    d.notifier.ping(got).result()
    assert json.loads(receiver.calls[-1].content)["type"] == "ping"
    # no webhooks: nothing is sent, nothing fails
    d.webhooks.delete("acme", w.id)
    assert d.notifier.turn_ended(run_turn(d)) == []


def test_a_failing_receiver_is_retried_then_given_up_on(setup):
    receiver = Receiver(fail_first=2)
    d = setup(receiver)
    w, _ = d.webhooks.create("acme", "https://hooks.example/hg")
    run_turn(d)
    d.notifier.drain()
    assert len(receiver.calls) == 3 and d.webhooks.get("acme", w.id).last_status == 200           # third time lucky
    receiver.fail_first = 100
    before = len(receiver.calls)
    run_turn(d)
    d.notifier.drain()
    assert len(receiver.calls) - before == 3                                                     # every pause used, then given up
    got = d.webhooks.get("acme", w.id)
    assert got.last_status == 503 and got.failures == 1
    run_turn(d)
    d.notifier.drain()
    assert d.webhooks.get("acme", w.id).failures == 2


def test_a_receiver_that_cannot_be_reached_counts_as_no_answer(setup):
    def down(request):
        raise httpx.ConnectError("refused")
    receiver = Receiver()
    receiver.handler = down
    d = setup(receiver)
    w, _ = d.webhooks.create("acme", "https://hooks.example/hg")
    run_turn(d)
    d.notifier.drain()
    assert d.webhooks.get("acme", w.id).last_status == 0 and d.webhooks.get("acme", w.id).failures == 1
