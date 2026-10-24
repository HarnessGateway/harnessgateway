"""Layer 5: sessions and turns, as the control plane sees them. A turn is one record that is both
the response a member reads and the job a worker claims; it moves queued -> claimed -> in_progress ->
a final status. A session is the chain of turns that share a workspace, kept as snapshots in blobs.

    response/<org>/<rid>      the turn: public fields, job fields, and the worker's commands
    session/<org>/<sid>       the chain: turns, the snapshot, which turn runs now
    chunk/<org>/<rid>:<seq>   a running turn's output, as it arrives, for any number of followers
    trace/<org>/<sid>:<seq>   every request, response, steer and cancel, as sent
    usage/<org>/<rid>         calls and bytes (the gateway's), seconds, the time, harness and status (the turn's)
    rodeo/<org>/<aid>         a matchup: one task, several harnesses' turns, and the winner the member picked

    blobs tenants/<o>/sessions/<sid>/snapshot.tgz             workspace/ and state/ after the last turn
          tenants/<o>/members/<m>/harnesses/<h>/home.tgz      the member's home for that package
          tenants/<o>/sessions/<sid>/turns/<rid>/request.json the client's request, verbatim
          tenants/<o>/sessions/<sid>/turns/<rid>/…            files in, the door log, the provider captures

The request payload goes to the package whole; the package's stream events and `response.sh`
shape come back and are validated against the Responses schema here (`wire`) before a client sees
them. Workers pull: `claim` hands out the oldest queued turn whose organization has a free slot,
with URLs good for that turn's blobs and a token good for that turn's provider; `commands` carries
cancels and steers back on each heartbeat; `result` ends it.
"""
from __future__ import annotations

import asyncio
import io
import json
import posixpath
import tarfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Mapping

from . import wire
from .errors import Conflict, Invalid, NotFound
from .runtimes.docker import PROVIDER_URL
from .store import Member, Store, Worker

FINAL = ("completed", "failed", "cancelled", "incomplete")
RUNNING = "1"
LOST_AFTER_S = 45
FILE_MAX = 4 * 1024 * 1024                       # of one workspace file read back through the API
WORKSPACE = "./workspace/"


@dataclass
class Record:
    """A turn, as kept. The public part becomes the response object; the rest is the job."""
    id: str
    session: str
    org: str
    member: str
    harness: str
    version: str                                     # the package commit the session runs on
    provider: str
    model: str
    turn: int
    created: float
    previous: str | None
    store: bool
    metadata: dict[str, str]
    text: str = ""                                   # the input's text, for lists and the console
    instructions: Any = None
    team: str = ""                                   # the harness's team: its members may see this turn
    background: bool = False                         # a background turn outlives the request that made it
    status: str = "queued"
    running: str = "0"                               # "1" while a worker has it: counted against max_runs
    worker: str = ""
    started: float | None = None
    ended: float | None = None
    output: list[dict[str, Any]] = field(default_factory=list)   # the response's items, validated
    usage: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    incomplete: dict[str, str] | None = None
    shaped: dict[str, str] = field(default_factory=dict)        # metadata the package added
    log: str = ""                                    # request.sh's stdout lines that were not events
    stderr: str = ""
    exit_code: int | None = None
    truncated: bool = False
    files: str = ""                                  # blob key of the request's files, if any
    cancel: bool = False
    stop_reason: str = ""                            # why it was stopped: cancelled, or client_disconnected
    interrupts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def final(self) -> bool:
        return self.status in FINAL


@dataclass
class SessionRecord:
    id: str
    org: str
    member: str
    harness: str
    version: str
    created: float
    team: str = ""
    title: str = ""
    stored: bool = True
    responses: list[str] = field(default_factory=list)
    running: str = ""                                # the turn in progress, if any
    snapshot_key: str = ""                           # blob key of the last snapshot, if any
    snapshot: str = ""                               # the turn that made it (a worker's cache key)

    @property
    def turn(self) -> int:
        return len(self.responses)


def home_blob(org: str, member: str, harness: str) -> str:
    return f"tenants/{org}/members/{member}/harnesses/{harness}/home.tgz"


