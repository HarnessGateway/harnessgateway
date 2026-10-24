"""Layer 4: metering. A turn's usage is compute reserved for its wall time, egress through the door
and the gateway, the snapshot it wrote, and the tokens the provider counted; storage is sampled once
a day from blob sizes; the summary narrows to a team, a member or a harness and groups by each;
housekeeping samples what is due and fails lost turns; readings go to billing when it is on."""
import time

import pytest

from fakes import archive
from harnessgateway.backends.blobs.dir import DirBlobs
from harnessgateway.backends.records.sqlite import SqliteRecords
from harnessgateway.contract import wire
from harnessgateway.services.metering import daily, memory_bytes, totals
from domainfix import domain
from test_sessions import DONE, MSG


class FakeBilling:
    enabled = True

    def __init__(self):
        self.reports = []

    def report(self, customer, readings, at):
        self.reports.append((customer, dict(readings), at))


@pytest.fixture
def d(tmp_path, vault):
    d = domain(SqliteRecords(tmp_path / "r.db"), DirBlobs(tmp_path / "b", "http://api.test", b"s"), vault, tmp_path / "git")
    d.orgs.create("acme", {"max_runs": 4, "cpus": 2, "memory": "4g"})
    d.members.create("acme", "chase", "owner")
    d.members.create("acme", "dev", "member")
    d.members.create("acme", "ops", "member")
    d.teams.put("acme", "platform", ["dev"], "Platform")
    d.providers.put("acme", "prov", "https://api.example/v1", "openai-chat", "sk-real-secret")
    d.harnesses.create_from_archive("acme", "cc", archive(), "chase")
    d.harnesses.create_from_archive("acme", "team-cc", archive(), "chase", "platform")
    d.metering.billing = FakeBilling()
    return d


def run(d, w, member, model, seconds=10.0, **report):
    who = d.members.get("acme", member)
    r = d.sessions.create(who, wire.Request.parse({"model": model, "input": "go"}))
    assert d.scheduler.claim(w)["id"] == r.id
    d.scheduler.started(w, r.id)
    return d.scheduler.result(w, r.id, {"outcome": "exited", "exit_code": 0, "stdout": f"{MSG}\n{DONE}\n", "stderr": "", "seconds": seconds, "snapshot": True, **report})


def test_a_turn_is_metered_as_compute_egress_snapshot_and_tokens(d):
    w, _ = d.nodes.register("w1", 4, "t")
    d.records.put("usage", "acme", "resp_pending", {"calls": 0})   # not this turn's
    r = run(d, w, "dev", "cc", seconds=10.0, egress_bytes=1000, snapshot_bytes=5000)
    u = d.records.get("usage", "acme", r.id)
    assert u["seconds"] == 10.0 and u["cpu_seconds"] == 20.0 and u["memory_gb_seconds"] == 40.0
    assert u["egress_bytes"] == 1000 and u["snapshot_bytes"] == 5000 and u["input_tokens"] == 2 and u["output_tokens"] == 3
    assert u["harness"] == "cc" and u["member"] == "dev" and u["team"] == "" and u["session"] == r.session and u["status"] == "completed"
    customer, readings, at = d.metering.billing.reports[-1]
    assert customer == "" and readings == {"cpu_seconds": 20.0, "memory_gb_seconds": 40.0, "egress_bytes": 1000} and at == u["at"]
    # the gateway's bytes count as egress too, and the gateway's calls are kept
    d.records.put("usage", "acme", "resp_x", {"calls": 2, "bytes_in": 300, "bytes_out": 700})
    r2 = run(d, w, "dev", "cc", seconds=1.0)
    assert d.records.get("usage", "acme", r2.id)["calls"] == 0            # a fresh turn starts from nothing
    assert memory_bytes("512m") == 512 * 1024 ** 2 and memory_bytes("2g") == 2 * 1024 ** 3 and memory_bytes("bad") == 0


