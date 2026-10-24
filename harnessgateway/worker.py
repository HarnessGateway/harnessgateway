"""Layer 5: the worker. It joins the control plane with a token, then pulls: claim a turn, fetch the
package, the request and the session's snapshots through the URLs the turn came with, run the turn on
this node, stream its output back, push the snapshots, report how it ended. It holds a worker
credential and nothing else: no database, no bucket, no key, no cloud identity. A worker that dies
loses only its turns in flight.

    <cache>/worker.json                 its credential, from the join
    <cache>/packages/<sha>/             packages, unpacked once per commit
    <cache>/tools/<sha>-<image>/        tools layers, built once per package commit
    <cache>/sessions/<sid>/{workspace,state,run}/   a session's trees, restored from its snapshot when stale
    <cache>/homes/<org>/<member>/<harness>/        a member's home for a package
"""
from __future__ import annotations

import asyncio
import io
import json
import shutil
import tarfile
import time
from pathlib import Path
from typing import Any, Protocol, Sequence

import httpx

from . import files, harness
from .errors import Invalid
from .execution import Execution, Outcome, Spec
from .limits import Limits
from .runtimes.docker import TURN_SCRIPT
from .sandboxes import InstallFailed, path_with

CHUNK_EVERY_S = 0.25
HEARTBEAT_S = 5
CLAIM_IDLE_S = 1


class NodeLike(Protocol):
    async def prepare(self) -> str: ...
    async def recover(self) -> None: ...
    def runtime_for(self, org: str, limits: Limits, *, read_only: Sequence[Path] = (), writable: Sequence[Path] = (),
                    egress: Sequence[str] = (), gateway_url: str = "", token: str = ""): ...
    async def tools_for(self, org: str, limits: Limits, folder: Path, config: harness.Config, root: Path, image_id: str) -> Path: ...


def turn_env(job: dict[str, Any], config: harness.Config, *, workspace: Path, home: Path, rundir: Path, state: Path,
             folder: Path, tools: Path, provider_url: str, token_path: str) -> dict[str, str]:
    """The environment a turn runs with: where things are, what the turn is, how to reach the provider."""
    env = {"HG_WORKSPACE": str(workspace), "HOME": str(home), "HG_DIR": str(rundir), "HG_STATE": str(state),
           "HG_FILES": str(rundir / "files"), "HG_REQUEST": str(rundir / "request.json"),
           "HG_SESSION": job["session"], "HG_RESPONSE": job["id"], "HG_TURN": str(job["turn"]), "HG_PREVIOUS": job.get("previous", ""),
           "HG_MODEL": job["model"], "HG_HARNESS_DIR": str(folder), "HG_TOOLS": str(tools), "HG_INTERRUPT": config.interrupt,
           "PATH": path_with(tools), "HG_PROVIDER_URL": provider_url, "HG_PROVIDER_API": job["provider"]["api"],
           "HG_PROVIDER_KEY_FILE": token_path, "HG_CONTRACT": str(harness.CONTRACT)}
    for name, value in ((config.key_env, job["token"]), (config.url_env, provider_url)):
        if name:
            env[name] = value
    return env


