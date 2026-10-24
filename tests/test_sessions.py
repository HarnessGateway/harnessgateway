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
from harnessgateway import wire
from harnessgateway.blobs import DirBlobs
from harnessgateway.errors import Conflict, Invalid, NotFound
from harnessgateway.records import SqliteRecords
from harnessgateway.sessions import Sessions
from harnessgateway.store import Store

MSG = json.dumps({"type": "response.output_item.done", "output_index": 0, "item": {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                                                                                   "content": [{"type": "output_text", "text": "the answer", "annotations": []}]}})
DONE = json.dumps({"type": "response.completed", "response": {"usage": {"input_tokens": 2, "output_tokens": 3}}})


@pytest.fixture
def sessions(tmp_path, vault):
    store = Store(SqliteRecords(tmp_path / "r.db"), DirBlobs(tmp_path / "b", "http://api.test", b"s"), vault, tmp_path / "git")
    store.create_org("acme", {"max_runs": 1})
    store.create_member("acme", "chase", "owner")
    store.create_member("acme", "dev", "member")
    store.put_provider("acme", "prov", "https://api.example/v1", "openai-chat", "sk-real-secret")
    store.create_harness_from_archive("acme", "cc", archive(), "chase")
    return Sessions(store, "http://api.test", "http://gateway.test:8081")


def ask(sessions, text="hello", member="dev", model="cc", **body):
    who = sessions.store.member("acme", member)
    return sessions.create(who, wire.Request.parse({"model": model, "input": text, **body}))


def worker(sessions, name="w1"):
    w, _ = sessions.store.register_worker(name, 4, "t")
    return w


def finish(sessions, w, rid, **body):
    return sessions.result(w, rid, {"outcome": "exited", "exit_code": 0, "stdout": f"log line\n{MSG}\n{DONE}\n", "stderr": "",
                                    "seconds": 1.5, "snapshot": True, **body})


def test_queue_claim_run_end(sessions):
    store = sessions.store
    r = ask(sessions, input=[{"role": "user", "content": [{"type": "input_text", "text": "hello"}, {"type": "input_file", "filename": "notes/a.txt", "file_data": "A"}]}],
            text=None, instructions="be brief")
    assert r.status == "queued" and r.turn == 1 and r.files.endswith("files.tgz") and r.team == "" and not r.background
    w = worker(sessions)
    job = sessions.claim(w)
    assert job and job["id"] == r.id and job["token"].startswith("sk-hg-") and job["provider"]["url"] == "http://hg-provider:8080/v1"
    assert job["urls"]["folder"].startswith("http://api.test/internal/blobs/") and job["urls"]["snapshot"] == "" and job["urls"]["files"] and job["urls"]["request"]
    assert job["config"]["run"]["egress"] == ["git.example"] and job["limits"]["max_runs"] == 1 and job["timeout_s"] == 60 and job["new_session"] and job["team"] == ""
    assert store.resolve_token(job["token"])["response"] == r.id
    assert sessions.claim(w) is None                      # max_runs 1: nothing else while this runs
    sessions.started(w, r.id)
    sessions.chunks_in(w, r.id, [{"seq": 1, "stream": "stdout", "text": "log li"}, {"seq": 2, "stream": "stdout", "text": "ne\n" + MSG + "\n"}])
    assert sessions.commands(w, [r.id]) == {r.id: {"cancel": False, "interrupts": []}}
    assert [i["type"] for i in sessions.live_items(sessions.response(store.member("acme", "dev"), r.id))] == ["message"]
    done = finish(sessions, w, r.id)
    dev = store.member("acme", "dev")
    assert done.status == "completed" and done.output[0]["content"][0]["text"] == "the answer" and done.usage["total_tokens"] == 5
    assert done.log == "log line" and done.running == "0"
    s = sessions.session(dev, r.session)
    assert s.responses == [r.id] and not s.running and s.snapshot_key.endswith("snapshot.tgz") and s.snapshot == r.id and s.title == "hello"
    assert [t["kind"] for t in sessions.trace(dev, s.id)] == ["request", "response"]
    assert json.loads(sessions.blobs.get(f"tenants/acme/sessions/{s.id}/turns/{r.id}/request.json"))["instructions"] == "be brief"
    usage = store.records.get("usage", "acme", r.id)
    assert usage["seconds"] == 1.5 and usage["input_tokens"] == 2 and usage["member"] == "dev"
    with pytest.raises(Conflict):
        finish(sessions, worker(sessions, "other"), r.id)   # another worker cannot end it