def test_storage_is_sampled_from_blob_sizes_once_a_day(d):
    d.blobs.put("tenants/acme/sessions/sess_1/snapshot.tgz", b"x" * 1000)
    d.blobs.put("tenants/acme/members/dev/harnesses/cc/home.tgz", b"y" * 10)
    d.blobs.put("tenants/other/sessions/sess_9/snapshot.tgz", b"z" * 999)
    sample = d.metering.sample_storage("acme", at=1_700_000_000)
    assert sample["sessions"] == 1000 and sample["members"] == 10 and sample["harnesses"] > 0 and sample["day"] == "2023-11-14"
    assert sample["bytes"] == 1010 + sample["harnesses"]
    assert d.metering.billing.reports[-1][1] == {"storage_gb_days": sample["bytes"] / 1024 ** 3}
    # due once per day per organization
    d.orgs.create("beta")
    assert d.metering.sample_due(at=1_700_000_000) == ["beta"]
    assert d.metering.sample_due(at=1_700_000_000) == []
    assert sorted(d.metering.sample_due(at=1_700_100_000)) == ["acme", "beta"]
    assert d.metering.summary("acme", now=1_700_100_000)["storage"]["day"] == "2023-11-16"


def test_summary_narrows_and_groups(d):
    w, _ = d.nodes.register("w1", 4, "t")
    run(d, w, "dev", "team-cc", seconds=2.0)
    run(d, w, "dev", "cc", seconds=3.0)
    run(d, w, "ops", "cc", seconds=5.0)
    whole = d.metering.summary("acme", days=0)
    assert whole["totals"]["turns"] == 3 and whole["totals"]["seconds"] == 10.0 and whole["totals"]["cpu_seconds"] == 20.0
    assert [h["harness"] for h in whole["harnesses"]] == ["cc", "team-cc"] and whole["harnesses"][0]["turns"] == 2
    assert [m["member"] for m in whole["members"]] == ["dev", "ops"] and [t["team"] for t in whole["teams"]] == ["", "platform"]
    assert len(whole["sessions"]) == 3 and set(whole["meters"]) == {"cpu_seconds", "memory_gb_seconds", "egress_bytes", "storage_gb_days"}
    assert whole["daily"][-1]["turns"] == 3 and len(whole["daily"]) == 1
    assert whole["storage"]["day"] == time.strftime("%Y-%m-%d", time.gmtime()) and whole["storage"]["sessions"] > 0   # sampled on first ask
    assert d.metering.summary("acme", team="platform")["totals"]["turns"] == 1
    assert d.metering.summary("acme", member="ops")["totals"]["seconds"] == 5.0
    assert d.metering.summary("acme", harness="cc", member="dev")["totals"]["turns"] == 1
    assert d.metering.summary("acme", days=30)["daily"].__len__() == 30
    # a window that ends before the turns sees none of them
    assert d.metering.summary("acme", days=1, now=time.time() - 86400 * 3)["totals"]["turns"] == 0


def test_totals_and_daily_helpers():
    rows = [{"at": 0, "seconds": 1, "cpu_seconds": 2}, {"at": 86400 * 2, "seconds": 1.5, "egress_bytes": 7}]
    t = totals(rows)
    assert t["turns"] == 2 and t["seconds"] == 2.5 and t["cpu_seconds"] == 2 and t["egress_bytes"] == 7 and t["input_tokens"] == 0
    days = daily(rows, 0, 86400 * 2 + 10)
    assert [x["day"] for x in days] == ["1970-01-01", "1970-01-02", "1970-01-03"] and [x["turns"] for x in days] == [1, 0, 1]
    assert daily([], 0, 1000)[0]["turns"] == 0 and len(daily([], 7, 1000)) == 7


def test_housekeeping_passes_sample_storage_on_schedule_and_sweep_lost_turns(d, monkeypatch):
    h = d.housekeeping
    h.every_s, h.sample_every_s = 15, 60
    first = h.pass_once()
    assert first == {"lost": [], "sampled": ["acme"]}
    assert all(p["sampled"] == [] for p in (h.pass_once(), h.pass_once(), h.pass_once()))
    assert h.pass_once()["sampled"] == []                                   # the hour came round: nothing due today
    # a turn on a node that went quiet is failed on the next pass
    w, _ = d.nodes.register("w1", 4, "t")
    who = d.members.get("acme", "dev")
    r = d.sessions.create(who, wire.Request.parse({"model": "cc", "input": "go"}))
    d.scheduler.claim(w)
    d.records.put("worker", "", w.id, {**d.records.get("worker", "", w.id), "last_seen": time.time() - 10_000})
    assert h.pass_once()["lost"] == [r.id] and d.sessions.turn_by_id(r.id).status == "failed"
    assert d.records.get("usage", "acme", r.id)["status"] == "failed"      # the lost turn is metered too
