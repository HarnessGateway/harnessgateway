"""The node agent (SPEC §6.2): joins, then runs four loops (heartbeat, claim, meter, evict idle) and one task per turn.

A turn on this node:
  1. the composed package (base, plus an overlay's seeds), verified against its digests, and its tools (install.sh,
     once per base digest and sandbox image, in an install box with the install egress);
  2. the session's box: the warm one when this node ran the session's last turn, otherwise a new box with the home
     seeds, then the session's snapshot (or, for a new session, the workspace seeds);
  3. the turn's inputs into the read-only /run/hg, staged container files applied to the workspace, and the door's
     rules for the turn: its egress, its secret bindings, its door token;
  4. turn.sh runs run.sh; stdout goes to the API in batches of whole lines, stderr is kept;
  5. the process exits or is stopped (cancel, timeout, the grace after its terminal event): the door's bindings are
     cleared, the end is reported, and the session's state is saved in the background. The box stays warm until its
     idle time runs out.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from .. import ids
from ..adapters.sandbox_docker import DockerRuntime
from ..config import Settings
from ..domain import package as pkg
from ..domain.meters import Kind, Meter
from ..domain.turn import Reason
from ..limits import LOG_TAIL
from ..ports.sandbox import UID, Box, BoxSpec, Mount, Process, Runtime
from . import state
from .client import ApiClient
from .pump import LineTooLarge, Pump
from .trust import DOOR_UID, Trust

HEARTBEAT_S = 3
CLAIM_IDLE_S = 1
METER_S = 30
EVICT_EVERY_S = 5
EXIT_WAIT_S = 30
SHUTDOWN_SAVES_S = 90
PIDFILE = "/tmp/hg-turn.pid"
TURN_SH = "/opt/hg/runtime/turn.sh"
log = logging.getLogger("harnessgateway.node")


class InstallFailed(Exception):
    pass


class TurnFailed(Exception):
    def __init__(self, reason: Reason, message: str):
        super().__init__(message)
        self.reason = reason


class Door:
    """The node's side of a box's door: its rules file, its counters and its connection log."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.offset = 0

    def create(self) -> None:
        fresh_dir(self.folder, owner=DOOR_UID, mode=0o700)
        self.rules(allow=[])

    def rules(self, *, allow: list[str], turn: str | None = None, bindings: list[dict[str, Any]] | None = None,
              door_token: str = "") -> None:
        tmp = self.folder / "rules.tmp"
        tmp.write_text(json.dumps({"turn": turn, "allow": allow, "bindings": bindings or [], "door_token": door_token}))
        os.chown(tmp, DOOR_UID, DOOR_UID)
        tmp.chmod(0o400)
        os.replace(tmp, self.folder / "rules.json")

    def counters(self) -> dict[str, int]:
        try:
            doc = json.loads((self.folder / "counters.json").read_text())
            return {"out": int(doc.get("out", 0)), "in": int(doc.get("in", 0))}
        except (OSError, ValueError):
            return {"out": 0, "in": 0}

    def connections(self, turn: str) -> list[dict[str, Any]]:
        """The connection records written since the last call, of this turn."""
        path = self.folder / "connections.jsonl"
        if not path.is_file():
            return []
        with path.open("rb") as f:
            f.seek(self.offset)
            data = f.read()
        self.offset += len(data)
        out = []
        for line in data.decode("utf-8", "replace").splitlines():
            with contextlib.suppress(ValueError):
                rec = json.loads(line)
                if rec.get("turn") == turn:
                    rec.pop("turn", None)
                    out.append(rec)
        return out


@dataclass
class Warm:
    """A session's box on this node."""
    box: Box
    door: Door
    session_id: str
    org_id: str
    harness: str
    package_key: str
    vcpus: float
    memory_mb: int
    idle_ttl_s: int
    fingerprint: str = ""
    busy: bool = False
    turn_id: str | None = None
    idle_since: float = field(default_factory=time.monotonic)
    metered_at: float = field(default_factory=time.monotonic)
    counted: dict[str, int] = field(default_factory=lambda: {"out": 0, "in": 0})


@dataclass
class Running:
    job: dict[str, Any]
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    reason: Reason | None = None

    def stop_for(self, reason: Reason) -> None:
        if not self.stop.is_set():
            self.reason = reason
            self.stop.set()


