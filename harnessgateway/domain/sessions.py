"""Sessions and turns, as the member sees them. A turn is one record that is both the response a
member reads and the job a node runs; it moves queued -> claimed -> in_progress -> a final status. A
session is the chain of turns that share a workspace, kept as snapshots in blobs.

    response/<org>/<rid>      the turn: public fields, job fields, and the node's commands
    session/<org>/<sid>       the chain: turns, the snapshot, which turn runs now, which node holds it
    chunk/<org>/<rid>:<seq>   a running turn's output, as it arrives, for any number of followers
    trace/<org>/<sid>:<seq>   every request, response, cancel, disconnect and supersede, as sent

    blobs tenants/<o>/sessions/<sid>/snapshot.tgz             workspace/ and state/ after the last turn
          tenants/<o>/members/<m>/harnesses/<h>/home.tgz      the member's home for that package
          tenants/<o>/sessions/<sid>/turns/<rid>/request.json the client's request, verbatim
          tenants/<o>/sessions/<sid>/turns/<rid>/…            files in, the door log, the provider captures

Reach is organization, then member: a member sees their own work, an owner the organization's,
nobody another organization's. A session runs one turn at a time, and a new request on a running
session supersedes the running turn: that turn is stopped gracefully and the new one runs when it
has ended, the way a coding agent's web view takes the next message. The node's side of a turn
(claim, output, result) is `services.scheduler`.
"""
from __future__ import annotations

import asyncio
import io
import json
import posixpath
import tarfile
import time
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Mapping

from ..backends.blobs import Blobs
from ..backends.events import Events
from ..backends.records import Records
from ..contract import wire
from ..errors import Conflict, Invalid, NotFound
from ..kernel.ids import new_id
from . import load
from .harnesses import Harnesses
from .members import Member
from .orgs import Orgs
from .providers import Providers

FINAL = ("completed", "failed", "cancelled", "incomplete")
RUNNING = "1"
FILE_MAX = 4 * 1024 * 1024                       # of one workspace file read back through the API
WORKSPACE = "./workspace/"
TITLE_MAX = 120


@dataclass
class Turn:
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
    background: bool = False                         # a background turn outlives the request that made it
    status: str = "queued"
    running: str = "0"                               # "1" while a node has it: counted against max_runs
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
    stop_reason: str = ""                            # why it was stopped: cancelled, client_disconnected, or superseded

    @property
    def final(self) -> bool:
        return self.status in FINAL


@dataclass
class Session:
    id: str
    org: str
    member: str
    harness: str
    version: str
    created: float
    title: str = ""
    stored: bool = True
    responses: list[str] = field(default_factory=list)
    running: str = ""                                # the turn in progress, if any; the next queued one takes over when it ends
    snapshot_key: str = ""                           # blob key of the last snapshot, if any
    snapshot: str = ""                               # the turn that made it (a node's cache key)
    node: str = ""                                   # the node that ran the last turn and holds the trees

    @property
    def turn(self) -> int:
        return len(self.responses)


def home_blob(org: str, member: str, harness: str) -> str:
    return f"tenants/{org}/members/{member}/harnesses/{harness}/home.tgz"


def turn_blob(org: str, sid: str, rid: str, name: str) -> str:
    return f"tenants/{org}/sessions/{sid}/turns/{rid}/{name}"


def snapshot_blob(org: str, sid: str) -> str:
    return f"tenants/{org}/sessions/{sid}/snapshot.tgz"


