"""The node agent: joins, then runs four loops (heartbeat, claim, meter, evict idle) and one task per turn.

A turn on this node:
  1. the package (verified against its digest) and its tools (install.sh, once per digest, in an install box);
  2. the session's box: warm from its last turn, or new (seeded from the package) or restored from its snapshot;
  3. the turn's inputs written read-only into /run/hg (request, settings, input files); staged container files applied;
  4. turn.sh runs request.sh; stdout is streamed to the control plane in numbered batches, stderr kept;
  5. on exit, timeout or stop: the workspace checkpointed (when the session is stored), logs uploaded when the
     organization has observability, and the outcome reported. The box stays warm until its idle time runs out.
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
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from ..config import Settings
from ..domain import package as pkg
from ..domain.pricing import Meter
from ..domain.turn import Reason
from ..limits import STDERR_TAIL, TURN_STDOUT_MAX
from .client import ControlClient
from .docker import DockerError, docker, docker_file
from .sandbox import UID, Box, BoxSpec, Mount, empty_dir, fresh_dir, sweep

log = logging.getLogger("harnessgateway.node")
BATCH_EVERY_S = 0.2
BATCH_LINES = 200
HEARTBEAT_S = 3
METER_S = 30
PIDFILE = "/tmp/hg-turn.pid"
MANIFEST_MAX = 20_000


class InstallFailed(Exception):
    pass


class TurnFailed(Exception):
    def __init__(self, reason: Reason, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass
class Warm:
    """A session's box on this node."""
    box: Box
    session_id: str
    org_id: str
    digest: str
    vcpus: float
    memory_mb: int
    idle_ttl_s: int
    busy: bool = False
    idle_since: float = field(default_factory=time.monotonic)
    metered_at: float = field(default_factory=time.monotonic)
    counted: dict[str, int] = field(default_factory=lambda: {"out": 0, "in": 0})


@dataclass
class Running:
    job: dict[str, Any]
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    reason: Reason | None = None


