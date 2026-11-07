"""The worker (SPEC §7.5, §7.6) with a fake launcher: jobs from the queue become boxes on the substrate, and the duties
re-queue, delete and clean what the records say."""
from __future__ import annotations

from typing import Any

import pytest_asyncio

from harnessgateway.ports.launch import BoxSpec
from harnessgateway.worker.main import Worker

BODY = {"model": "shell", "input": "echo hi"}


class FakeLauncher:
    def __init__(self, fail: bool = False):
        self.started: list[BoxSpec] = []
        self.stopped: list[str] = []
        self.fail = fail
        self.reaped = 0

    async def start(self, spec: BoxSpec) -> str:
        if self.fail:
            raise OSError("no substrate")
        self.started.append(spec)
        return f"container-{spec.box_id[-6:]}"

    async def stop(self, reference: str) -> None:
        self.stopped.append(reference)

    async def alive(self, reference: str) -> bool:
        return reference not in self.stopped

    async def reap(self) -> int:
        self.reaped += 1
        return 0


@pytest_asyncio.fixture(autouse=True)
async def clean_queue(plat):
    """The job stream is shared by every test on this Redis: start each worker test from an empty one."""
    await plat.moments.r.delete("hg:jobs")
    await plat.moments.ensure_group()


async def drain(worker: Worker) -> list[tuple[str, dict[str, Any]]]:
    """Every job on the queue, run and acknowledged."""
    done = []
    while batch := await worker.plat.moments.consume(worker.name, block_ms=100):
        for job_id, kind, payload in batch:
            await worker.job(kind, payload)
            await worker.plat.moments.ack(job_id)
            done.append((kind, payload))
    return done


async def box_row(plat, box_id):
    async with plat.db.platform() as tx:
        return await tx.get("SELECT * FROM boxes WHERE id = %s", box_id)


async def test_install_and_launch_jobs_start_boxes_with_the_organizations_limits(plat, org, shell):
    launcher = FakeLauncher()
    worker = Worker(plat, launcher)
    await plat.orgs.update(org.id, limits={"vcpus": 1.5, "memory_mb": 1024})
    done = await drain(worker)
    assert ("install", {"install_id": shell["install"]["id"], "org_id": org.id}) in done
    assert len(launcher.started) == 1 and launcher.started[0].kind == "install"
    spec = launcher.started[0]
    assert spec.vcpus == 1.5 and spec.memory_mb == 1024 and spec.token.startswith("hgb_") and spec.org_id == org.id
    assert spec.env("http://api.test:8000", "hg-door:3128") == {"HG_BOX_ID": spec.box_id, "HG_BOX_TOKEN": spec.token,
                                                                "HG_API_URL": "http://api.test:8000", "HG_DOOR": "hg-door:3128"}
    row = await box_row(plat, spec.box_id)
    assert row["launcher_ref"] == f"container-{spec.box_id[-6:]}" and row["status"] == "starting"
    box = await plat.boxes.authenticate(spec.box_id, spec.token)
    await plat.boxes.installed(box, {"ok": True, "bytes": 1, "log_tail": "ok"})
    turn = await plat.turns.create(org.admin, BODY)
    done = await drain(worker)
    kinds = [k for k, _ in done]
    assert kinds.count("stop") == 1 and kinds.count("launch") == 1              # the install box stopped, a session box launched
    assert launcher.stopped == [f"container-{spec.box_id[-6:]}"]
    session_box = launcher.started[-1]
    assert session_box.kind == "session"
    async with plat.db.platform() as tx:
        assert await tx.value("SELECT session_id FROM boxes WHERE id = %s", session_box.box_id) == turn["session_id"]


async def test_a_launch_the_substrate_refuses_fails_the_turn(plat, org, ready):
    worker = Worker(plat, FakeLauncher(fail=True))
    await drain(worker)                                                        # the install box's stop job
    turn = await plat.turns.create(org.admin, BODY)
    await drain(worker)
    doc = await plat.turns.response(org.admin, turn["id"])
    assert doc["status"] == "failed" and doc["metadata"]["hg_reason"] == "sandbox_failed"
    async with plat.db.platform() as tx:
        assert await tx.value("SELECT status FROM boxes WHERE session_id = %s", turn["session_id"]) == "gone"


async def test_delete_session_ends_the_box_and_purges(plat, org, ready, runner):
    launcher = FakeLauncher()
    worker = Worker(plat, launcher)
    turn = await plat.turns.create(org.admin, BODY)
    box = await runner.boot(turn["session_id"])                                # the fake runner does the worker's part
    await runner.turn(box, await runner.next(box), files={"workspace/a.txt": b"A"})
    await plat.containers.delete(org.admin, turn["session_id"])
    done = await drain(worker)
    assert ("delete_session", {"session_id": turn["session_id"], "org_id": org.id}) in done
    assert (await box_row(plat, box["id"]))["status"] == "gone"
    assert launcher.stopped                                                    # the launcher was told to stop it
    async with plat.db.platform() as tx:
        assert await tx.value("SELECT count(*) FROM turns WHERE session_id = %s", turn["session_id"]) == 0
        assert await tx.value("SELECT refs FROM blobs WHERE org_id = %s", org.id) == 0


async def test_the_duties_requeue_delete_and_clean(plat, org, ready):
    launcher = FakeLauncher()
    worker = Worker(plat, launcher)
    await drain(worker)
    turn = await plat.turns.create(org.admin, BODY)
    async with plat.db.platform() as tx:                                       # a launch that never happened, a minute ago
        await tx.run("UPDATE boxes SET created_at = now() - interval '2 minutes' WHERE session_id = %s", turn["session_id"])
        await tx.run("DELETE FROM installs WHERE org_id = %s AND status = 'pending'", org.id)
    assert await worker.reconcile() == 1
    assert [k for k, _ in await drain(worker)] == ["launch", "launch"]          # the first one, and the re-queued one
    assert len(launcher.started) == 1 and launcher.started[-1].kind == "session"   # started once
    other = await plat.turns.create(org.admin, {**BODY, "store": False})
    await plat.turns.cancel(org.admin, other["id"])
    async with plat.db.platform() as tx:
        await tx.run("UPDATE sessions SET last_active_at = now() - interval '1 hour' WHERE id = %s", other["session_id"])
        await tx.run("UPDATE boxes SET status = 'gone' WHERE session_id = %s", other["session_id"])
    assert await worker.purge() >= 1
    assert ("delete_session", {"session_id": other["session_id"], "org_id": org.id}) in await drain(worker)
    await plat.orgs.update(org.id, limits={"retention_days": 1})
    await plat.turns.cancel(org.admin, turn["id"])
    async with plat.db.platform() as tx:
        await tx.run("UPDATE sessions SET last_active_at = now() - interval '3 days' WHERE id = %s", turn["session_id"])
    assert await worker.retention() == 1
    assert ("delete_session", {"session_id": turn["session_id"], "org_id": org.id}) in await drain(worker)
    assert (await plat.containers.list(org.admin, 10, None, "desc"))["data"] == []