def turn_blob(org: str, sid: str, rid: str, name: str) -> str:
    return f"tenants/{org}/sessions/{sid}/turns/{rid}/{name}"


class Sessions:
    def __init__(self, store: Store, public_url: str, gateway_url: str):
        self.store, self.public_url, self.gateway_url = store, public_url, gateway_url
        self.records, self.blobs = store.records, store.blobs

    # records
    def _put_session(self, s: SessionRecord) -> None:
        self.records.put("session", s.org, s.id, asdict(s))

    def _put_record(self, r: Record) -> None:
        self.records.put("response", r.org, r.id, asdict(r))

    def _session_by_id(self, sid: str) -> SessionRecord:
        found = self.records.find("session", sid) if sid.startswith("sess_") else None
        if not found:
            raise NotFound(f"no session {sid}")
        return SessionRecord(**found[1])

    def _record_by_id(self, rid: str) -> Record:
        found = self.records.find("response", rid) if rid.startswith("resp_") else None
        if not found:
            raise NotFound(f"no response {rid}")
        return Record(**found[1])

    def _trace(self, s: SessionRecord, kind: str, rid: str, body: Any) -> None:
        seq = len(self.records.list("trace", s.org, f"{s.id}:"))
        self.records.put("trace", s.org, f"{s.id}:{seq:06d}", {"at": time.time(), "kind": kind, "response": rid, "body": body})

    def _reachable(self, who: Member, org: str, member: str, team: str = "") -> bool:
        """Reach is organization, then team, then member: a member sees their own work and the work
        of the teams they are in; an owner the organization's; nobody another organization's."""
        if org != who.org:
            return False
        return who.owner or member == who.name or (bool(team) and self.store.in_team(who, team))

    def session(self, who: Member, sid: str) -> SessionRecord:
        s = self._session_by_id(sid)
        if not self._reachable(who, s.org, s.member, s.team):
            raise NotFound(f"no session {sid}")
        return s

    def response(self, who: Member, rid: str) -> Record:
        r = self._record_by_id(rid)
        if not self._reachable(who, r.org, r.member, r.team):
            raise NotFound(f"no response {rid}")
        return r

    def sessions(self, who: Member) -> list[SessionRecord]:
        found = [SessionRecord(**d) for d in self.records.list("session", who.org)]
        return sorted((s for s in found if self._reachable(who, s.org, s.member, s.team)), key=lambda s: -s.created)

    def records_of(self, who: Member, sid: str) -> list[Record]:
        s = self.session(who, sid)
        return [self._record_by_id(rid) for rid in s.responses]

    def trace(self, who: Member, sid: str) -> list[dict[str, Any]]:
        s = self.session(who, sid)
        return self.records.list("trace", s.org, f"{s.id}:")

    # a turn asked
    def create(self, who: Member, req: wire.Request) -> Record:
        """Queue a turn; a worker runs it. Conflict while the session is busy."""
        org = self.store.org(who.org)
        if org.status != "active":
            raise Invalid(f"organization {org.name} is suspended")
        if req.previous:
            prev = self._record_by_id(req.previous)
            if not self._reachable(who, prev.org, prev.member, prev.team):
                raise NotFound(f"no response {req.previous}")
            s = self._session_by_id(prev.session)
            if not s.stored:
                raise Invalid(f"{req.previous} was not stored, so it cannot be continued")
            if s.running:
                raise Conflict(f"session {s.id} is running a response")
            if req.harness != s.harness:
                raise Invalid(f"session {s.id} runs {s.harness}, not {req.harness}")
            h = self.store.harness(who.org, s.harness)
            version = s.version
            config = self.store.harness_config(who.org, h.name, version)
            inherited = prev.provider                 # a continuation keeps the provider unless the request names one
        else:
            inherited = ""
            h = self.store.harness(who.org, req.harness)
            if not self.store.in_team(who, h.team):
                raise NotFound(f"no harness {req.harness}")
            version = h.head
            config = h.configuration()
            s = SessionRecord(f"sess_{uuid.uuid4().hex}", who.org, who.name, h.name, version, time.time(), h.team,
                              req.text.strip().splitlines()[0][:120] if req.text.strip() else "", req.store)
        provider_name = req.provider or inherited or config.provider
        if not provider_name:
            raise Invalid("no provider: name one in the request's model path (harness/provider/model) or in harness.json's defaults")
        provider = self.store.provider(who.org, provider_name)
        rid = f"resp_{uuid.uuid4().hex}"
        files = ""
        if req.files:
            total = sum(len(b) for b in req.files.values())
            if total > org.ceilings.upload_mb * 1024 * 1024:
                raise Invalid(f"the request's files are over the organization's upload limit of {org.ceilings.upload_mb} MB")
            files = turn_blob(s.org, s.id, rid, "files.tgz")
            self.blobs.put(files, pack_files(req.files))
        self.blobs.put(turn_blob(s.org, s.id, rid, "request.json"), json.dumps(req.body).encode())
        r = Record(rid, s.id, s.org, s.member, s.harness, version, provider.name, req.model or config.model, s.turn + 1,
                   time.time(), req.previous, req.store, dict(req.metadata), text=req.text, instructions=req.body.get("instructions"),
                   team=s.team, background=req.background, files=files)
        s.responses.append(rid)
        s.running = rid
        self._put_session(s)
        self._put_record(r)
        self._trace(s, "request", rid, req.for_trace())
        return r

    # the worker's side
    def claim(self, worker: Worker) -> dict[str, Any] | None:
        """The oldest queued turn whose organization has a free slot, as a job, or None."""
        for org, rid, doc in self.records.where("response", "status", "queued", limit=50):
            try:
                ceilings = self.store.org(org).ceilings
            except NotFound:
                continue
            if self.records.count("response", org, "running", RUNNING) >= ceilings.max_runs:
                continue
            r = Record(**{**doc, "status": "claimed", "running": RUNNING, "worker": worker.id, "started": time.time()})
            if self.records.update_if("response", org, rid, "status", "queued", asdict(r)):
                return self._job(r, ceilings)
        return None

    def _job(self, r: Record, ceilings) -> dict[str, Any]:
        s = self._session_by_id(r.session)
        config = self.store.harness_config(r.org, r.harness, r.version)
        provider = self.store.provider(r.org, r.provider)
        timeout = min(config.timeout_s, ceilings.run_timeout_s)
        token = self.store.issue_token(r.org, provider.name, s.id, r.id, provider.api, timeout + 120)
        home = home_blob(r.org, r.member, r.harness)
        get, put = self.blobs.presign_get, self.blobs.presign_put
        ttl = int(timeout) + 600
        return {"id": r.id, "session": s.id, "org": r.org, "team": r.team, "member": r.member, "harness": r.harness, "version": r.version,
                "turn": r.turn, "new_session": r.turn == 1, "previous": r.previous or "", "model": r.model,
                "store": r.store, "config": config.as_json(), "limits": ceilings.as_dict(), "timeout_s": timeout, "token": token,
                "provider": {"name": provider.name, "api": provider.api, "path": provider.path, "url": PROVIDER_URL + provider.path},
                "gateway_url": self.gateway_url, "snapshot": s.snapshot,
                "urls": {"folder": get(self.store.harness_blob(r.org, r.harness, r.version), ttl),
                         "request": get(turn_blob(r.org, s.id, r.id, "request.json"), ttl),
                         "snapshot": get(s.snapshot_key, ttl) if s.snapshot_key else "",
                         "home": get(home, ttl) if self.blobs.exists(home) else "",
                         "files": get(r.files, ttl) if r.files else "",
                         "snapshot_put": put(f"tenants/{r.org}/sessions/{s.id}/snapshot.tgz", ttl) if r.store else "",
                         "home_put": put(home, ttl),
                         "door_log_put": put(turn_blob(r.org, s.id, r.id, "door.log"), ttl)}}

    def started(self, worker: Worker, rid: str) -> None:
        r = self._record_by_id(rid)
        if r.worker == worker.id and r.status == "claimed":
            r.status = "in_progress"
            self._put_record(r)

    def chunks_in(self, worker: Worker, rid: str, chunks: list[dict[str, Any]]) -> None:
        r = self._record_by_id(rid)
        if r.worker != worker.id or r.final:
            raise Conflict(f"{rid} is not this worker's running turn")
        for c in chunks:
            self.records.put("chunk", r.org, f"{rid}:{int(c['seq']):08d}", {"stream": c["stream"], "text": c["text"]})

    def commands(self, worker: Worker, running: list[str]) -> dict[str, dict[str, Any]]:
        """What the worker should do to the turns it reports; also its heartbeat."""
        self.store.touch_worker(worker, len(running))
        out: dict[str, dict[str, Any]] = {}
        for rid in running:
            try:
                r = self._record_by_id(rid)
            except NotFound:
                out[rid] = {"cancel": True, "interrupts": []}
                continue
            out[rid] = {"cancel": r.cancel or r.final, "interrupts": r.interrupts}
        return out

    def result(self, worker: Worker, rid: str, body: dict[str, Any]) -> Record:
        """The worker's report ends the turn: the package's events and shape become the response."""
        r = self._record_by_id(rid)
        if r.worker != worker.id:
            raise Conflict(f"{rid} is not this worker's turn")
        if r.final:
            return r
        s = self._session_by_id(r.session)
        ceilings = self.store.org(r.org).ceilings
        cap = ceilings.output_mb * 1024 * 1024
        kind, code = body.get("outcome", "failed"), body.get("exit_code")
        stdout, stderr = str(body.get("stdout", ""))[:cap], str(body.get("stderr", ""))[:cap]
        r.truncated = bool(body.get("truncated")) or any(len(str(body.get(k, ""))) > cap for k in ("stdout", "stderr", "response"))
        r.exit_code, r.ended = code, time.time()
        events, r.log = wire.events_of(stdout)
        r.stderr = stderr
        shape = wire.Shape()
        try:
            if body.get("shaped"):
                shape = wire.from_response_sh(str(body.get("response", "")))
            else:
                shape = wire.from_events(events)
            if not shape.output and not events and r.log.strip() and kind in ("exited", "stopped", "timed_out"):
                shape.output = [wire.message_item(rid, r.log)]   # a package that printed plain text: its text is the answer
                shape.metadata["raw_stdout"] = "true"
        except Invalid as e:
            shape = wire.Shape(status="failed", error={"code": "package_output_invalid", "message": str(e)})
        r.output, r.usage, r.shaped = shape.output, shape.usage, shape.metadata
        if shape.status:
            r.status, r.error, r.incomplete = shape.status, shape.error, shape.incomplete
        elif kind == "failed":
            r.status, r.error = "failed", {"code": "server_error", "message": str(body.get("error", "the turn could not run"))}
        elif kind == "timed_out":
            r.status, r.incomplete = "incomplete", {"reason": "timeout"}
        elif kind in ("stopped", "killed"):
            r.status, r.incomplete = "cancelled", {"reason": r.stop_reason or "cancelled"}
        elif code == 0:
            r.status = "completed"
        else:
            r.status, r.error = "failed", {"code": "exit_status", "message": f"the package exited {code}"}
        if r.status == "completed" and shape.error:
            r.status = "failed"
        r.running = "0"
        self._put_record(r)
        usage = self.records.get("usage", r.org, rid) or {"calls": 0, "bytes_in": 0, "bytes_out": 0, "seconds": 0}
        tokens = {k: (r.usage or {}).get(k, 0) for k in ("input_tokens", "output_tokens", "total_tokens")}
        self.records.put("usage", r.org, rid, {**usage, **tokens, "seconds": round(float(body.get("seconds", 0)), 1), "at": r.ended,
                                               "harness": r.harness, "status": r.status, "member": r.member})
        if r.store and body.get("snapshot"):
            s.snapshot_key, s.snapshot = f"tenants/{r.org}/sessions/{s.id}/snapshot.tgz", rid
        if not r.store:
            s.stored = False
        s.running = ""
        self._put_session(s)
        self._trace(s, "response", rid, self.response_object(r))
        return r

    def sweep_lost(self) -> list[str]:
        """Turns whose worker stopped heartbeating are failed; their sessions go on."""
        lost: list[str] = []
        stale = {w.id for w in self.store.workers() if time.time() - w.last_seen > LOST_AFTER_S}
        for org, rid, doc in self.records.where("response", "running", RUNNING, limit=1000):
            if doc["worker"] in stale:
                worker = Worker(doc["worker"], "", 0, 0)
                self.result(worker, rid, {"outcome": "failed", "error": "the worker running it was lost"})
                lost.append(rid)
        return lost

    # the response object and the stream
    def response_object(self, r: Record) -> dict[str, Any]:
        meta = {**r.metadata, **r.shaped, "session_id": r.session, "harness": r.harness, "version": r.version[:12], "turn": str(r.turn),
                **({"exit_code": str(r.exit_code)} if r.exit_code is not None else {}), **({"truncated": "true"} if r.truncated else {})}
        status = r.status if r.final else "in_progress"
        incomplete = r.incomplete if r.status in ("incomplete", "cancelled") else None
        return wire.response(rid=r.id, created=r.created, status=status, model=wire.model_path(r.harness, r.provider, r.model),
                             previous=r.previous, store=r.store, instructions=r.instructions, metadata=meta,
                             output=r.output if r.final else [], usage=r.usage if r.final else None,
                             error=r.error if r.status == "failed" else None, incomplete=incomplete, completed=r.ended)

    async def wait(self, rid: str, poll_s: float = 0.25) -> Record:
        while not (r := self._record_by_id(rid)).final:
            await asyncio.sleep(poll_s)
        return r

    def follow(self, who: Member, rid: str, poll_s: float = 0.25) -> AsyncIterator[dict[str, Any]]:
        """The package's stream events as they arrive, or all of them if it has ended."""
        r = self.response(who, rid)

        async def live() -> AsyncIterator[dict[str, Any]]:
            seen, pending = 0, ""
            while True:
                chunks = self.records.list("chunk", r.org, f"{rid}:")
                final = self._record_by_id(rid).final
                if final:
                    chunks = self.records.list("chunk", r.org, f"{rid}:")
                for c in chunks[seen:]:
                    if c["stream"] == "stdout":
                        pending += c["text"]
                        *lines, pending = pending.split("\n")
                        for e in wire.events_of("\n".join(lines))[0]:
                            yield e
                seen = len(chunks)
                if final:
                    for e in wire.events_of(pending)[0]:
                        yield e
                    return
                await asyncio.sleep(poll_s)

        async def replay() -> AsyncIterator[dict[str, Any]]:
            for item in r.output:
                yield {"type": "response.output_item.done", "output_index": r.output.index(item), "item": item}
        return replay() if r.final else live()

    def live_log(self, r: Record) -> tuple[str, str]:
        """What a running turn has printed so far: the log lines and stderr."""
        if r.final:
            return r.log, r.stderr
        chunks = self.records.list("chunk", r.org, f"{r.id}:")
        stdout = "".join(c["text"] for c in chunks if c["stream"] == "stdout")
        return wire.events_of(stdout)[1], "".join(c["text"] for c in chunks if c["stream"] == "stderr")

    def live_items(self, r: Record) -> list[dict[str, Any]]:
        """The items a running turn has completed so far."""
        if r.final:
            return r.output
        chunks = self.records.list("chunk", r.org, f"{r.id}:")
        try:
            return wire.from_events(wire.events_of("".join(c["text"] for c in chunks if c["stream"] == "stdout"))[0]).output
        except Invalid:
            return []

    def cancel(self, who: Member, rid: str, reason: str = "cancelled") -> Record:
        """Ask the worker to stop it gracefully (the package's stop signal, then its grace); a queued
        turn ends at once. `reason` is what the response says: `cancelled`, or `client_disconnected`."""
        r = self.response(who, rid)
        if r.final:
            return r
        s = self._session_by_id(r.session)
        self._trace(s, "cancel" if reason == "cancelled" else "disconnect", rid, {})
        if r.status == "queued" and self.records.update_if("response", r.org, rid, "status", "queued",
                                                           asdict(Record(**{**asdict(r), "status": "cancelled", "running": "0", "stop_reason": reason,
                                                                            "incomplete": {"reason": reason}, "ended": time.time()}))):
            s.running = ""
            self._put_session(s)
            return self._record_by_id(rid)
        r = self._record_by_id(rid)
        r.cancel, r.stop_reason = True, reason
        self._put_record(r)
        return r

    def disconnected(self, who: Member, rid: str) -> Record:
        """The client that asked for this turn closed its request. A foreground turn is theirs for as
        long as they wait, so it is stopped the way a cancel stops it; a background turn (`background:
        true`) was asked to outlive the request and runs on."""
        r = self.response(who, rid)
        if r.final or r.background:
            return r
        return self.cancel(who, rid, reason="client_disconnected")

    def interrupt(self, who: Member, sid: str, text: str) -> Record:
        """Steer the session's running turn: the text reaches request.sh with the package's signal."""
        s = self.session(who, sid)
        if not s.running:
            raise Conflict(f"session {sid} is not running a response")
        r = self._record_by_id(s.running)
        if r.final or r.status == "queued":
            raise Conflict(f"session {sid} is not running a response")
        if not isinstance(text, str) or not text.strip():
            raise Invalid("a steer carries text")
        r.interrupts.append({"seq": len(r.interrupts) + 1, "text": text})
        self._put_record(r)
        self._trace(s, "interrupt", r.id, {"input": text})
        return r

    def archive(self, who: Member, sid: str) -> bytes:
        """The workspace after its last turn, as a gzipped tar."""
        s = self.session(who, sid)
        if not s.stored or not s.snapshot_key:
            raise NotFound(f"session {sid} kept no workspace")
        src = tarfile.open(fileobj=io.BytesIO(self.blobs.get(s.snapshot_key)), mode="r:gz")
        buf = io.BytesIO()
        with src, tarfile.open(fileobj=buf, mode="w:gz") as out:
            for m in src:
                if m.name.startswith(WORKSPACE) and m.name != WORKSPACE.rstrip("/"):
                    m.name = "./" + m.name[len(WORKSPACE):]
                    out.addfile(m, src.extractfile(m) if m.isfile() else None)
        return buf.getvalue()

    # the workspace's files, from its last snapshot
    def files(self, who: Member, sid: str) -> list[dict[str, Any]]:
        """Every file in the workspace after its last turn: path and size. Nothing before the first
        turn ends, and nothing for a stateless session."""
        s = self.session(who, sid)
        if not s.stored or not s.snapshot_key:
            return []
        out = []
        with tarfile.open(fileobj=io.BytesIO(self.blobs.get(s.snapshot_key)), mode="r:gz") as tar:
            for m in tar:
                if m.isfile() and m.name.startswith(WORKSPACE):
                    out.append({"path": posixpath.normpath(m.name[len(WORKSPACE):]), "size": m.size})
        return sorted(out, key=lambda f: f["path"])

    def file(self, who: Member, sid: str, path: str, limit: int = FILE_MAX) -> bytes:
        """One file from the workspace's last snapshot, at most `limit` bytes."""
        s = self.session(who, sid)
        wanted = posixpath.normpath(path)
        if not s.stored or not s.snapshot_key or wanted.startswith(("/", "..")):
            raise NotFound(f"no file {path}")
        with tarfile.open(fileobj=io.BytesIO(self.blobs.get(s.snapshot_key)), mode="r:gz") as tar:
            for m in tar:
                if m.isfile() and m.name.startswith(WORKSPACE) and posixpath.normpath(m.name[len(WORKSPACE):]) == wanted:
                    if m.size > limit:
                        raise Invalid(f"{path} is over {limit // (1024 * 1024)} MB; download the archive")
                    return tar.extractfile(m).read()
        raise NotFound(f"no file {path}")

    # rodeos: one task, several harnesses, the member's pick
    def create_rodeo(self, who: Member, task: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
        if not 2 <= len(entries) <= 6:
            raise Invalid("a rodeo has two to six entries")
        rows = []
        for e in entries:
            r = self.response(who, str(e.get("response", "")))
            rows.append({"harness": r.harness, "model": wire.model_path(r.harness, r.provider, r.model), "response": r.id, "session": r.session})
        if len({row["harness"] for row in rows}) < 2:
            raise Invalid("a rodeo pits different harnesses against each other")
        d = {"id": f"rodeo_{uuid.uuid4().hex[:12]}", "org": who.org, "member": who.name, "task": (task or "")[:4000],
             "entries": rows, "winner": "", "created": time.time()}
        self.records.put("rodeo", who.org, d["id"], d)
        return d

    def rodeo(self, who: Member, aid: str) -> dict[str, Any]:
        d = self.records.get("rodeo", who.org, aid) if aid.startswith("rodeo_") else None
        if d is None or not self._reachable(who, d["org"], d["member"]):
            raise NotFound(f"no rodeo {aid}")
        return d

    def rodeos(self, who: Member) -> list[dict[str, Any]]:
        found = [d for d in self.records.list("rodeo", who.org) if self._reachable(who, d["org"], d["member"])]
        return sorted(found, key=lambda d: -d["created"])

    def pick_winner(self, who: Member, aid: str, winner: str) -> dict[str, Any]:
        d = self.rodeo(who, aid)
        if winner and winner not in {e["harness"] for e in d["entries"]}:
            raise Invalid("the winner is one of the rodeo's harnesses")
        d["winner"] = winner
        self.records.put("rodeo", who.org, aid, d)
        return d

    def delete_rodeo(self, who: Member, aid: str) -> None:
        self.rodeo(who, aid)
        self.records.delete("rodeo", who.org, aid)

    def leaderboard(self, who: Member) -> list[dict[str, Any]]:
        """Per harness, across the rodeos this member can see: runs and wins, most wins first."""
        board: dict[str, dict[str, int]] = {}
        for d in self.rodeos(who):
            for e in d["entries"]:
                row = board.setdefault(e["harness"], {"runs": 0, "wins": 0})
                row["runs"] += 1
                row["wins"] += e["harness"] == d["winner"]
        return [{"harness": hn, **v} for hn, v in sorted(board.items(), key=lambda kv: (-kv[1]["wins"], -kv[1]["runs"], kv[0]))]

    def captures(self, who: Member, rid: str) -> list[str]:
        r = self.response(who, rid)
        prefix = turn_blob(r.org, r.session, r.id, "")
        return [k[len(prefix):] for k in self.blobs.list(prefix.rstrip("/"))]

    def capture(self, who: Member, rid: str, name: str) -> bytes:
        r = self.response(who, rid)
        if name not in self.captures(who, rid):
            raise NotFound(f"no capture {name}")
        return self.blobs.get(turn_blob(r.org, r.session, r.id, name))

    def request_body(self, who: Member, rid: str) -> dict[str, Any]:
        """The client's request, as the package received it."""
        r = self.response(who, rid)
        return json.loads(self.blobs.get(turn_blob(r.org, r.session, r.id, "request.json")))

    def delete(self, who: Member, sid: str) -> None:
        s = self.session(who, sid)
        if s.running:
            raise Conflict(f"session {sid} is running a response")
        for rid in s.responses:
            for kind in ("response", "usage"):
                self.records.delete(kind, s.org, rid)
            for i in range(len(self.records.list("chunk", s.org, f"{rid}:"))):
                self.records.delete("chunk", s.org, f"{rid}:{i:08d}")
        for i in range(len(self.records.list("trace", s.org, f"{sid}:"))):
            self.records.delete("trace", s.org, f"{sid}:{i:06d}")
        self.blobs.delete(f"tenants/{s.org}/sessions/{sid}")
        self.records.delete("session", s.org, sid)


def pack_files(files: Mapping[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, data in files.items():
            info = tarfile.TarInfo(posixpath.normpath(rel))
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