class Node:
    def __init__(self, settings: Settings):
        self.s = settings
        self.sandbox_id = ""                              # the sandbox image's id, read at start
        self.dir = settings.node_dir
        self.client = ControlClient(settings.api_url, self.dir / "node.json")
        self.sessions: dict[str, Warm] = {}
        self.running: dict[str, Running] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.pending_usage: list[dict[str, Any]] = []

    def lock(self, key: str) -> asyncio.Lock:
        return self.locks.setdefault(key, asyncio.Lock())

    # ── lifecycle ──────────────────────────────────────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        for sub in ("sessions", "packages", "tools", "runtime"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        runtime = self.dir / "runtime" / "turn.sh"
        runtime.write_bytes(resources.files("harnessgateway.node").joinpath("runtime/turn.sh").read_bytes())
        runtime.chmod(0o755)
        node_id = await self.client.join(self.s.node_join_token, self.s.node_name, self.s.node_slots)
        removed = await sweep(node_id)
        shutil.rmtree(self.dir / "sessions", ignore_errors=True)
        (self.dir / "sessions").mkdir()
        await self._images()
        log.info("node %s ready: %d slots, runtime %s (removed %d leftover containers)", node_id, self.s.node_slots,
                 self.s.runtime, removed)
        await asyncio.gather(self._heartbeats(), self._claims(), self._meter(), self._evict_idle())

    async def _images(self) -> None:
        for image in (self.s.sandbox_image, self.s.platform_image):
            if (await docker("image", "inspect", image, timeout=60)).code != 0:
                log.info("pulling %s", image)
                (await docker("pull", image, timeout=1800)).check(f"pull {image}")
        self.sandbox_id = (await docker("image", "inspect", "-f", "{{.Id}}", self.s.sandbox_image,
                                        timeout=60)).check("sandbox image").out.decode().strip()

    async def _heartbeats(self) -> None:
        while True:
            try:
                reply = await self.client.heartbeat(list(self.running), list(self.sessions), self.s.node_slots)
                for rid in reply.get("cancel", []):
                    if rid in self.running and not self.running[rid].stop.is_set():
                        self.running[rid].reason = self.running[rid].reason or Reason.CANCELLED
                        self.running[rid].stop.set()
                for sid in reply.get("evict", []):
                    warm = self.sessions.get(sid)
                    if warm and not warm.busy:
                        await self._close(warm)
            except Exception as e:
                log.warning("heartbeat failed: %s", e)
            await asyncio.sleep(HEARTBEAT_S)

    async def _claims(self) -> None:
        while True:
            if len(self.running) >= self.s.node_slots:
                await asyncio.sleep(0.5)
                continue
            try:
                job = await self.client.claim()
            except Exception as e:
                log.warning("claim failed: %s", e)
                job = None
            if job is None:
                await asyncio.sleep(1)
                continue
            self.running[job["turn_id"]] = Running(job)
            asyncio.create_task(self._turn(job))

    async def _evict_idle(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            for warm in list(self.sessions.values()):
                if not warm.busy and now - warm.idle_since >= warm.idle_ttl_s:
                    await self._close(warm)

    async def _meter(self) -> None:
        while True:
            await asyncio.sleep(METER_S)
            for warm in list(self.sessions.values()):
                self._measure(warm)
            await self._send_usage()

    def _measure(self, warm: Warm) -> None:
        now = time.monotonic()
        dt = now - warm.metered_at
        warm.metered_at = now
        counters = warm.box.counters()
        out, inn = counters["out"] - warm.counted["out"], counters["in"] - warm.counted["in"]
        warm.counted = counters
        base = {"org_id": warm.org_id, "session_id": warm.session_id}
        self.pending_usage += [{**base, "meter": Meter.VCPU_SECONDS.value, "quantity": round(warm.vcpus * dt, 3)},
                               {**base, "meter": Meter.MEMORY_GB_SECONDS.value,
                                "quantity": round(warm.memory_mb / 1024 * dt, 3)},
                               {**base, "meter": Meter.EGRESS_BYTES.value, "quantity": max(out, 0)},
                               {**base, "meter": Meter.INGRESS_BYTES.value, "quantity": max(inn, 0)}]

    async def _send_usage(self) -> None:
        rows, self.pending_usage = self.pending_usage, []
        try:
            await self.client.usage(rows)
        except Exception:
            log.exception("reporting usage failed; keeping it for the next report")
            self.pending_usage = rows + self.pending_usage

    async def _make_room(self) -> None:
        """Keep at most HG_NODE_WARM_MAX boxes: each holds a network and memory, so the longest idle goes first."""
        while len(self.sessions) >= self.s.node_warm_max:
            idle = [w for w in self.sessions.values() if not w.busy]
            if not idle:
                return
            await self._close(min(idle, key=lambda w: w.idle_since))

    async def _close(self, warm: Warm) -> None:
        if self.sessions.get(warm.session_id) is not warm:
            return
        self._measure(warm)
        del self.sessions[warm.session_id]
        await warm.box.destroy()
        shutil.rmtree(self.dir / "sessions" / warm.session_id, ignore_errors=True)

    # ── packages and tools ─────────────────────────────────────────────────────────────────────────────────────────
    async def _package(self, spec: dict[str, Any]) -> Path:
        digest = spec["digest"]
        folder = self.dir / "packages" / digest
        async with self.lock(f"pkg:{digest}"):
            if folder.is_dir():
                return folder
            with tempfile.TemporaryDirectory(dir=self.dir / "packages") as tmp:
                data = await self.client.fetch(spec["url"])
                staging = Path(tmp) / "p"
                await asyncio.to_thread(pkg.unpack, data, staging)
                if await asyncio.to_thread(pkg.digest, staging) != digest:
                    raise TurnFailed(Reason.SANDBOX_FAILED, "the package archive does not match its digest")
                for p in [staging, *staging.rglob("*")]:
                    p.chmod(0o755 if p.is_dir() or p.stat().st_mode & 0o111 else 0o644)
                staging.rename(folder)
        return folder

    @staticmethod
    def tools_key(package_dir: Path, manifest: dict[str, Any], sandbox_id: str) -> str:
        """The tools layer depends on install.sh, the install section (install.sh is self-contained) and the sandbox
        image it was built in (its runtimes and libc), so package versions that change only request.sh or seeds share
        one install, and a new sandbox image installs afresh."""
        h = hashlib.sha256(sandbox_id.encode())
        h.update((package_dir / "install.sh").read_bytes())
        h.update(json.dumps(manifest["install"], sort_keys=True).encode())
        return h.hexdigest()

    async def _tools(self, job: dict[str, Any], package_dir: Path) -> Path:
        digest = self.tools_key(package_dir, job["package"]["manifest"], self.sandbox_id)
        tools = self.dir / "tools" / digest
        async with self.lock(f"tools:{digest}"):
            if tools.is_dir():
                return tools
            manifest = job["package"]["manifest"]
            staging = fresh_dir(self.dir / "tools" / f".{digest}.partial", owner=UID)
            limits = job["limits"]
            box = Box(BoxSpec(name=f"i{digest[:20]}{os.urandom(3).hex()}", node_id=self.client.node_id, org_id=job["org_id"],
                              vcpus=limits["vcpus"], memory_mb=limits["memory_mb"], pids=limits["pids"],
                              egress=manifest["install"]["egress"],
                              mounts=[Mount(package_dir, "/opt/hg/package"), Mount(staging, "/opt/hg/tools", readonly=False)],
                              env={"HOME": "/tmp/home", "HG_TOOLS": "/opt/hg/tools", "HG_PACKAGE_DIR": "/opt/hg/package",
                                   "HG_CONTRACT": "1"}, tmp_mb=4096),
                      self.s, self.dir / "tools" / f".{digest}.door")
            started = time.monotonic()
            try:
                await box.start()
                ex = await box.exec(["bash", "-c", "mkdir -p \"$HOME\" && cd /tmp && bash /opt/hg/package/install.sh"],
                                    {"HG_TOOLS": "/opt/hg/tools"})
                tail = bytearray()

                async def drain(stream) -> None:
                    async for chunk in stream:
                        tail.extend(chunk)
                        del tail[:-STDERR_TAIL]
                try:
                    await asyncio.wait_for(asyncio.gather(drain(ex.lines()), drain(ex.stderr())),
                                           manifest["install"]["timeout_s"])
                    code = await ex.wait()
                except TimeoutError:
                    ex.kill()
                    raise InstallFailed(f"install.sh ran past {manifest['install']['timeout_s']}s") from None
                if code != 0:
                    raise InstallFailed(f"install.sh exited {code}: {tail.decode(errors='replace')[-2000:]}")
                staging.rename(tools)
            finally:
                dt = time.monotonic() - started
                counters = box.counters()
                base = {"org_id": job["org_id"], "turn_id": job["turn_id"]}
                self.pending_usage += [{**base, "meter": Meter.VCPU_SECONDS.value, "quantity": round(limits["vcpus"] * dt, 3)},
                                       {**base, "meter": Meter.MEMORY_GB_SECONDS.value,
                                        "quantity": round(limits["memory_mb"] / 1024 * dt, 3)},
                                       {**base, "meter": Meter.INGRESS_BYTES.value, "quantity": counters["in"]},
                                       {**base, "meter": Meter.EGRESS_BYTES.value, "quantity": counters["out"]}]
                await box.destroy()
                shutil.rmtree(staging, ignore_errors=True)
                shutil.rmtree(self.dir / "tools" / f".{digest}.door", ignore_errors=True)
        return tools

    # ── a session's box ────────────────────────────────────────────────────────────────────────────────────────────
    async def _session(self, job: dict[str, Any], package_dir: Path, tools: Path) -> Warm:
        sid = job["session_id"]
        warm = self.sessions.get(sid)
        if warm and warm.digest == job["package"]["digest"] and await warm.box.alive():
            return warm
        if warm:
            await self._close(warm)
        await self._make_room()
        root = fresh_dir(self.dir / "sessions" / sid)
        dirs = {name: fresh_dir(root / name, owner=UID, mode=0o700) for name in ("workspace", "home", "state")}
        fresh_dir(root / "run")
        limits = job["limits"]
        box = Box(BoxSpec(name=sid.split("_", 1)[1], node_id=self.client.node_id, org_id=job["org_id"],
                          vcpus=limits["vcpus"], memory_mb=limits["memory_mb"], pids=limits["pids"],
                          egress=job["package"]["manifest"]["run"]["egress"],
                          mounts=[Mount(dirs["workspace"], "/workspace", False), Mount(dirs["home"], "/home/hg", False),
                                  Mount(dirs["state"], "/state", False), Mount(root / "run", "/run/hg"),
                                  Mount(package_dir, "/opt/hg/package"), Mount(tools, "/opt/hg/tools"),
                                  Mount(self.dir / "runtime", "/opt/hg/runtime")]),
                  self.s, root / "door")
        await box.start()
        warm = Warm(box, sid, job["org_id"], job["package"]["digest"], limits["vcpus"], limits["memory_mb"],
                    limits["idle_ttl_s"])
        self.sessions[sid] = warm
        if job["snapshot"]["get"]:
            snap = root / "restore.tgz"
            await self.client.download(job["snapshot"]["get"], snap)
            (await docker_file("exec", "-i", "-u", f"{UID}:{UID}", box.container, "tar", "xzf", "-", "-C", "/",
                               "--no-same-owner", "--no-overwrite-dir", stdin_path=snap)).check("restore the workspace")
            snap.unlink()
        else:
            await box.sh('for d in workspace home; do src=/opt/hg/package/seeds/$d; dst=/workspace; '
                         '[ "$d" = home ] && dst=/home/hg; [ -d "$src" ] && cp -R "$src/." "$dst/"; done; true')
        return warm

    async def _inputs(self, job: dict[str, Any], warm: Warm) -> None:
        """The turn's inputs into the read-only /run/hg, written by the node into a directory only it writes."""
        run = empty_dir(self.dir / "sessions" / job["session_id"] / "run")
        (run / "request.json").write_bytes(await self.client.fetch(job["request_url"]))
        settings = run / "settings.json"
        settings.write_text(json.dumps(job["settings"]))
        os.chown(settings, 0, UID)
        settings.chmod(0o640)
        files = empty_dir(run / "files")
        index = {}
        for f in job["files"]:
            dest = files / f["id"] / os.path.basename(f["filename"])
            await self.client.download(f["url"], dest)
            dest.parent.chmod(0o755)
            index[f["id"]] = {"filename": f["filename"], "path": f"/run/hg/files/{f['id']}/{dest.name}",
                              "bytes": dest.stat().st_size}
        (files / "index.json").write_text(json.dumps(index))
        for op in job["ops"]:
            if op["op"] == "put":
                (run / "ops").mkdir(exist_ok=True)
                staged = run / "ops" / op["id"]
                await self.client.download(op["url"], staged)
                await warm.box.sh('mkdir -p "$(dirname "/workspace/$1")" && cp "$2" "/workspace/$1"', op["path"],
                                  f"/run/hg/ops/{op['id']}")
            else:
                await warm.box.sh('rm -rf -- "/workspace/$1"', op["path"])

    # ── one turn ───────────────────────────────────────────────────────────────────────────────────────────────────
    async def _turn(self, job: dict[str, Any]) -> None:
        rid = job["turn_id"]
        report: dict[str, Any] = {"outcome": "failed", "reason": Reason.SANDBOX_FAILED.value, "error": "the turn could not run"}
        warm: Warm | None = None
        try:
            package_dir = await self._package(job["package"])
            tools = await self._tools(job, package_dir)
            async with self.lock(f"session:{job['session_id']}"):
                warm = await self._session(job, package_dir, tools)
                warm.busy = True
                await self._inputs(job, warm)
                report = await self._execute(job, warm)
                if job["store"] and report["outcome"] != "failed":
                    report["snapshot"] = await self._checkpoint(job, warm)
                report["applied_ops"] = [op["id"] for op in job["ops"]]
        except InstallFailed as e:
            report = {"outcome": "failed", "reason": Reason.INSTALL_FAILED.value, "error": str(e)[:4000]}
        except TurnFailed as e:
            report = {**report, "outcome": "failed", "reason": e.reason.value, "error": str(e)[:4000]}
        except Exception as e:
            log.exception("turn %s failed on this node", rid)
            report = {"outcome": "failed", "reason": Reason.SANDBOX_FAILED.value, "error": f"{type(e).__name__}: {e}"[:4000]}
        finally:
            await self.client.finish(rid, report)
            self.running.pop(rid, None)
            if warm is not None:
                warm.busy, warm.idle_since = False, time.monotonic()
                if not job["store"] or warm.idle_ttl_s == 0 or report.get("reason") == Reason.SANDBOX_FAILED.value:
                    await self._close(warm)
            await self._send_usage()

    def _env(self, job: dict[str, Any]) -> dict[str, str]:
        provider = job["provider"] or {}
        return {"HG_CONTRACT": "1", "HG_REQUEST": "/run/hg/request.json", "HG_SETTINGS": "/run/hg/settings.json",
                "HG_FILES": "/run/hg/files", "HG_WORKSPACE": "/workspace", "HG_STATE": "/state", "HOME": "/home/hg",
                "HG_PACKAGE_DIR": "/opt/hg/package", "HG_TOOLS": "/opt/hg/tools", "HG_SESSION": job["session_id"],
                "HG_RESPONSE": job["turn_id"], "HG_TURN": str(job["seq"]), "HG_MODEL": job["model"] or "",
                "HG_PROVIDER_API": provider.get("api", ""), "HG_PROVIDER_URL": "http://hg-egress:8080" if provider else "",
                "HG_PROVIDER_TOKEN": provider.get("token", "")}

    async def _execute(self, job: dict[str, Any], warm: Warm) -> dict[str, Any]:
        rid, state = job["turn_id"], self.running[job["turn_id"]]
        logs = self.dir / "sessions" / job["session_id"] / "logs"
        logs.mkdir(exist_ok=True)
        out_path, err_path = logs / f"{rid}.stdout", logs / f"{rid}.stderr"
        ex = await warm.box.exec(["bash", "/opt/hg/runtime/turn.sh"], self._env(job))
        pending: list[str] = []
        batch, total = 0, 0
        err_tail = bytearray()

        async def flush() -> None:
            nonlocal pending, batch
            if pending:
                lines, pending = pending, []
                reply = await self.client.events(rid, batch, lines)
                batch += 1
                if reply.get("stop") and not state.stop.is_set():
                    state.reason = Reason.INVALID_OUTPUT
                    state.stop.set()

        async def read_stdout() -> None:
            nonlocal total
            last = time.monotonic()
            with out_path.open("wb") as f:
                async for raw in ex.lines():
                    total += len(raw)
                    if total > TURN_STDOUT_MAX:
                        state.reason = Reason.OUTPUT_TOO_LARGE
                        state.stop.set()
                        break
                    f.write(raw)
                    pending.append(raw.decode("utf-8", "replace").rstrip("\n"))
                    if len(pending) >= BATCH_LINES or time.monotonic() - last >= BATCH_EVERY_S:
                        await flush()
                        last = time.monotonic()
            await flush()

        async def read_stderr() -> None:
            with err_path.open("wb") as f:
                async for chunk in ex.stderr():
                    f.write(chunk)
                    err_tail.extend(chunk)
                    del err_tail[:-STDERR_TAIL]

        async def ticker() -> None:                     # flush a quiet stream's last lines promptly
            while True:
                await asyncio.sleep(BATCH_EVERY_S)
                await flush()

        readers = asyncio.gather(read_stdout(), read_stderr())
        tick = asyncio.create_task(ticker())
        stopper = asyncio.create_task(state.stop.wait())
        outcome = "exited"
        try:
            done, _ = await asyncio.wait({readers, stopper}, timeout=job["timeout_s"], return_when=asyncio.FIRST_COMPLETED)
            if readers not in done:
                outcome = "stopped" if stopper in done else "timed_out"
                await self._stop(warm, ex, job["package"]["manifest"]["run"]["stop_grace_s"])
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(readers, 30)
            code = await asyncio.wait_for(ex.wait(), 30)
        finally:
            tick.cancel()
            stopper.cancel()
            ex.kill()
        await flush()
        report: dict[str, Any] = {"outcome": outcome, "exit_code": code, "stderr_tail": err_tail.decode("utf-8", "replace")}
        if state.reason == Reason.OUTPUT_TOO_LARGE:
            report.update(outcome="failed", reason=Reason.OUTPUT_TOO_LARGE.value,
                          error=f"the turn printed more than {TURN_STDOUT_MAX // 2**20} MiB")
        if job.get("logs"):
            await self.client.upload(job["logs"]["stdout"], out_path)
            await self.client.upload(job["logs"]["stderr"], err_path)
        out_path.unlink(missing_ok=True)
        err_path.unlink(missing_ok=True)
        return report

    async def _stop(self, warm: Warm, ex, grace: int) -> None:
        await warm.box.terminate(PIDFILE)
        try:
            await asyncio.wait_for(ex.wait(), grace)
        except TimeoutError:
            await warm.box.kill_all()
            ex.kill()

    async def _checkpoint(self, job: dict[str, Any], warm: Warm) -> dict[str, Any]:
        root = self.dir / "sessions" / job["session_id"]
        snap = root / "snapshot.tgz"
        result = await docker_file("exec", "-u", f"{UID}:{UID}", warm.box.container, "tar", "czf", "-", "-C", "/",
                                   "--warning=no-file-changed", "workspace", "home/hg", "state", stdout_path=snap,
                                   timeout=900)
        if result.code not in (0, 1):
            raise DockerError(f"checkpoint: {result.err.decode(errors='replace')[-300:]}")
        size = snap.stat().st_size
        if size > job["limits"]["workspace_mb"] * 1024 * 1024:
            snap.unlink()
            raise TurnFailed(Reason.WORKSPACE_TOO_LARGE,
                             f"the session's files are over the organization's limit of {job['limits']['workspace_mb']} MB")
        listing = await warm.box.sh("find /workspace -xdev -type f -printf '%P\\t%s\\t%T@\\n' 2>/dev/null | head -n $1",
                                    str(MANIFEST_MAX), timeout=120)
        manifest = []
        for line in listing.decode("utf-8", "replace").splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0]:
                manifest.append({"path": parts[0], "bytes": int(parts[1]), "mtime": int(float(parts[2]))})
        await self.client.upload(job["snapshot"]["put"], snap)
        await self.client.upload(job["snapshot"]["manifest_put"], json.dumps(manifest).encode())
        snap.unlink()
        return {"key": job["snapshot"]["key"], "bytes": size, "manifest_key": job["snapshot"]["manifest_key"]}
