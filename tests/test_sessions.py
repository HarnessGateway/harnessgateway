"""Layer 5, the control plane's side: a turn is queued, claimed by a worker with its job, streamed,
ended; a session continues from its snapshot and inherits its provider; reach is organization, team,
member; cancel, steer and a client's disconnect as commands; a lost worker fails its turn and frees the
session; store:false keeps no snapshot."""
import io
import json
import tarfile
import time

import pytest

from fakes import archive
from harnessgateway.contract import wire
from harnessgateway.backends.blobs.dir import DirBlobs
from harnessgateway.errors import Conflict, Invalid, NotFound
from harnessgateway.backends.records.sqlite import SqliteRecords
from domainfix import domain

MSG = json.dumps({"type": "response.output_item.done", "output_index": 0, "item": {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                                                                                   "content": [{"type": "output_text", "text": "the answer", "annotations": []}]}})
DONE = json.dumps({"type": "response.completed", "response": {"usage": {"input_tokens": 2, "output_tokens": 3}}})


@pytest.fixture
def d(tmp_path, vault):
    """The domain with one organization, two members, a provider and a harness."""
    d = domain(SqliteRecords(tmp_path / "r.db"), DirBlobs(tmp_path / "b", "http://api.test", b"s"), vault, tmp_path / "git")
    d.orgs.create("acme", {"max_runs": 1})
    d.members.create("acme", "chase", "owner")
    d.members.create("acme", "dev", "member")
    d.providers.put("acme", "prov", "https://api.example/v1", "openai-chat", "sk-real-secret")
    d.harnesses.create_from_archive("acme", "cc", archive(), "chase")
    return d


def ask(d, text="hello", member="dev", model="cc", **body):
    who = d.members.get("acme", member)
    return d.sessions.create(who, wire.Request.parse({"model": model, "input": text, **body}))


def worker(d, name="w1"):
    w, _ = d.nodes.register(name, 4, "t")
    return w


def finish(d, w, rid, **body):
    return d.scheduler.result(w, rid, {"outcome": "exited", "exit_code": 0, "stdout": f"log line\n{MSG}\n{DONE}\n", "stderr": "",
                                    "seconds": 1.5, "snapshot": True, **body})


def test_queue_claim_run_end(d):
    sessions, scheduler, store = d.sessions, d.scheduler, d
    r = ask(d, input=[{"role": "user", "content": [{"type": "input_text", "text": "hello"}, {"type": "input_file", "filename": "notes/a.txt", "file_data": "A"}]}],
            text=None, instructions="be brief")
    assert r.status == "queued" and r.turn == 1 and r.files.endswith("files.tgz") and r.team == "" and not r.background
    w = worker(d)
    job = scheduler.claim(w)
    assert job and job["id"] == r.id and job["token"].startswith("sk-hg-") and job["provider"]["url"] == "http://hg-provider:8080/v1"
    assert job["urls"]["folder"].startswith("http://api.test/internal/blobs/") and job["urls"]["snapshot"] == "" and job["urls"]["files"] and job["urls"]["request"]
    assert job["config"]["run"]["egress"] == ["git.example"] and job["limits"]["max_runs"] == 1 and job["timeout_s"] == 60 and job["new_session"] and job["team"] == ""
    assert store.providers.resolve_token(job["token"])["response"] == r.id
    assert scheduler.claim(w) is None                      # max_runs 1: nothing else while this runs
    scheduler.started(w, r.id)
    scheduler.chunks_in(w, r.id, [{"seq": 1, "stream": "stdout", "text": "log li"}, {"seq": 2, "stream": "stdout", "text": "ne\n" + MSG + "\n"}])
    assert scheduler.commands(w, [r.id]) == {r.id: {"cancel": False, "interrupts": []}}
    assert [i["type"] for i in sessions.live_items(sessions.turn(store.members.get("acme", "dev"), r.id))] == ["message"]
    done = finish(d, w, r.id)
    dev = store.members.get("acme", "dev")
    assert done.status == "completed" and done.output[0]["content"][0]["text"] == "the answer" and done.usage["total_tokens"] == 5
    assert done.log == "log line" and done.running == "0"
    s = sessions.session(dev, r.session)
    assert s.responses == [r.id] and not s.running and s.snapshot_key.endswith("snapshot.tgz") and s.snapshot == r.id and s.title == "hello"
    assert [t["kind"] for t in sessions.trace(dev, s.id)] == ["request", "response"]
    assert json.loads(sessions.blobs.get(f"tenants/acme/sessions/{s.id}/turns/{r.id}/request.json"))["instructions"] == "be brief"
    usage = d.records.get("usage", "acme", r.id)
    assert usage["seconds"] == 1.5 and usage["input_tokens"] == 2 and usage["member"] == "dev"
    with pytest.raises(Conflict):
        finish(d, worker(d, "other"), r.id)   # another worker cannot end it