class Worker:
    def __init__(self, control_url: str, node: NodeLike, cache: Path, *, name: str, slots: int, version: str,
                 join_token: str = "", ca_file: Path | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 gateway_url: str = ""):
        self.control_url, self.node, self.cache = control_url.rstrip("/"), node, Path(cache)
        self.name, self.slots, self.version, self.join_token = name, slots, version, join_token
        self.gateway_url = gateway_url                # this node's own, when set; else what the node finds; else the job's
        self.cache.mkdir(parents=True, exist_ok=True)
        verify = str(ca_file) if ca_file else True
        self._http = httpx.AsyncClient(base_url=self.control_url, timeout=60, verify=verify, transport=transport)
        self._blob_http = httpx.AsyncClient(timeout=600, verify=verify, transport=transport)
        self._turns: dict[str, asyncio.Task] = {}
        self._seen: dict[str, int] = {}              # interrupts delivered, per turn
        self._executions: dict[str, Execution] = {}
        self._stop_timeouts: dict[str, float] = {}
        self._interrupts: dict[str, str] = {}
        self._sessions: dict[str, str] = {}
        self._image_id = ""
        self.stopping = False

    # joining
    async def join(self, patience_s: float = 120) -> None:
        """Present the saved credential, or join with the token; the control plane may still be starting."""
        saved = self.cache / "worker.json"
        deadline = time.monotonic() + patience_s
        while True:
            try:
                if saved.is_file():
                    self._http.headers["Authorization"] = f"Bearer {json.loads(saved.read_text())['secret']}"
                    r = await self._http.post("/internal/heartbeat", json={"running": []})
                    if r.status_code == 200:
                        return
                if not self.join_token:
                    raise RuntimeError("no saved credential and no join token: give the worker HG_JOIN_TOKEN")
                r = await self._http.post("/internal/join", json={"name": self.name, "slots": self.slots, "version": self.version},
                                          headers={"Authorization": f"Bearer {self.join_token}"})
                r.raise_for_status()
                break
            except (httpx.HTTPError, OSError) as e:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"could not reach the control plane at {self.control_url}: {e}") from None
                await asyncio.sleep(2)
        secret = r.json()["secret"]
        saved.write_text(json.dumps({"secret": secret, "id": r.json()["id"]}))
        saved.chmod(0o600)
        self._http.headers["Authorization"] = f"Bearer {secret}"

    # the loop
    async def run(self) -> None:
        await self.join()
        await self.node.recover()
        self._image_id = await self.node.prepare()
        self.gateway_url = self.gateway_url or getattr(self.node, "gateway_url", "")
        beat = asyncio.create_task(self._heartbeats())
        try:
            while not self.stopping:
                if len(self._turns) < self.slots:
                    r = await self._http.post("/internal/claim", json={})
                    if r.status_code == 200:
                        job = r.json()
                        self._turns[job["id"]] = asyncio.create_task(self._turn(job))
                        self._turns[job["id"]].add_done_callback(lambda t, rid=job["id"]: self._turns.pop(rid, None))
                        continue
                await asyncio.sleep(CLAIM_IDLE_S)
        finally:
            beat.cancel()
            for t in list(self._turns.values()):
                t.cancel()
            await asyncio.gather(*self._turns.values(), return_exceptions=True)

    async def _heartbeats(self) -> None:
        while True:
            try:
                r = await self._http.post("/internal/heartbeat", json={"running": list(self._turns)})
                if r.status_code == 200:
                    for rid, command in r.json().get("commands", {}).items():
                        await self._obey(rid, command)
            except (httpx.HTTPError, OSError):
                pass
            await asyncio.sleep(HEARTBEAT_S)

    async def _obey(self, rid: str, command: dict[str, Any]) -> None:
        ex = self._executions.get(rid)
        if ex is None:
            return
        if command.get("cancel"):
            ex.stop(self._stop_timeouts.get(rid, 20))
        for steer in command.get("interrupts", []):
            if steer["seq"] > self._seen.get(rid, 0):
                self._seen[rid] = steer["seq"]
                rundir = self.cache / "sessions" / self._sessions[rid] / "run"
                try:
                    files.write(rundir, "interrupt", steer["text"].encode())
                except Invalid:
                    continue
                await ex.interrupt(self._interrupts[rid])

    # one turn
    async def _turn(self, job: dict[str, Any]) -> None:
        rid, report = job["id"], {"outcome": "failed", "error": "the turn could not run"}
        began = time.monotonic()
        try:
            report = await self._run(job)
        except asyncio.CancelledError:
            report = {"outcome": "killed"}
            raise
        except InstallFailed as e:
            report = {"outcome": "failed", "error": str(e)}
        except Exception as e:
            report = {"outcome": "failed", "error": f"{type(e).__name__}: {e}"[:2000]}
        finally:
            report["seconds"] = round(time.monotonic() - began, 1)
            self._executions.pop(rid, None)
            for d in (self._stop_timeouts, self._interrupts, self._sessions, self._seen):
                d.pop(rid, None)
            for attempt in range(5):
                try:
                    r = await self._http.post(f"/internal/turns/{rid}/result", json=report)
                    if r.status_code < 500:
                        break
                except (httpx.HTTPError, OSError):
                    pass
                await asyncio.sleep(2 ** attempt)

    async def _run(self, job: dict[str, Any]) -> dict[str, Any]:
        rid, sid, org = job["id"], job["session"], job["org"]
        config = harness.load_config_dict(job["config"])
        limits = Limits.from_dict(job["limits"])
        urls = job["urls"]
        folder = await self._package(job["version"], urls["folder"])
        tools = await self.node.tools_for(org, limits, folder, config, self.cache / "tools", self._image_id)
        session_dir = self.cache / "sessions" / sid
        workspace, state, rundir = session_dir / "workspace", session_dir / "state", session_dir / "run"
        home = self.cache / "homes" / org / job["member"] / job["harness"]
        marker = session_dir / ".snapshot"
        fetched = {"snapshot": b"", "home": b"", "files": b"", "request": await self._get(urls["request"])}
        if urls["snapshot"] and (not marker.is_file() or marker.read_text() != job["snapshot"]):
            fetched["snapshot"] = await self._get(urls["snapshot"])
        if urls["home"] and not home.is_dir():
            fetched["home"] = await self._get(urls["home"])
        if urls["files"]:
            fetched["files"] = await self._get(urls["files"])
        await asyncio.to_thread(self._materialize, job, folder, session_dir, home, fetched)
        await self._http.post(f"/internal/turns/{rid}/started", json={})
        self._stop_timeouts[rid], self._interrupts[rid], self._sessions[rid] = config.stop_timeout_s, config.interrupt, sid
        runtime = self.node.runtime_for(org, limits, read_only=(folder, tools), writable=(home, rundir, state),
                                        egress=config.run_egress, gateway_url=self.gateway_url or job["gateway_url"], token=job["token"])
        base = getattr(runtime, "provider_base", "")   # a node without a door names the gateway itself
        env = turn_env(job, config, workspace=workspace, home=home, rundir=rundir, state=state, folder=folder, tools=tools,
                       provider_url=(base + job["provider"]["path"]) if base else job["provider"]["url"], token_path=str(runtime.token_path or ""))
        spec = Spec(("bash", str(TURN_SCRIPT)), input=fetched["request"], env=env, workspace=workspace, timeout_s=job["timeout_s"])
        execution = await runtime.start(spec)
        self._executions[rid] = execution
        door_log = io.BytesIO()
        outcome = await self._stream(rid, execution, door_log)
        report = self._report(outcome, rundir, limits, shaped=(folder / "response.sh").is_file())
        if door_log.getvalue():
            await self._put(urls["door_log_put"], door_log.getvalue())
        if outcome.kind != "failed":
            report["snapshot"] = await self._snapshots(job, session_dir, home, limits)
        return report

    async def _package(self, sha: str, url: str) -> Path:
        folder = self.cache / "packages" / sha
        if not (folder / "harness.json").is_file():
            data = await self._get(url)
            tmp = folder.with_name(f".{sha}.tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            harness.unpack(data, tmp)
            harness.validate(tmp)
            tmp.replace(folder)
        return folder

    @staticmethod
    def _materialize(job: dict[str, Any], folder: Path, session_dir: Path, home: Path, fetched: dict[str, bytes]) -> None:
        """The session's trees, from their snapshot when this node's copies are stale; then the seeds,
        the request and the request's files, through the safe writer."""
        workspace, state, rundir = session_dir / "workspace", session_dir / "state", session_dir / "run"
        marker = session_dir / ".snapshot"
        if fetched["snapshot"]:
            restore(fetched["snapshot"], session_dir, keep=("run",))
            marker.write_text(job["snapshot"])
        elif job["new_session"]:
            shutil.rmtree(session_dir, ignore_errors=True)
        if fetched["home"]:
            restore(fetched["home"], home)
        for p in (workspace, state, rundir, home):
            p.mkdir(parents=True, exist_ok=True)
        files.unlink(rundir, "interrupt")
        shutil.rmtree(rundir / "files", ignore_errors=True)
        (rundir / "files").mkdir()
        if job["new_session"] and (seed := harness.seed(folder, "workspace")):
            harness.copy_seed(seed, workspace, overwrite=False)
        if seed := harness.seed(folder, "home"):
            harness.copy_seed(seed, home, overwrite=True)
        if fetched["files"]:
            with tarfile.open(fileobj=io.BytesIO(fetched["files"]), mode="r:gz") as tar:
                for m in tar.getmembers():
                    if m.isfile():
                        files.write(rundir / "files", m.name, tar.extractfile(m).read())
        files.write(rundir, "request.json", fetched["request"])

    async def _stream(self, rid: str, execution: Execution, door_log: io.BytesIO) -> Outcome:
        """Relay the run's output as chunk records, a batch every quarter second, until it ends."""
        pending: list[dict[str, Any]] = []
        seq = 0

        async def flush() -> None:
            nonlocal pending
            if pending:
                batch, pending = pending, []
                try:
                    await self._http.post(f"/internal/turns/{rid}/chunks", json={"chunks": batch})
                except (httpx.HTTPError, OSError):
                    pass

        async def flusher() -> None:
            while True:
                await asyncio.sleep(CHUNK_EVERY_S)
                await flush()
        ticker = asyncio.create_task(flusher())
        try:
            async for event in execution.events():
                if event.stream == "door":
                    door_log.write(event.data)
                    continue
                seq += 1
                pending.append({"seq": seq, "stream": event.stream, "text": event.data.decode("utf-8", "replace")})
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
            await flush()
        return await execution.wait()

    @staticmethod
    def _report(outcome: Outcome, rundir: Path, limits: Limits, shaped: bool) -> dict[str, Any]:
        cap = limits.output_mb * 1024 * 1024

        def text(name: str) -> str:
            try:
                return files.read(rundir, name, limit=cap + 1).decode("utf-8", "replace")
            except (Invalid, FileNotFoundError):
                return ""
        out = {"outcome": outcome.kind, "exit_code": outcome.code, "error": outcome.reason, "shaped": shaped,
               "response": text("response") if shaped else "", "stdout": text("stdout"), "stderr": text("stderr")}
        if err := text("response.err"):
            out["stderr"] = out["stderr"] + ("\n" if out["stderr"] else "") + "response.sh: " + err
        out["truncated"] = any(len(out[k]) > cap for k in ("response", "stdout", "stderr"))
        return out

    async def _snapshots(self, job: dict[str, Any], session_dir: Path, home: Path, limits: Limits) -> bool:
        """The home always, the session (workspace and state) when it is stored; False when nothing was kept."""
        cap = limits.disk_mb * 1024 * 1024
        home_tgz = await asyncio.to_thread(snapshot, home)
        if len(home_tgz) <= cap:
            await self._put(job["urls"]["home_put"], home_tgz)
        if not job["store"] or not job["urls"]["snapshot_put"]:
            shutil.rmtree(session_dir, ignore_errors=True)
            return False
        tgz = await asyncio.to_thread(snapshot, session_dir, ("workspace", "state"))
        if len(tgz) > cap:
            raise Invalid(f"the workspace is over the organization's disk limit of {limits.disk_mb} MB")
        await self._put(job["urls"]["snapshot_put"], tgz)
        (session_dir / ".snapshot").write_text(job["id"])
        return True

    # blobs, through the URLs the turn came with
    async def _get(self, url: str) -> bytes:
        r = await self._blob_http.get(url)
        r.raise_for_status()
        return r.content

    async def _put(self, url: str, data: bytes) -> None:
        r = await self._blob_http.put(url, content=data, headers={"Content-Type": "application/octet-stream"})
        r.raise_for_status()


def snapshot(tree: Path, only: Sequence[str] = ()) -> bytes:
    """A tree as a gzipped tar, links kept as links, nothing followed; `only` names the top-level
    entries to take, every one when empty."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", dereference=False) as tar:
        if tree.is_dir():
            if only:
                for name in only:
                    if (tree / name).exists():
                        tar.add(tree / name, arcname=f"./{name}", recursive=True)
            else:
                tar.add(tree, arcname=".", recursive=True)
    return buf.getvalue()


def restore(data: bytes, into: Path, keep: Sequence[str] = ()) -> None:
    """A snapshot into a tree emptied first (but for `keep`), with the data filter: nothing escapes,
    nothing is special."""
    into.mkdir(parents=True, exist_ok=True)
    for child in into.iterdir():
        if child.name not in keep:
            shutil.rmtree(child, ignore_errors=True) if child.is_dir() and not child.is_symlink() else child.unlink()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(into, filter="data")
