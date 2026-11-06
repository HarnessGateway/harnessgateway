"""The services on a real Postgres and Redis (scratch ones from tests/conftest.py), with the file object store in a
temporary directory and no worker, door or sandbox: what the platform guarantees, without a deploy. A fake runner
drives the box routes the way sandbox/runner.py does. The database is migrated once; each test opens its own Platform
on it and works in organizations of its own."""
from __future__ import annotations

import asyncio
import hashlib
import io
import secrets
import tarfile
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from hgkit import Emitter
from hgsnapshot import Entry, Manifest

from harnessgateway.config import Settings
from harnessgateway.domain.access import ADMIN, DEVELOPER, Principal
from harnessgateway.services.platform import Platform

SHELL = Path(__file__).resolve().parents[2] / "packages" / "shell"


def token_of(link: str) -> str:
    return parse_qs(urlsplit(link).query)["token"][0]


def pack(folder: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(folder.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                tar.add(p, arcname=p.relative_to(folder).as_posix())
    return buf.getvalue()


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


def said(text: str, **metadata: str) -> list[str]:
    """The stdout lines of a run that says `text` and ends completed."""
    buf = io.StringIO()
    out = Emitter(buf)
    out.say(text)
    out.done(metadata=metadata)
    return buf.getvalue().splitlines()


def snapshot(files: dict[str, bytes]) -> tuple[Manifest, dict[str, bytes]]:
    """A manifest over `files` (paths under the roots, e.g. `workspace/a.txt`) and the blobs it names."""
    entries: list[Entry] = []
    blobs: dict[str, bytes] = {}
    dirs: set[str] = set()
    for path, data in sorted(files.items()):
        parts = path.split("/")
        dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
        digest = hashlib.sha256(data).hexdigest()
        blobs[digest] = data
        entries.append(Entry(path, "f", 0o644, 1_700_000_000_000_000_000, len(data), (digest,) if data else ()))
    entries += [Entry(d, "d", 0o755) for d in sorted(dirs)]
    return Manifest(entries), blobs


@pytest.fixture(scope="session")
def database_url(scratch_database) -> str:
    return scratch_database()


@pytest.fixture(scope="session")
def settings(database_url: str, redis_url: str, tmp_path_factory: pytest.TempPathFactory) -> Settings:
    blobs = tmp_path_factory.mktemp("blobs")
    s = Settings.load(("api",), env={"HG_DATABASE_URL": database_url, "HG_REDIS_URL": redis_url,
                                     "HG_BLOB_URL": f"file://{blobs}",
                                     "HG_SECRET_KEY": "dGVzdC1zZWNyZXQta2V5LW9mLXRoaXJ0eS10d28tYnl0ZXM=",
                                     "HG_OPERATOR_KEY": "hgo_test", "HG_DOOR_KEY": "hgd_test",
                                     "HG_API_URL": "http://api.test:8000", "HG_CONSOLE_URL": "http://console.test:8800",
                                     "HG_DEFAULT_STANDBY_S": "1"})

    async def migrate() -> None:
        plat = Platform(s)
        await plat.open()
        try:
            await plat.migrate()
        finally:
            await plat.close()
    worker = threading.Thread(target=lambda: asyncio.run(migrate()))   # its own loop: a test's may be running
    worker.start()
    worker.join()
    return s


@pytest_asyncio.fixture
async def plat(settings: Settings) -> AsyncIterator[Platform]:
    plat = Platform(settings)
    await plat.open()
    try:
        yield plat
    finally:
        await plat.close()


class Org:
    """An organization made for one test: its id and three principals (an admin key, a developer key, a run-only key).
    Tests about people create accounts of their own."""

    def __init__(self, org_id: str):
        self.id = org_id
        self.admin = Principal(org_id, "key:admin", ADMIN)
        self.developer = Principal(org_id, "key:dev", DEVELOPER)
        self.app = Principal(org_id, "key:app", DEVELOPER, run_only=True)


@pytest_asyncio.fixture
async def org(plat: Platform) -> Org:
    created = await plat.orgs.create(f"org-{secrets.token_hex(4)}")
    return Org(str(created["id"]))


@pytest_asyncio.fixture
async def shell(plat: Platform, org: Org) -> dict[str, Any]:
    """The shell package installed in the test's organization (its install pending: no box ran it yet)."""
    return await plat.harnesses.install(org.admin, pack(SHELL))


class Runner:
    """A stand-in for sandbox/runner.py: it calls the box routes in the runner's order, with the bytes it would."""

    def __init__(self, plat: Platform):
        self.plat = plat
        self.have: set[str] = set()                   # blobs this box uploaded before (its local cache)

    async def install(self, org_id: str, digest: str, *, ok: bool = True, log_tail: str = "installed") -> dict[str, Any]:
        """An install box's whole life: launched, the archive uploaded, the result reported."""
        boxes = self.plat.boxes
        install = await self.plat.installs.ensure(org_id, digest)
        box_id = await boxes.ensure_install_box(org_id, install["id"])
        assert box_id, "an install box could not be made"
        spec = await boxes.to_launch(box_id)
        assert spec
        await boxes.launched(box_id, "install-container")
        box = await boxes.authenticate(box_id, spec["token"])
        info = await boxes.info(box)
        assert info["kind"] == "install" and info["package"]["digest"] == digest
        await boxes.ready(box)
        size = await boxes.put_archive(box, chunks(b"not really a tar")) if ok else 0
        await boxes.installed(box, {"ok": ok, "bytes": size, "log_tail": log_tail, "cpu_seconds": 0.5})
        return await self.plat.installs.get(org_id, install["id"])

    async def boot(self, sid: str) -> dict[str, Any]:
        """The session's box as the worker would launch it and the runner would start: authenticated and ready."""
        boxes = self.plat.boxes
        async with self.plat.db.platform() as tx:
            box_id = await tx.value("SELECT id FROM boxes WHERE session_id = %s AND status <> 'gone'", sid)
        assert box_id, f"no live box for {sid}"
        spec = await boxes.to_launch(box_id)
        assert spec
        await boxes.launched(box_id, f"container-{box_id[-6:]}")
        box = await boxes.authenticate(box_id, spec["token"])
        box["_token"] = spec["token"]
        await boxes.ready(box)
        return box

    async def info(self, box: dict[str, Any]) -> dict[str, Any]:
        return await self.plat.boxes.info(box)

    async def next(self, box: dict[str, Any], wait_s: float = 0) -> dict[str, Any] | None:
        return await self.plat.boxes.next(box, wait_s)

    async def turn(self, box: dict[str, Any], job: dict[str, Any], lines: list[str] | None = None, *,
                   outcome: str = "exited", exit_code: int = 0, files: dict[str, bytes] | None = None,
                   report: dict[str, Any] | None = None) -> dict[str, Any]:
        """One turn: its output lines sent, its exit reported, its state saved when asked. The finish reply."""
        boxes, rid = self.plat.boxes, job["turn_id"]
        if lines is None:
            lines = said("hi")
        reply = await boxes.events(box, rid, 1, lines)
        done = await boxes.finish(box, rid, {"outcome": outcome, "exit_code": exit_code, "stderr_tail": "", "cpu_seconds": 1.5,
                                             "memory_peak_mb": 100, "stdout_bytes": 10, "stderr_bytes": 0, "logs": False,
                                             **(report or {})})
        if done["persist"]:
            if files is None:
                await boxes.saved(box, rid, {})
            else:
                await self.save(box, rid, files)
        reply["persist"] = done["persist"]
        return reply

    async def save(self, box: dict[str, Any], rid: str, files: dict[str, bytes]) -> dict[str, Any]:
        """The state after `rid`: blobs the store lacks uploaded, the manifest recorded."""
        boxes = self.plat.boxes
        manifest, blobs = snapshot(files)
        new = [h for h in sorted(blobs) if h not in self.have]
        for h in await boxes.blobs_missing(box, new):
            await boxes.put_blob(box, h, chunks(blobs[h]))
        self.have |= set(blobs)
        return await boxes.put_snapshot(box, rid, manifest.dumps())

    async def bye(self, box: dict[str, Any]) -> None:
        await self.plat.boxes.bye(box)


@pytest_asyncio.fixture
async def runner(plat: Platform) -> Runner:
    return Runner(plat)


@pytest_asyncio.fixture
async def ready(plat: Platform, org: Org, shell: dict[str, Any], runner: Runner) -> dict[str, Any]:
    """The shell package installed and its install run: turns can get boxes."""
    install = await runner.install(org.id, shell["digest"])
    assert install["status"] == "ready"
    return shell