@dataclass
class Save:
    rid: str
    session_id: str
    links: dict[str, str]
    archive: Path | None = None
    size: int = 0
    files: list[dict[str, Any]] = field(default_factory=list)
    fingerprint: str = ""
    error: str = ""


def fresh_dir(path: Path, owner: int | None = None, mode: int = 0o755) -> Path:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, mode=mode)
    path.chmod(mode)
    if owner is not None:
        os.chown(path, owner, owner)
    return path


def empty_dir(path: Path) -> Path:
    """A directory's contents removed, the directory kept: a box's bind mount refers to its inode."""
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    return path


def failure(reason: Reason, message: str) -> dict[str, Any]:
    return {"outcome": "failed", "reason": reason.value, "error": message[:4000]}


def package_key(job: dict[str, Any]) -> str:
    p = job["package"]
    return hashlib.sha256(f"{p['base']['digest']}\0{(p['overlay'] or {}).get('digest', '')}".encode()).hexdigest()


class Node:
    def __init__(self, settings: Settings):
        self.s = settings
        self.dir = settings.node_dir
        self.client = ApiClient(settings.api_url, self.dir / "node.json")
        self.trust = Trust(self.dir, settings.node_name, settings.ca_file)
        self.runtime: Runtime | None = None
        self.image_id = ""
        self.warm: dict[str, Warm] = {}
        self.running: dict[str, Running] = {}
        self.saving: Counter[str] = Counter()
        self.saves_done = asyncio.Condition()
        self.locks: dict[str, asyncio.Lock] = {}
        self.pending_usage: list[dict[str, Any]] = []
        self.tasks: set[asyncio.Task[None]] = set()
        self.draining = False

    def lock(self, key: str) -> asyncio.Lock:
        return self.locks.setdefault(key, asyncio.Lock())

    def _spawn(self, work: Any) -> None:
        task = asyncio.create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    @property
    def rt(self) -> Runtime:
        assert self.runtime is not None, "the runtime exists once the node has joined"
        return self.runtime

    # ── lifecycle ──────────────────────────────────────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        for sub in ("packages", "tools", "runtime"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        for sub in ("sessions", "saves", "logs", "doors"):
            fresh_dir(self.dir / sub)
        turn_sh = self.dir / "runtime" / "turn.sh"
        turn_sh.write_bytes(resources.files("harnessgateway.node").joinpath("runtime/turn.sh").read_bytes())
        turn_sh.chmod(0o755)
        node_id = await self.client.join(self.s.node_join_token, self.s.node_name, self.s.node_slots)
        runtime = DockerRuntime(self.s, node_id, self.trust)
        self.runtime = runtime
        removed = await runtime.sweep()
        self.image_id = await runtime.prepare()
        self.trust.ensure(await runtime.ca_bundle())
        log.info("node %s ready: %d slots, runtime %s (removed %d leftover boxes)", node_id, self.s.node_slots,
                 self.s.runtime, removed)
        try:
            await asyncio.gather(self._heartbeats(), self._claims(), self._meter(), self._evict_idle())
        finally:
            await self._shutdown()
            await self.client.close()

    async def _shutdown(self) -> None:
        """A graceful stop: saves under way finish (for a while), then this node's boxes go. Every session restores
        from its last save wherever its next turn runs."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SHUTDOWN_SAVES_S
        while self.saving and loop.time() < deadline:
            await asyncio.sleep(0.5)
        removed = await self.rt.sweep()
        log.info("node stopped: removed %d boxes", removed)

    async def _heartbeats(self) -> None:
        while True:
            try:
                reply = await self.client.heartbeat(list(self.running), list(self.warm),
                                                    [s for s, n in self.saving.items() if n > 0], self.s.node_slots)
                self.draining = bool(reply.get("draining"))
                for rid in reply.get("cancel", []):
                    if rid in self.running:
                        self.running[rid].stop_for(Reason.CANCELLED)
                for sid in reply.get("evict", []):
                    if warm := self.warm.get(sid):
                        await self._evict(warm)
            except Exception as e:
                log.warning("heartbeat failed: %s", e)
            await asyncio.sleep(HEARTBEAT_S)

    async def _claims(self) -> None:
        while True:
            if len(self.running) >= self.s.node_slots or self.draining:
                await asyncio.sleep(0.5)
                continue
            try:
                job = await self.client.claim()
            except Exception as e:
                log.warning("claim failed: %s", e)
                job = None
            if job is None:
                await asyncio.sleep(CLAIM_IDLE_S)
                continue
            self.running[job["turn_id"]] = Running(job)
            self._spawn(self._turn(job))

    async def _evict_idle(self) -> None:
        while True:
            await asyncio.sleep(EVICT_EVERY_S)
            now = time.monotonic()
            for warm in list(self.warm.values()):
                if now - warm.idle_since >= warm.idle_ttl_s:
                    await self._evict(warm)

    # ── metering ───────────────────────────────────────────────────────────────────────────────────────────────────
    async def _meter(self) -> None:
        while True:
            await asyncio.sleep(METER_S)
            for warm in list(self.warm.values()):
                self._measure(warm)
            await self._send_usage()

    def _usage(self, base: dict[str, Any], vcpus: float, memory_mb: int, start: float, end: float, out: int,
               inn: int) -> None:
        seconds = max(end - start, 0)
        for meter, quantity in ((Meter.VCPU_SECONDS, vcpus * seconds), (Meter.MEMORY_GIB_SECONDS, memory_mb / 1024 * seconds),
                                (Meter.EGRESS_BYTES, max(out, 0)), (Meter.INGRESS_BYTES, max(inn, 0))):
            if quantity > 0:
                self.pending_usage.append({**base, "id": ids.new("usage"), "meter": meter.value,
                                           "quantity": round(quantity, 6), "start": start, "end": end})

    def _measure(self, warm: Warm) -> None:
        now_wall, now = time.time(), time.monotonic()
        start = now_wall - (now - warm.metered_at)
        warm.metered_at = now
        c = warm.door.counters()
        out, inn = c["out"] - warm.counted["out"], c["in"] - warm.counted["in"]
        warm.counted = c
        kind = Kind.TURN if warm.turn_id else Kind.WARM
        self._usage({"org_id": warm.org_id, "kind": kind.value, "harness": warm.harness, "session_id": warm.session_id,
                     "response_id": warm.turn_id}, warm.vcpus, warm.memory_mb, start, now_wall, out, inn)

    async def _send_usage(self) -> None:
        rows, self.pending_usage = self.pending_usage, []
        try:
            await self.client.usage(rows)
        except Exception:
            log.exception("reporting usage failed; keeping it for the next report")
            self.pending_usage = rows + self.pending_usage

    # ── boxes ──────────────────────────────────────────────────────────────────────────────────────────────────────
    async def _make_room(self) -> None:
        for warm in sorted(self.warm.values(), key=lambda w: w.idle_since):
            if len(self.warm) < self.s.node_warm_max:
                return
            await self._evict(warm)

    async def _evict(self, warm: Warm) -> None:
        lock = self.lock(f"session:{warm.session_id}")
        if lock.locked() or not self._closable(warm):
            return
        async with lock:
            if self._closable(warm):
                await self._close(warm)

    def _closable(self, warm: Warm) -> bool:
        return not warm.busy and not self.saving[warm.session_id]

    async def _close(self, warm: Warm) -> None:
        if self.warm.get(warm.session_id) is not warm:
            return
        self._measure(warm)
        del self.warm[warm.session_id]
        await warm.box.destroy()
        shutil.rmtree(self.dir / "sessions" / warm.session_id, ignore_errors=True)
        shutil.rmtree(warm.door.folder, ignore_errors=True)

    # ── packages and tools ─────────────────────────────────────────────────────────────────────────────────────────
    async def _archive_dir(self, spec: dict[str, Any], into: Path) -> None:
        await asyncio.to_thread(pkg.unpack, await self.client.fetch(spec["url"]), into)
        if await asyncio.to_thread(pkg.digest, into) != spec["digest"]:
            raise TurnFailed(Reason.SANDBOX_FAILED, "a package archive does not match its digest")

    async def _package(self, job: dict[str, Any]) -> Path:
        """The composed package a box mounts, built once per (base, overlay) pair."""
        key = package_key(job)
        folder = self.dir / "packages" / key
        async with self.lock(f"pkg:{key}"):
            if folder.is_dir():
                return folder
            with tempfile.TemporaryDirectory(dir=self.dir / "packages") as tmp:
                base, staging = Path(tmp) / "base", Path(tmp) / "composed"
                await self._archive_dir(job["package"]["base"], base)
                if job["package"]["overlay"]:
                    overlay = Path(tmp) / "overlay"
                    await self._archive_dir(job["package"]["overlay"], overlay)
                    await asyncio.to_thread(pkg.overlay_onto, base, overlay, staging, json.dumps(job["package"]["manifest"]))
                else:
                    base.rename(staging)
                for p in [staging, *staging.rglob("*")]:
                    p.chmod(0o755 if p.is_dir() or p.stat().st_mode & 0o111 else 0o644)
                staging.rename(folder)
        return folder

    def tools_key(self, base_digest: str) -> str:
        return hashlib.sha256(f"{base_digest}\0{self.image_id}".encode()).hexdigest()

    async def _tools(self, job: dict[str, Any], package_dir: Path) -> Path:
        key = self.tools_key(job["package"]["base"]["digest"])
        tools = self.dir / "tools" / key
        async with self.lock(f"tools:{key}"):
            if tools.is_dir():
                return tools
            manifest, size = job["package"]["manifest"], job["box"]
            staging = fresh_dir(self.dir / "tools" / f".{key}.partial", owner=UID)
            door = Door(self.dir / "doors" / f"i{key[:16]}{os.urandom(3).hex()}")
            door.create()
            door.rules(allow=job["egress"]["install"], turn=job["turn_id"])
            box = self.rt.box(BoxSpec(name=door.folder.name, org_id=job["org_id"], vcpus=size["vcpus"],
                                      memory_mb=size["memory_mb"], pids=size["pids"], tmp_mb=max(size["tmp_mb"], 4096),
                                      mounts=(Mount(package_dir, "/opt/hg/package"),
                                              Mount(staging, "/opt/hg/tools", readonly=False)),
                                      door_dir=door.folder,
                                      env={"HOME": "/tmp/home", "HG_TOOLS": "/opt/hg/tools",
                                           "HG_PACKAGE_DIR": "/opt/hg/package", "HG_CONTRACT": "1"}))
            start, started = time.time(), time.monotonic()
            try:
                await box.start()
                proc = await box.exec(["bash", "-c", 'mkdir -p "$HOME" && cd /tmp && bash /opt/hg/package/install.sh'], {})
                code, tail = await self._drain(proc, manifest["install"]["timeout_s"])
                if code is None:
                    raise InstallFailed(f"install.sh ran past its {manifest['install']['timeout_s']}s")
                if code != 0:
                    raise InstallFailed(f"install.sh exited {code}: {tail[-2000:]}")
                staging.rename(tools)
            finally:
                c = door.counters()
                self._usage({"org_id": job["org_id"], "kind": Kind.INSTALL.value, "harness": job["harness"],
                             "response_id": job["turn_id"]}, size["vcpus"], size["memory_mb"], start,
                            start + time.monotonic() - started, c["out"], c["in"])
                await box.destroy()
                shutil.rmtree(staging, ignore_errors=True)
                shutil.rmtree(door.folder, ignore_errors=True)
        return tools

    @staticmethod
    async def _drain(proc: Process, timeout_s: float) -> tuple[int | None, str]:
        tail = bytearray()

        async def read_out() -> None:
            while chunk := await proc.read():
                tail.extend(chunk)
                del tail[:-LOG_TAIL]

        async def read_err() -> None:
            async for chunk in proc.stderr():
                tail.extend(chunk)
                del tail[:-LOG_TAIL]
        try:
            await asyncio.wait_for(asyncio.gather(read_out(), read_err()), timeout_s)
        except TimeoutError:
            proc.kill()
            return None, tail.decode(errors="replace")
        return await proc.wait(), tail.decode(errors="replace")

    # ── a session's box ────────────────────────────────────────────────────────────────────────────────────────────
    async def _box(self, job: dict[str, Any], package_dir: Path, tools: Path) -> Warm:
        sid, key = job["session_id"], package_key(job)
        warm = self.warm.get(sid)
        if warm:
            warm.busy = True
            if job["held"] and warm.package_key == key and await warm.box.alive():
                return warm
            await self._close(warm)
        if self.saving[sid]:
            async with self.saves_done:
                await self.saves_done.wait_for(lambda: not self.saving[sid])
            job["snapshot"] = await self.client.snapshot(job["turn_id"])
        await self._make_room()
        root = fresh_dir(self.dir / "sessions" / sid)
        dirs = {name: fresh_dir(root / name, owner=UID, mode=0o700) for name in ("workspace", "home", "state")}
        fresh_dir(root / "run")
        door = Door(self.dir / "doors" / sid.split("_", 1)[1])
        door.create()
        size = job["box"]
        box = self.rt.box(BoxSpec(name=sid.split("_", 1)[1], org_id=job["org_id"], vcpus=size["vcpus"],
                                  memory_mb=size["memory_mb"], pids=size["pids"], tmp_mb=size["tmp_mb"],
                                  mounts=(Mount(dirs["workspace"], state.BOX_WORKSPACE, False),
                                          Mount(dirs["home"], state.BOX_HOME, False),
                                          Mount(dirs["state"], state.BOX_STATE, False), Mount(root / "run", "/run/hg"),
                                          Mount(package_dir, "/opt/hg/package"), Mount(tools, "/opt/hg/tools"),
                                          Mount(self.dir / "runtime", "/opt/hg/runtime")), door_dir=door.folder))
        warm = Warm(box, door, sid, job["org_id"], job["harness"], key, size["vcpus"], size["memory_mb"],
                    size["idle_ttl_s"], busy=True)
        self.warm[sid] = warm
        try:
            await box.start()
            await state.seed(box, new_session=job["snapshot"]["url"] is None)
            if job["snapshot"]["url"]:
                archive = root / "restore.tgz"
                await self.client.download(job["snapshot"]["url"], archive)
                await state.restore(box, archive)
                archive.unlink()
            warm.fingerprint = (await state.listing(box, self._included(job))).fingerprint
        except Exception as e:
            await self._close(warm)
            raise TurnFailed(Reason.SANDBOX_FAILED, f"the session's sandbox could not start: {e}") from e
        return warm

    @staticmethod
    def _included(job: dict[str, Any]) -> list[str]:
        return state.included(job["package"]["manifest"].get("persist", []))

    async def _inputs(self, job: dict[str, Any], warm: Warm) -> list[str]:
        """/run/hg (a directory only the node writes), the staged container files applied, and the door's rules."""
        run = empty_dir(self.dir / "sessions" / job["session_id"] / "run")
        await self.client.download(job["request_url"], run / "request.json")
        (run / "mcp.json").write_text(json.dumps(job["mcp"]))
        files = empty_dir(run / "files")
        index = {}
        for f in job["files"]:
            dest = files / f["id"] / os.path.basename(f["filename"])
            await self.client.download(f["url"], dest)
            dest.parent.chmod(0o755)
            index[f["id"]] = {"filename": f["filename"], "path": f"/run/hg/files/{f['id']}/{dest.name}",
                              "bytes": dest.stat().st_size}
        (files / "index.json").write_text(json.dumps(index))
        applied = []
        for op in job["ops"]:
            if op["op"] == "put":
                await self.client.download(op["url"], run / "ops" / op["id"])
                (run / "ops").chmod(0o755)
                await warm.box.sh('mkdir -p "$(dirname "/workspace/$1")" && cp "$2" "/workspace/$1"', op["path"],
                                  f"/run/hg/ops/{op['id']}")
            else:
                await warm.box.sh('rm -rf -- "/workspace/$1"', op["path"])
            applied.append(op["id"])
        provider = job["provider"] or {}
        warm.door.rules(allow=job["egress"]["run"], turn=job["turn_id"], bindings=job["bindings"],
                        door_token=provider.get("door_token", ""))
        return applied

    # ── one turn ───────────────────────────────────────────────────────────────────────────────────────────────────
    async def _turn(self, job: dict[str, Any]) -> None:
        rid, sid = job["turn_id"], job["session_id"]
        save, warm = None, None
        try:
            async with self.lock(f"session:{sid}"):
                warm, report, applied = await self._run(job)
                try:
                    save = await self._report(job, warm, report, applied)
                finally:
                    if warm is not None:
                        warm.busy, warm.idle_since = False, time.monotonic()
                        if not job["store"] or (warm.idle_ttl_s == 0 and self._closable(warm)):
                            await self._close(warm)
        except Exception:
            log.exception("turn %s could not be reported", rid)
        finally:
            self.running.pop(rid, None)
            await self._send_usage()
        if save is not None:
            self._spawn(self._upload(save, warm))

    async def _report(self, job: dict[str, Any], warm: Warm | None, report: dict[str, Any], applied: list[str]
                      ) -> Save | None:
        persist = warm is not None and job["store"]
        report.update(persist=persist, applied_ops=applied)
        if warm is not None:
            self._measure(warm)
            warm.turn_id = None
            warm.door.rules(allow=[])                     # the turn's bindings and token end with it
            report["door"] = warm.door.connections(job["turn_id"])
        if not persist or warm is None:
            await self.client.finish(job["turn_id"], report)
            return None
        self.saving[job["session_id"]] += 1
        save = None
        try:
            reply = await self.client.finish(job["turn_id"], report)
            self.running.pop(job["turn_id"], None)
            save = await self._archive(job, warm, reply.get("snapshot"))
            return save
        finally:
            if save is None:
                await self._saved(job["session_id"])

    async def _run(self, job: dict[str, Any]) -> tuple[Warm | None, dict[str, Any], list[str]]:
        warm: Warm | None = None
        applied: list[str] = []
        try:
            package_dir = await self._package(job)
            tools = await self._tools(job, package_dir)
            warm = await self._box(job, package_dir, tools)
            warm.busy = True
            self._measure(warm)
            warm.turn_id = job["turn_id"]
            applied = await self._inputs(job, warm)
            return warm, await self._execute(job, warm), applied
        except InstallFailed as e:
            return warm, failure(Reason.INSTALL_FAILED, str(e)), applied
        except TurnFailed as e:
            return warm, failure(e.reason, str(e)), applied
        except Exception as e:
            log.exception("turn %s failed on this node", job["turn_id"])
            return warm, failure(Reason.SANDBOX_FAILED, f"{type(e).__name__}: {e}"), applied

    @staticmethod
    def _env(job: dict[str, Any]) -> dict[str, str]:
        provider = job["provider"] or {}
        return {**job["env"], "HG_CONTRACT": "1", "HG_REQUEST": "/run/hg/request.json", "HG_FILES": "/run/hg/files",
                "HG_MCP": "/run/hg/mcp.json", "HG_WORKSPACE": state.BOX_WORKSPACE, "HG_STATE": state.BOX_STATE,
                "HOME": state.BOX_HOME, "HG_PACKAGE_DIR": "/opt/hg/package", "HG_TOOLS": "/opt/hg/tools",
                "HG_SESSION": job["session_id"], "HG_RESPONSE": job["turn_id"], "HG_TURN": str(job["seq"]),
                "HG_MODEL": job["model"] or "", "HG_PROVIDER_API": provider.get("api", ""),
                "HG_PROVIDER_URL": "http://hg-door:8080" if provider else "", "HG_PROVIDER_TOKEN": provider.get("token", "")}

    async def _execute(self, job: dict[str, Any], warm: Warm) -> dict[str, Any]:
        rid, running = job["turn_id"], self.running[job["turn_id"]]
        grace = job["package"]["manifest"]["run"]["stop_grace_s"]
        logs = job.get("logs")
        out_log = self.dir / "logs" / f"{rid}.stdout" if logs else None
        err_log = self.dir / "logs" / f"{rid}.stderr" if logs else None
        proc = await warm.box.exec(["bash", TURN_SH], self._env(job))
        pump = Pump(self.client, rid, running.stop_for, out_log)
        err_tail = bytearray()
        err_bytes = 0

        async def read_stderr() -> None:
            nonlocal err_bytes
            with contextlib.ExitStack() as stack:
                f = stack.enter_context(err_log.open("wb")) if err_log else None
                async for chunk in proc.stderr():
                    err_bytes += len(chunk)
                    if f:
                        f.write(chunk)
                    err_tail.extend(chunk)
                    del err_tail[:-LOG_TAIL]

        async def linger() -> None:
            await pump.concluded.wait()
            await asyncio.sleep(grace)

        reading: asyncio.Future[Any] = asyncio.ensure_future(asyncio.gather(pump.run(proc), read_stderr()))
        stopper, lingering = asyncio.create_task(running.stop.wait()), asyncio.create_task(linger())
        outcome, broken = "exited", None
        try:
            done, _ = await asyncio.wait({reading, stopper, lingering}, timeout=job["timeout_s"],
                                         return_when=asyncio.FIRST_COMPLETED)
            if reading in done:
                broken = reading.exception()
            if reading not in done or broken:
                outcome = "timed_out" if not done else "stopped"
                await self._stop(warm, proc, grace)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(reading), EXIT_WAIT_S)
            code = await asyncio.wait_for(proc.wait(), EXIT_WAIT_S)
        finally:
            for t in (stopper, lingering, reading):
                t.cancel()
            proc.kill()
        report: dict[str, Any] = {"outcome": outcome, "exit_code": code, "stderr_tail": err_tail.decode("utf-8", "replace"),
                                  "stdout_bytes": pump.total, "stderr_bytes": err_bytes}
        log.info("turn %s %s with exit code %s after %d output batches", rid, outcome, code, pump.batch)
        if isinstance(broken, LineTooLarge):
            report.update(failure(Reason.INVALID_OUTPUT, str(broken)))
        elif broken is not None:
            report.update(failure(Reason.SANDBOX_FAILED, f"delivering the output failed: {broken}"))
        if logs and out_log and err_log:
            uploaded = True
            for name, path in (("stdout", out_log), ("stderr", err_log)):
                try:
                    await self.client.upload(logs[name], path)
                except Exception:
                    uploaded = False
                    log.exception("uploading the %s of %s failed", name, rid)
                path.unlink(missing_ok=True)
            report["logs"] = uploaded
        return report

    async def _stop(self, warm: Warm, proc: Process, grace: int) -> None:
        """SIGTERM to the turn's process group; after it exits or the grace runs out, SIGKILL to every process."""
        await warm.box.terminate(PIDFILE)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), grace)
        await warm.box.kill_all()
        proc.kill()

    # ── saving the session's state ─────────────────────────────────────────────────────────────────────────────────
    async def _archive(self, job: dict[str, Any], warm: Warm, links: dict[str, str] | None) -> Save | None:
        if not links:
            return None
        save = Save(job["turn_id"], job["session_id"], links)
        try:
            paths = self._included(job)
            current = await state.listing(warm.box, paths)
            save.fingerprint = current.fingerprint
            if current.fingerprint != warm.fingerprint:
                save.archive = self.dir / "saves" / f"{job['session_id']}-{job['seq']}.tgz"
                save.size = await state.archive(warm.box, paths, save.archive)
                save.files = current.workspace_files()
        except Exception as e:
            log.exception("archiving the state of %s failed", job["session_id"])
            save.error = f"{type(e).__name__}: {e}"
        return save

    async def _upload(self, save: Save, warm: Warm | None) -> None:
        report: dict[str, Any] = {}
        try:
            if save.archive is not None and not save.error:
                await self.client.upload(save.links["put"], save.archive)
                await self.client.upload(save.links["files_put"], json.dumps(save.files).encode())
                report["snapshot"] = {"key": save.links["key"], "files_key": save.links["files_key"], "bytes": save.size}
            elif save.error:
                report["error"] = save.error
            if warm is not None and not save.error:
                warm.fingerprint = save.fingerprint
        except Exception as e:
            log.exception("uploading the state of %s failed", save.session_id)
            report = {"error": f"{type(e).__name__}: {e}"}
        finally:
            if save.archive is not None:
                save.archive.unlink(missing_ok=True)
        try:
            await self.client.saved(save.rid, report)
        except Exception:
            log.exception("reporting the save of %s failed", save.session_id)
        finally:
            await self._saved(save.session_id)

    async def _saved(self, sid: str) -> None:
        self.saving[sid] -= 1
        if self.saving[sid] <= 0:
            del self.saving[sid]
        async with self.saves_done:
            self.saves_done.notify_all()