def test_continue_inherits_overrides_and_reach(d):
    sessions, scheduler, store = d.sessions, d.scheduler, d
    store.providers.put("acme", "other", "https://o.example", "anthropic-messages", "k")
    w = worker(d)
    r1 = ask(d, model="cc/other/m2")
    assert (r1.provider, r1.model) == ("other", "m2")
    scheduler.claim(w)
    finish(d, w, r1.id)
    r2 = ask(d, "again", previous_response_id=r1.id)
    assert r2.session == r1.session and r2.turn == 2 and (r2.provider, r2.model) == ("other", "m1")   # the provider is inherited, the model is the package's
    job = scheduler.claim(w)
    assert job["urls"]["snapshot"] and job["snapshot"] == r1.id and not job["new_session"] and job["token"].startswith("sk-ant-api03-hg-")
    with pytest.raises(Conflict):
        ask(d, "busy", previous_response_id=r1.id)
    finish(d, w, r2.id)
    chase, dev = store.members.get("acme", "chase"), store.members.get("acme", "dev")
    assert [x.id for x in sessions.turns_of(dev, r1.session)] == [r1.id, r2.id]
    store.orgs.create("other")
    store.members.create("other", "eve", "owner")
    store.members.create("acme", "peer", "member")
    for who in (store.members.get("other", "eve"), store.members.get("acme", "peer")):
        with pytest.raises(NotFound):
            sessions.session(who, r1.session)
        with pytest.raises(NotFound):
            sessions.turn(who, r1.id)
        assert sessions.list(who) == []
    assert sessions.turn(chase, r1.id).id == r1.id
    with pytest.raises(NotFound):
        ask(d, previous_response_id="resp_" + "0" * 32)
    store.harnesses.create_from_archive("acme", "other", archive(), "chase")
    with pytest.raises(Invalid, match="runs cc, not other"):
        ask(d, "x", model="other", previous_response_id=r1.id)
    # a team's harness: its sessions are the team's
    store.teams.put("acme", "platform", ["dev", "peer"])
    store.members.create("acme", "outsider", "member")
    store.harnesses.create_from_archive("acme", "tc", archive(), "chase", team="platform")
    t = ask(d, "team work", model="tc")
    assert t.team == "platform"
    peer, outsider = store.members.get("acme", "peer"), store.members.get("acme", "outsider")
    assert sessions.turn(peer, t.id).id == t.id and [s.id for s in sessions.list(peer)] == [t.session]
    with pytest.raises(NotFound):
        sessions.turn(outsider, t.id)
    with pytest.raises(NotFound):
        ask(d, "no", member="outsider", model="tc")             # not even the harness
    assert scheduler.claim(w)["team"] == "platform"