def test_continue_inherits_overrides_and_reach(sessions):
    store = sessions.store
    store.put_provider("acme", "other", "https://o.example", "anthropic-messages", "k")
    w = worker(sessions)
    r1 = ask(sessions, model="cc/other/m2")
    assert (r1.provider, r1.model) == ("other", "m2")
    sessions.claim(w)
    finish(sessions, w, r1.id)
    r2 = ask(sessions, "again", previous_response_id=r1.id)
    assert r2.session == r1.session and r2.turn == 2 and (r2.provider, r2.model) == ("other", "m1")   # the provider is inherited, the model is the package's
    job = sessions.claim(w)
    assert job["urls"]["snapshot"] and job["snapshot"] == r1.id and not job["new_session"] and job["token"].startswith("sk-ant-api03-hg-")
    with pytest.raises(Conflict):
        ask(sessions, "busy", previous_response_id=r1.id)
    finish(sessions, w, r2.id)
    chase, dev = store.member("acme", "chase"), store.member("acme", "dev")
    assert [x.id for x in sessions.records_of(dev, r1.session)] == [r1.id, r2.id]
    store.create_org("other")
    store.create_member("other", "eve", "owner")
    store.create_member("acme", "peer", "member")
    for who in (store.member("other", "eve"), store.member("acme", "peer")):
        with pytest.raises(NotFound):
            sessions.session(who, r1.session)
        with pytest.raises(NotFound):
            sessions.response(who, r1.id)
        assert sessions.sessions(who) == []
    assert sessions.response(chase, r1.id).id == r1.id
    with pytest.raises(NotFound):
        ask(sessions, previous_response_id="resp_" + "0" * 32)
    store.create_harness_from_archive("acme", "other", archive(), "chase")
    with pytest.raises(Invalid, match="runs cc, not other"):
        ask(sessions, "x", model="other", previous_response_id=r1.id)
    # a team's harness: its sessions are the team's
    store.put_team("acme", "platform", ["dev", "peer"])
    store.create_member("acme", "outsider", "member")
    store.create_harness_from_archive("acme", "tc", archive(), "chase", team="platform")
    t = ask(sessions, "team work", model="tc")
    assert t.team == "platform"
    peer, outsider = store.member("acme", "peer"), store.member("acme", "outsider")
    assert sessions.response(peer, t.id).id == t.id and [s.id for s in sessions.sessions(peer)] == [t.session]
    with pytest.raises(NotFound):
        sessions.response(outsider, t.id)
    with pytest.raises(NotFound):
        ask(sessions, "no", member="outsider", model="tc")             # not even the harness
    assert sessions.claim(w)["team"] == "platform"


def test_cancel_interrupt_disconnect_and_lost_worker(sessions):
    store = sessions.store
    dev = store.member("acme", "dev")
    r = ask(sessions)
    assert sessions.cancel(dev, r.id).status == "cancelled"         # queued: ends at once
    assert not sessions.session(dev, r.session).running
    r = ask(sessions, "wait")
    with pytest.raises(Conflict, match="not running"):
        sessions.interrupt(dev, r.session, "turn")                     # queued: nothing to steer yet
    w = worker(sessions)
    sessions.claim(w)
    sessions.started(w, r.id)
    sessions.interrupt(dev, r.session, "turn left")
    with pytest.raises(Invalid):
        sessions.interrupt(dev, r.session, "   ")
    sessions.cancel(dev, r.id)
    assert sessions.commands(w, [r.id])[r.id] == {"cancel": True, "interrupts": [{"seq": 1, "text": "turn left"}]}
    finish(sessions, w, r.id, outcome="stopped", exit_code=0)
    got = sessions.response(dev, r.id)
    assert got.status == "cancelled" and got.incomplete == {"reason": "cancelled"}
    assert [t["kind"] for t in sessions.trace(dev, r.session)] == ["request", "interrupt", "cancel", "response"]
    # the client went away: a foreground turn is stopped and says so; a background one is not
    r = ask(sessions, "gone")
    sessions.claim(w)
    sessions.started(w, r.id)
    assert sessions.disconnected(dev, r.id).cancel and sessions.commands(w, [r.id])[r.id]["cancel"]
    finish(sessions, w, r.id, outcome="stopped", exit_code=0)
    got = sessions.response(dev, r.id)
    assert got.status == "cancelled" and got.incomplete == {"reason": "client_disconnected"}
    assert sessions.response_object(got)["metadata"]["incomplete_reason"] == "client_disconnected"
    assert [t["kind"] for t in sessions.trace(dev, r.session)][-3:] == ["request", "disconnect", "response"]
    b = ask(sessions, "bg", background=True)
    sessions.claim(w)
    sessions.started(w, b.id)
    assert not sessions.disconnected(dev, b.id).cancel and not sessions.commands(w, [b.id])[b.id]["cancel"]
    finish(sessions, w, b.id)
    assert sessions.response(dev, b.id).status == "completed"
    # a lost worker
    r = ask(sessions, "lost")
    sessions.claim(w)
    store.records.put("worker", "", w.id, {**store.records.get("worker", "", w.id), "last_seen": time.time() - 999})
    assert sessions.sweep_lost() == [r.id]
    got = sessions.response(dev, r.id)
    assert got.status == "failed" and "lost" in got.error["message"] and not sessions.session(dev, r.session).running


def test_stateless_archive_delete(sessions):
    store = sessions.store
    dev = store.member("acme", "dev")
    w = worker(sessions)
    r = ask(sessions, store=False)
    job = sessions.claim(w)
    assert job["urls"]["snapshot_put"] == "" and not job["store"]
    finish(sessions, w, r.id, snapshot=False)
    s = sessions.session(dev, r.session)
    assert not s.stored and not s.snapshot_key
    with pytest.raises(Invalid, match="not stored"):
        ask(sessions, previous_response_id=r.id)
    with pytest.raises(NotFound):
        sessions.archive(dev, s.id)
    r = ask(sessions)
    sessions.claim(w)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("./workspace/out.txt", b"hello"), ("./state/session", b"sid")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    sessions.blobs.put(f"tenants/acme/sessions/{r.session}/snapshot.tgz", buf.getvalue())
    finish(sessions, w, r.id)
    with tarfile.open(fileobj=io.BytesIO(sessions.archive(dev, r.session))) as tar:
        assert tar.getnames() == ["./out.txt"]                             # the workspace, without the CLI's state
    assert [f["path"] for f in sessions.files(dev, r.session)] == ["out.txt"] and sessions.file(dev, r.session, "out.txt") == b"hello"
    sessions.delete(dev, r.session)
    with pytest.raises(NotFound):
        sessions.response(dev, r.id)
    assert sessions.blobs.list(f"tenants/acme/sessions/{r.session}") == []