class Sessions:
    def __init__(self, records: Records, blobs: Blobs, events: Events, orgs: Orgs, harnesses: Harnesses, providers: Providers):
        self.records, self.blobs, self.events = records, blobs, events
        self.orgs, self.harnesses, self.providers = orgs, harnesses, providers

    # records
    def put_session(self, s: Session) -> None:
        self.records.put("session", s.org, s.id, asdict(s))

    def put_turn(self, r: Turn) -> None:
        self.records.put("response", r.org, r.id, asdict(r))

    def session_by_id(self, sid: str) -> Session:
        found = self.records.find("session", sid) if sid.startswith("sess_") else None
        if not found:
            raise NotFound(f"no session {sid}")
        return load(Session, found[1])

    def turn_by_id(self, rid: str) -> Turn:
        found = self.records.find("response", rid) if rid.startswith("resp_") else None
        if not found:
            raise NotFound(f"no response {rid}")
        return load(Turn, found[1])

    def trace_in(self, s: Session, kind: str, rid: str, body: Any) -> None:
        seq = len(self.records.list("trace", s.org, f"{s.id}:"))
        self.records.put("trace", s.org, f"{s.id}:{seq:06d}", {"at": time.time(), "kind": kind, "response": rid, "body": body})

    # reach
    def reachable(self, who: Member, org: str, member: str) -> bool:
        return org == who.org and (who.owner or member == who.name)

    def session(self, who: Member, sid: str) -> Session:
        s = self.session_by_id(sid)
        if not self.reachable(who, s.org, s.member):
            raise NotFound(f"no session {sid}")
        return s

    def turn(self, who: Member, rid: str) -> Turn:
        r = self.turn_by_id(rid)
        if not self.reachable(who, r.org, r.member):
            raise NotFound(f"no response {rid}")
        return r

    def list(self, who: Member) -> list[Session]:
        found = [load(Session, d) for d in self.records.list("session", who.org)]
        return sorted((s for s in found if self.reachable(who, s.org, s.member)), key=lambda s: -s.created)

    def turns_of(self, who: Member, sid: str) -> list[Turn]:
        s = self.session(who, sid)
        return [self.turn_by_id(rid) for rid in s.responses]

    def trace(self, who: Member, sid: str) -> list[dict[str, Any]]:
        s = self.session(who, sid)
        return self.records.list("trace", s.org, f"{s.id}:")

    # a turn asked
    def create(self, who: Member, req: wire.Request) -> Turn:
        """Queue a turn; a node runs it. A session that is running a turn stops it: the new request
        supersedes the running one, which ends `cancelled` with reason `superseded`, and the new turn
        runs on the workspace it left."""
        org = self.orgs.active(who.org)
        superseded = None
        if req.previous:
            prev = self.turn_by_id(req.previous)
            if not self.reachable(who, prev.org, prev.member):
                raise NotFound(f"no response {req.previous}")
            s = self.session_by_id(prev.session)
            if not s.stored:
                raise Invalid(f"{req.previous} was not stored, so it cannot be continued")
            if s.running:
                superseded = self.turn_by_id(s.running)
            if req.harness != s.harness:
                raise Invalid(f"session {s.id} runs {s.harness}, not {req.harness}")
            version = s.version
            config = self.harnesses.config_at(who.org, s.harness, version)
            inherited = prev.provider                 # a continuation keeps the provider unless the request names one
        else:
            inherited = ""
            h = self.harnesses.get(who.org, req.harness)
            version = h.head
            config = h.configuration()
            s = Session(new_id("sess"), who.org, who.name, h.name, version, time.time(),
                        req.text.strip().splitlines()[0][:TITLE_MAX] if req.text.strip() else "", req.store)
        provider_name = req.provider or inherited or config.provider
        if not provider_name and config.needs_provider:
            raise Invalid("no provider: name one in the request's model path (harness/provider/model) or in harness.json's defaults")
        provider = self.providers.get(who.org, provider_name) if provider_name else None   # a package without provider_env needs none
        rid = new_id("resp")
        files = ""
        if req.files:
            total = sum(len(b) for b in req.files.values())
            if total > org.ceilings.upload_mb * 1024 * 1024:
                raise Invalid(f"the request's files are over the organization's upload limit of {org.ceilings.upload_mb} MB")
            files = turn_blob(s.org, s.id, rid, "files.tgz")
            self.blobs.put(files, pack_files(req.files))
        self.blobs.put(turn_blob(s.org, s.id, rid, "request.json"), json.dumps(req.body).encode())
        r = Turn(rid, s.id, s.org, s.member, s.harness, version, provider.name if provider else "", req.model or config.model, s.turn + 1,
                 time.time(), req.previous, req.store, dict(req.metadata), text=req.text, instructions=req.body.get("instructions"),
                 background=req.background, files=files)
        if superseded is not None and not superseded.final:
            self.trace_in(s, "supersede", superseded.id, {"by": rid})
            self._stop(superseded, s, "superseded")          # a queued one ends now and frees the session; a running one when the node stops it
        s.responses.append(rid)
        if not s.running:
            s.running = rid
        self.put_session(s)
        self.put_turn(r)
        self.trace_in(s, "request", rid, req.for_trace())
        return r

    def next_turn(self, s: Session) -> str:
        """The session's oldest queued turn, which takes the session over when the running one ends."""
        for rid in s.responses:
            d = self.records.get("response", s.org, rid)
            if d and d["status"] == "queued":
                return rid
        return ""

    # the response object and the stream
    def response_object(self, r: Turn) -> dict[str, Any]:
        meta = {**r.metadata, **r.shaped, "session_id": r.session, "harness": r.harness, "version": r.version[:12], "turn": str(r.turn),
                **({"exit_code": str(r.exit_code)} if r.exit_code is not None else {}), **({"truncated": "true"} if r.truncated else {})}
        status = r.status if r.final else "in_progress"
        incomplete = r.incomplete if r.status in ("incomplete", "cancelled") else None
        return wire.response(rid=r.id, created=r.created, status=status, model=wire.model_path(r.harness, r.provider, r.model),
                             previous=r.previous, store=r.store, instructions=r.instructions, metadata=meta,
                             output=r.output if r.final else [], usage=r.usage if r.final else None,
                             error=r.error if r.status == "failed" else None, incomplete=incomplete, completed=r.ended)

    async def wait(self, rid: str, poll_s: float = 0.25) -> Turn:
        while not (r := self.turn_by_id(rid)).final:
            await asyncio.sleep(poll_s)
        return r

    def follow(self, who: Member, rid: str, poll_s: float = 0.25) -> AsyncIterator[dict[str, Any]]:
        """The package's stream events as they arrive, or all of them if it has ended."""
        r = self.turn(who, rid)

        async def live() -> AsyncIterator[dict[str, Any]]:
            seen, pending = 0, ""
            while True:
                final = self.turn_by_id(rid).final
                chunks = self.events.read(r.org, rid)             # after the final check, so nothing arrives unseen
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
                await self.events.wait(r.org, rid, seen, poll_s)

        async def replay() -> AsyncIterator[dict[str, Any]]:
            for item in r.output:
                yield {"type": "response.output_item.done", "output_index": r.output.index(item), "item": item}
        return replay() if r.final else live()

    def live_log(self, r: Turn) -> tuple[str, str]:
        """What a running turn has printed so far: the log lines and stderr."""
        if r.final:
            return r.log, r.stderr
        chunks = self.events.read(r.org, r.id)
        stdout = "".join(c["text"] for c in chunks if c["stream"] == "stdout")
        return wire.events_of(stdout)[1], "".join(c["text"] for c in chunks if c["stream"] == "stderr")

    def live_items(self, r: Turn) -> list[dict[str, Any]]:
        """The items a running turn has completed so far."""
        if r.final:
            return r.output
        chunks = self.events.read(r.org, r.id)
        try:
            return wire.from_events(wire.events_of("".join(c["text"] for c in chunks if c["stream"] == "stdout"))[0]).output
        except Invalid:
            return []

    # stopping
    def cancel(self, who: Member, rid: str, reason: str = "cancelled") -> Turn:
        """Ask the node to stop it gracefully (the package's stop signal, then its grace); a queued turn
        ends at once. `reason` is what the response says: `cancelled`, or `client_disconnected`."""
        r = self.turn(who, rid)
        if r.final:
            return r
        s = self.session_by_id(r.session)
        self.trace_in(s, "cancel" if reason == "cancelled" else "disconnect", rid, {})
        return self._stop(r, s, reason)

    def _stop(self, r: Turn, s: Session, reason: str) -> Turn:
        if r.status == "queued" and self.records.update_if("response", r.org, r.id, "status", "queued",
                                                           asdict(Turn(**{**asdict(r), "status": "cancelled", "running": "0", "stop_reason": reason,
                                                                          "incomplete": {"reason": reason}, "ended": time.time()}))):
            if s.running == r.id:
                s.running = self.next_turn(s)
                self.put_session(s)
            return self.turn_by_id(r.id)
        r = self.turn_by_id(r.id)
        r.cancel, r.stop_reason = True, reason
        self.put_turn(r)
        return r

    def disconnected(self, who: Member, rid: str) -> Turn:
        """The client that asked for this turn closed its request. A foreground turn is theirs for as
        long as they wait, so it is stopped the way a cancel stops it; a background turn (`background:
        true`) was asked to outlive the request and runs on."""
        r = self.turn(who, rid)
        if r.final or r.background:
            return r
        return self.cancel(who, rid, reason="client_disconnected")

    # the workspace, from its last snapshot
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

    # what the turn exchanged
    def captures(self, who: Member, rid: str) -> list[str]:
        r = self.turn(who, rid)
        prefix = turn_blob(r.org, r.session, r.id, "")
        return [k[len(prefix):] for k in self.blobs.list(prefix.rstrip("/"))]

    def capture(self, who: Member, rid: str, name: str) -> bytes:
        r = self.turn(who, rid)
        if name not in self.captures(who, rid):
            raise NotFound(f"no capture {name}")
        return self.blobs.get(turn_blob(r.org, r.session, r.id, name))

    def request_body(self, who: Member, rid: str) -> dict[str, Any]:
        """The client's request, as the package received it."""
        r = self.turn(who, rid)
        return json.loads(self.blobs.get(turn_blob(r.org, r.session, r.id, "request.json")))

    def delete(self, who: Member, sid: str) -> None:
        s = self.session(who, sid)
        if s.running:
            raise Conflict(f"session {sid} is running a response")
        for rid in s.responses:
            for kind in ("response", "capture"):
                self.records.delete(kind, s.org, rid)
            self.events.clear(s.org, rid)
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