def test_cancel_interrupt_disconnect_and_lost_worker(d):
    sessions, scheduler, store = d.sessions, d.scheduler, d
    dev = store.members.get("acme", "dev")
    r = ask(d)
    assert sessions.cancel(dev, r.id).status == "cancelled"         # queued: ends at once
    assert not sessions.session(dev, r.session).running
    r = ask(d, "wait")
    with pytest.raises(Conflict, match="not running"):
        sessions.interrupt(dev, r.session, "turn")                     # queued: nothing to steer yet
    w = worker(d)
    scheduler.claim(w)
    scheduler.started(w, r.id)
    sessions.interrupt(dev, r.session, "turn left")
    with pytest.raises(Invalid):
        sessions.interrupt(dev, r.session, "   ")
    sessions.cancel(dev, r.id)
    assert scheduler.commands(w, [r.id])[r.id] == {"cancel": True, "interrupts": [{"seq": 1, "text": "turn left"}]}
    finish(d, w, r.id, outcome="stopped", exit_code=0)
    got = sessions.turn(dev, r.id)
    assert got.status == "cancelled" and got.incomplete == {"reason": "cancelled"}
    assert [t["kind"] for t in sessions.trace(dev, r.session)] == ["request", "interrupt", "cancel", "response"]
    # the client went away: a foreground turn is stopped and says so; a background one is not
    r = ask(d, "gone")
    scheduler.claim(w)
    scheduler.started(w, r.id)
    assert sessions.disconnected(dev, r.id).cancel and scheduler.commands(w, [r.id])[r.id]["cancel"]
    finish(d, w, r.id, outcome="stopped", exit_code=0)
    got = sessions.turn(dev, r.id)
    assert got.status == "cancelled" and got.incomplete == {"reason": "client_disconnected"}
    assert sessions.response_object(got)["metadata"]["incomplete_reason"] == "client_disconnected"
    assert [t["kind"] for t in sessions.trace(dev, r.session)][-3:] == ["request", "disconnect", "response"]
    b = ask(d, "bg", background=True)
    scheduler.claim(w)
    scheduler.started(w, b.id)
    assert not sessions.disconnected(dev, b.id).cancel and not scheduler.commands(w, [b.id])[b.id]["cancel"]
    finish(d, w, b.id)
    assert sessions.turn(dev, b.id).status == "completed"
    # a lost worker
    r = ask(d, "lost")
    scheduler.claim(w)
    d.records.put("worker", "", w.id, {**d.records.get("worker", "", w.id), "last_seen": time.time() - 999})
    assert scheduler.sweep_lost() == [r.id]
    got = sessions.turn(dev, r.id)
    assert got.status == "failed" and "lost" in got.error["message"] and not sessions.session(dev, r.session).running


def test_stateless_archive_delete(d):
    sessions, scheduler, store = d.sessions, d.scheduler, d
    dev = store.members.get("acme", "dev")
    w = worker(d)
    r = ask(d, store=False)
    job = scheduler.claim(w)
    assert job["urls"]["snapshot_put"] == "" and not job["store"]
    finish(d, w, r.id, snapshot=False)
    s = sessions.session(dev, r.session)
    assert not s.stored and not s.snapshot_key
    with pytest.raises(Invalid, match="not stored"):
        ask(d, previous_response_id=r.id)
    with pytest.raises(NotFound):
        sessions.archive(dev, s.id)
    r = ask(d)
    scheduler.claim(w)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("./workspace/out.txt", b"hello"), ("./state/session", b"sid")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    sessions.blobs.put(f"tenants/acme/sessions/{r.session}/snapshot.tgz", buf.getvalue())
    finish(d, w, r.id)
    with tarfile.open(fileobj=io.BytesIO(sessions.archive(dev, r.session))) as tar:
        assert tar.getnames() == ["./out.txt"]                             # the workspace, without the CLI's state
    assert [f["path"] for f in sessions.files(dev, r.session)] == ["out.txt"] and sessions.file(dev, r.session, "out.txt") == b"hello"
    sessions.delete(dev, r.session)
    with pytest.raises(NotFound):
        sessions.turn(dev, r.id)
    assert sessions.blobs.list(f"tenants/acme/sessions/{r.session}") == []
