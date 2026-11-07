"""Boxes (SPEC §6, §9.2, §11, §12): the platform's side of a sandbox's life, and what the door asks about it.

One live box per session. The api creates a box row when a turn is queued and no box stands by (a worker launches
it), hands the box its session's next turn when the runner asks, takes the runner's events, heartbeats and reports,
records the snapshot it saves, meters its time, and gives the door the turn's policy and bindings. Nothing here
contacts a box; boxes call in with their token."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hgsnapshot import Manifest, ManifestError, included_roots

from .. import ids
from ..config import Settings
from ..contract import OutputError, file_ids, outcome, read_line, response
from ..crypto import DoorCA
from ..domain import egress
from ..domain.limits import OrgLimits
from ..domain.manifest import Manifest as PackageManifest
from ..domain.meters import Kind, Meter
from ..domain.provider import WIRES
from ..domain.turn import Reason, Status
from ..errors import Conflict, Forbidden, Invalid, NotFound, TooLarge, Unauthenticated
from ..extensions import Extensions, TurnSummary
from ..limits import BLOB_MAX, INSTALL_ARCHIVE_MAX, LOG_TAIL, MANIFEST_MAX
from ..store.db import Database, Tx, dumps
from ..store.moments import Moments
from ..store.objects import Objects, blob_key, org_key
from .harnesses import archive_key, secret_context
from .installs import Installs
from .installs import archive_key as install_archive_key
from .providers import key_context
from .snapshots import Snapshots
from .traces import Traces
from .turns import completed_items, conclude, history, record_events, request_body
from .usage import Usage
from .vault import Vault

LEASE_S = 60                     # a box is lost when it has not called in for this long
NEXT_WAIT_MAX_S = 30             # a long-poll's longest wait before the runner asks again
OUTCOMES = ("exited", "timed_out", "stopped", "failed")
TOKEN_GRACE_S = 600
log = logging.getLogger("harnessgateway.boxes")


@dataclass(frozen=True)
class After:
    """What to do once the transaction that decided it has committed."""
    launch: str | None = None        # a box id to queue a launch for
    wake: str | None = None          # a session id to wake
    install: str | None = None       # an install id to queue (the package's first install on this sandbox image)


def _tail(previous: str, more: str) -> str:
    return (previous + more)[-LOG_TAIL:]


def _reason(value: object) -> Reason:
    try:
        return Reason(str(value))
    except ValueError:
        raise Invalid(f"unknown reason {value!r}", param="reason") from None


SECRET_REF = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


def _render_mcp(servers: list[dict[str, Any]], placeholders: dict[str, str]) -> list[dict[str, Any]]:
    """The turn's MCP servers with each ${SECRET} replaced by its placeholder; a header naming a secret the
    organization has not set is left out."""
    out = []
    for s in servers:
        headers = {}
        for name, value in (s.get("headers") or {}).items():
            refs = SECRET_REF.findall(value)
            if all(r in placeholders for r in refs):
                headers[name] = SECRET_REF.sub(lambda m: placeholders[m.group(1)], value)
        out.append({**s, "headers": headers})
    return out


class Boxes:
    def __init__(self, db: Database, objects: Objects, moments: Moments, vault: Vault, snapshots: Snapshots,
                 installs: Installs, traces: Traces, usage: Usage, extensions: Extensions, settings: Settings):
        self.db, self.objects, self.moments, self.vault = db, objects, moments, vault
        self.snapshots, self.installs, self.traces, self.usage, self.ext = snapshots, installs, traces, usage, extensions
        self.s = settings
        self.pepper = settings.pepper
        self.ca = DoorCA(settings.secret_key)
        # what a box trusts beyond its image's CAs: the door's, and the operator's when the network re-signs TLS
        self.trust_pem = self.ca.cert_pem.decode() + (Path(settings.ca_file).read_text() if settings.ca_file else "")

    # ── creating boxes (the api, when a turn is queued) ─────────────────────────────────────────────────────────────
    async def ensure(self, tx: Tx, org_id: str, session: dict[str, Any], limits_doc: dict[str, Any]) -> After:
        """A box for the session: wake the live one, or make one when the organization's install is ready and its
        concurrency allows. An install the current sandbox image lacks (a fresh install, an upgraded image) is started
        here. Returns what to do after commit."""
        live = await tx.one("SELECT id, status FROM boxes WHERE session_id = %s AND status <> 'gone'", session["id"])
        if live:
            return After(wake=session["id"])
        install, queued = await self.installs.ensure_in(tx, org_id, session["digest"])
        if install["status"] != "ready":                     # the install's finish admits the pending sessions
            return After(install=install["id"] if queued else None)
        limits = OrgLimits.parse(limits_doc, self.s.defaults)
        if limits.max_concurrent_turns:
            live_boxes = await tx.value("SELECT count(*) FROM boxes WHERE org_id = %s AND kind = 'session' AND status <> 'gone'",
                                        org_id)
            if live_boxes >= limits.max_concurrent_turns:
                return After()                               # a finishing box admits the next session
        box_id = ids.new("box")
        await tx.run("INSERT INTO boxes (id, org_id, kind, session_id) VALUES (%s, %s, 'session', %s)", box_id, org_id,
                     session["id"])
        return After(launch=box_id)

    async def after_commit(self, after: After, org_id: str) -> None:
        if after.launch:
            await self.moments.enqueue("launch", box_id=after.launch)
        if after.wake:
            await self.moments.wake(after.wake)
        if after.install:
            await self.moments.enqueue("install", install_id=after.install, org_id=org_id)

    async def admit_pending(self, org_id: str) -> None:
        """Sessions with a queued turn and no live box get one, within the organization's concurrency: called when a
        box ends, an install becomes ready, or the reconcile duty runs."""
        afters: list[After] = []
        async with self.db.tenant(org_id) as tx:
            org = await tx.get("SELECT limits, status FROM orgs WHERE id = %s", org_id)
            if org["status"] != "active":
                return
            waiting = await tx.all("SELECT DISTINCT s.* FROM sessions s JOIN turns t ON t.session_id = s.id WHERE s.org_id = %s "
                                   "AND t.status = 'queued' AND s.status = 'active' AND NOT EXISTS (SELECT 1 FROM boxes b "
                                   "WHERE b.session_id = s.id AND b.status <> 'gone') ORDER BY s.last_active_at", org_id)
            for session in waiting:
                afters.append(await self.ensure(tx, org_id, session, org["limits"]))
        for after in afters:
            await self.after_commit(after, org_id)

    async def ensure_install_box(self, org_id: str, install_id: str) -> str | None:
        """A box for a pending install: the worker launches it (`to_launch` arms it). None when one is live already or
        the install is not pending."""
        async with self.db.tenant(org_id) as tx:
            install = await tx.one("SELECT * FROM installs WHERE id = %s", install_id)
            if not install or install["status"] != "pending":
                return None
            if await tx.one("SELECT 1 FROM boxes WHERE install_id = %s AND status <> 'gone'", install_id):
                return None
            box_id = ids.new("box")
            await tx.run("INSERT INTO boxes (id, org_id, kind, install_id) VALUES (%s, %s, 'install', %s)", box_id, org_id,
                         install_id)
        return box_id

    # ── the worker's side ───────────────────────────────────────────────────────────────────────────────────────────
    async def to_launch(self, box_id: str) -> dict[str, Any] | None:
        """The box as the worker needs it, armed with a fresh token: its spec and the token. None when it is gone."""
        token = ids.new_secret("box")
        async with self.db.platform() as tx:
            box = await tx.one("SELECT b.*, o.limits FROM boxes b JOIN orgs o ON o.id = b.org_id WHERE b.id = %s FOR UPDATE",
                               box_id)
            if not box or box["status"] == "gone" or box["launcher_ref"]:
                return None
            await tx.run("UPDATE boxes SET token_digest = %s, last_seen_at = now(), expires_at = now() + %s * interval '1 second'"
                         " WHERE id = %s", ids.digest(token, self.pepper), LEASE_S * 5, box_id)
        limits = OrgLimits.parse(box["limits"], self.s.defaults)
        return {"id": box_id, "org_id": box["org_id"], "kind": box["kind"], "token": token, "limits": limits}

    async def launched(self, box_id: str, reference: str) -> None:
        async with self.db.platform() as tx:
            await tx.run("UPDATE boxes SET launcher_ref = %s, launched_at = now() WHERE id = %s", reference, box_id)

    async def launch_failed(self, box_id: str, message: str) -> None:
        """The substrate would not start the box: the box is gone and what waited on it fails."""
        await self._gone(box_id, f"launch failed: {message}", Reason.SANDBOX_FAILED, message)

    async def stale_launches(self) -> list[dict[str, Any]]:
        async with self.db.platform() as tx:
            return await tx.all("SELECT id FROM boxes WHERE status = 'starting' AND launcher_ref IS NULL AND created_at < "
                                "now() - interval '1 minute'")

    async def to_stop(self, box_id: str) -> str | None:
        async with self.db.platform() as tx:
            return await tx.value("SELECT launcher_ref FROM boxes WHERE id = %s", box_id)

    # ── a box's own calls ───────────────────────────────────────────────────────────────────────────────────────────
    async def authenticate(self, box_id: str, token: str) -> dict[str, Any]:
        """The live box this token names, with its lease renewed."""
        if not token.startswith(ids.SECRET["box"]):
            raise Unauthenticated("a box token is required")
        async with self.db.platform() as tx:
            box = await tx.one("UPDATE boxes SET last_seen_at = now(), expires_at = now() + %s * interval '1 second' "
                               "WHERE id = %s AND token_digest = %s AND status <> 'gone' RETURNING *", LEASE_S,
                               ids.check(box_id, "box"), ids.digest(token, self.pepper))
        if not box:
            raise Unauthenticated("unknown box, or a box that is gone")
        return box

    async def info(self, box: dict[str, Any]) -> dict[str, Any]:
        """What the runner needs to set itself up: the package, the install archive, the snapshot, the trust."""
        async with self.db.tenant(box["org_id"]) as tx:
            if box["kind"] == "install":
                install = await tx.get("SELECT * FROM installs WHERE id = %s", box["install_id"])
                package = await tx.get("SELECT * FROM packages WHERE digest = %s", install["digest"])
                manifest = PackageManifest.model_validate(package["manifest"])
                org = await tx.get("SELECT limits FROM orgs WHERE id = %s", box["org_id"])
                limits = OrgLimits.parse(org["limits"], self.s.defaults)
                return {"id": box["id"], "kind": "install", "org_id": box["org_id"], "package": {"digest": install["digest"],
                        "key": archive_key(install["digest"]), "manifest": manifest.model_dump()},
                        "install": {"id": install["id"], "timeout_s": manifest.install.timeout_s},
                        "ca_pem": self.trust_pem, "tmp_mb": limits.tmp_mb}
            session = await tx.get("SELECT * FROM sessions WHERE id = %s", box["session_id"])
            current = await self.installs.current(tx, box["org_id"], session["digest"])
            org = await tx.get("SELECT limits FROM orgs WHERE id = %s", box["org_id"])
            snapshot = await tx.one("SELECT manifest_key FROM snapshots WHERE id = %s", session["snapshot_id"]) \
                if session["snapshot_id"] else None
        if not current or current["status"] != "ready":
            raise Conflict("the harness's install is not ready")
        manifest = PackageManifest.model_validate(session["manifest"])
        limits = OrgLimits.parse(org["limits"], self.s.defaults)
        return {"id": box["id"], "kind": "session", "org_id": box["org_id"], "session_id": session["id"],
                "harness": session["harness"], "new_session": session["snapshot_id"] is None,
                "package": {"digest": session["digest"], "key": archive_key(session["digest"]),
                            "manifest": manifest.model_dump()},
                "install": {"id": current["id"], "key": current["object_key"]},
                "snapshot": {"key": snapshot["manifest_key"]} if snapshot else None,
                "roots": included_roots(manifest.persist), "standby_s": limits.standby_s, "tmp_mb": limits.tmp_mb,
                "ca_pem": self.trust_pem}

    async def ready(self, box: dict[str, Any]) -> None:
        async with self.db.platform() as tx:
            await tx.run("UPDATE boxes SET status = 'standby', ready_at = coalesce(ready_at, now()), idle_since = now() "
                         "WHERE id = %s AND status = 'starting'", box["id"])

    async def next(self, box: dict[str, Any], wait_s: float) -> dict[str, Any] | None:
        """The session's next queued turn as a job, waiting up to `wait_s` for one to arrive; None when none came."""
        assert box["kind"] == "session"
        deadline = time.monotonic() + min(max(wait_s, 0), NEXT_WAIT_MAX_S)
        while True:
            job = await self._take(box)
            if job is not None:
                return job
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            await self.moments.wait_wake(box["session_id"], min(left, 5.0))
            async with self.db.platform() as tx:          # the lease, renewed while the runner waits here
                await tx.run("UPDATE boxes SET last_seen_at = now(), expires_at = now() + %s * interval '1 second' WHERE id = %s",
                             LEASE_S, box["id"])

    async def _take(self, box: dict[str, Any]) -> dict[str, Any] | None:
        org_id = box["org_id"]
        ended: list[TurnSummary] = []
        async with self.db.tenant(org_id) as tx:
            turn = await tx.one("SELECT * FROM turns WHERE session_id = %s AND status = 'queued' ORDER BY seq LIMIT 1 FOR UPDATE",
                                box["session_id"])
            if not turn:
                return None
            if await tx.value("SELECT status FROM orgs WHERE id = %s", org_id) != "active":
                summary, events = await conclude(tx, self.objects, turn, Status.FAILED, reason=Reason.SUSPENDED,
                                                 message="the organization is suspended")
                ended.append(summary)
                await self._after_fail(turn["id"], events, ended)
                return None
            b = await tx.get("SELECT * FROM boxes WHERE id = %s FOR UPDATE", box["id"])
            if b["status"] == "gone":
                return None
            idle_from = b["idle_since"]
            await tx.run("UPDATE turns SET status = 'in_progress', box_id = %s, started_at = now() WHERE id = %s",
                         box["id"], turn["id"])
            await tx.run("UPDATE boxes SET status = 'running', turn_id = %s, policy_version = policy_version + 1, "
                         "idle_since = NULL WHERE id = %s", turn["id"], box["id"])
            turn = await tx.get("SELECT * FROM turns WHERE id = %s", turn["id"])
            job, events = await self._job(tx, box, turn)
        await self.moments.publish(turn["id"], events)
        if idle_from is not None:
            await self._meter_span(org_id, box, Kind.STANDBY, idle_from, datetime.now(UTC), session_id=box["session_id"])
        return job

    async def _after_fail(self, rid: str, events: list[dict[str, Any]], ended: list[TurnSummary]) -> None:
        await self.moments.publish(rid, events)
        await self.moments.end_stream(rid)
        for e in ended:
            await self.ext.turn_ended(e)
        return None

    async def _job(self, tx: Tx, box: dict[str, Any], turn: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        org_id, rid = box["org_id"], turn["id"]
        session = await tx.get("SELECT * FROM sessions WHERE id = %s", turn["session_id"])
        org = await tx.get("SELECT egress, limits FROM orgs WHERE id = %s", org_id)
        harness = await tx.one("SELECT settings, secrets FROM harnesses WHERE org_id = %s AND name = %s",
                               org_id, session["harness"]) or {"settings": {}, "secrets": {}}
        manifest = PackageManifest.model_validate(session["manifest"])
        limits = OrgLimits.parse(org["limits"], self.s.defaults)
        body = await request_body(self.objects, turn)
        partial = response(rid=rid, body=body, created=turn["created_at"].timestamp(), status="in_progress",
                           metadata={"session_id": session["id"]})
        events = await record_events(tx, turn, [{"type": "response.in_progress", "response": partial}])
        bounds = [b for b in (manifest.run.timeout_s, limits.turn_timeout_s) if b]
        timeout = min(bounds) if bounds else None
        provider = None
        if turn["provider_id"]:
            p = await tx.get("SELECT id, api, base_url FROM providers WHERE id = %s", turn["provider_id"])
            provider = {"api": p["api"], "url": p["base_url"], "token": ids.new_secret("placeholder")}
        settings = {k: harness["settings"].get(k, s.default) for k, s in manifest.settings.items()
                    if k in harness["settings"] or s.default is not None}
        placeholders = {k: ids.new_secret("placeholder") for k in manifest.secrets if k in harness["secrets"]}
        files = await tx.all("SELECT id, filename, object_key FROM files WHERE id = ANY(%s) AND deleted_at IS NULL",
                             file_ids(body.get("input")))
        ops = await tx.all("SELECT id, op, path, object_key FROM container_ops WHERE session_id = %s AND applied_at IS NULL "
                           "ORDER BY created_at", session["id"])
        history_key = org_key(org_id, "turns", rid, "history.json")
        await self.objects.put(history_key, dumps(await history(tx, self.objects, session, turn["seq"])).encode(),
                               org_id=org_id, kind="request")
        await self._bind(tx, org_id, rid, placeholders, provider)
        return {
            "turn_id": rid, "seq": turn["seq"], "harness": session["harness"], "model": turn["provider_model"],
            "store": session["store"], "timeout_s": timeout, "stop_grace_s": manifest.run.stop_grace_s,
            "observability": limits.observability, "request_key": turn["request_key"], "history_key": history_key,
            "env": {**settings, **placeholders}, "mcp": _render_mcp(turn["mcp"], placeholders), "provider": provider,
            "files": [{"id": f["id"], "filename": f["filename"], "key": f["object_key"]} for f in files],
            "ops": [{"id": op["id"], "op": op["op"], "path": op["path"], "key": op["object_key"]} for op in ops],
            "roots": included_roots(manifest.persist),
        }, events

    async def _bind(self, tx: Tx, org_id: str, rid: str, placeholders: dict[str, str], provider: dict[str, Any] | None) -> None:
        """The turn's bindings for the door, sealed: the caller's (written when the turn was created, a list) and the
        platform's (the harness secrets and the provider key this turn stands in for, by placeholder)."""
        context = f"turn/{rid}/bindings"
        row = await tx.one("SELECT sealed FROM turn_bindings WHERE turn_id = %s", rid)
        caller = json.loads(await self.vault.open(org_id, context, row["sealed"])) if row else []
        platform: list[dict[str, Any]] = [{"placeholder": v, "secret": k} for k, v in placeholders.items()]
        if provider:
            platform.append({"placeholder": provider["token"], "provider": True})
        sealed = await self.vault.seal(org_id, context, dumps({"caller": caller, "platform": platform}))
        await tx.run("INSERT INTO turn_bindings (turn_id, org_id, sealed) VALUES (%s, %s, %s) ON CONFLICT (turn_id) DO UPDATE "
                     "SET sealed = EXCLUDED.sealed", rid, org_id, sealed)

    async def heartbeat(self, box: dict[str, Any], rid: str) -> dict[str, Any]:
        async with self.db.platform() as tx:
            turn = await tx.one("SELECT status, cancel FROM turns WHERE id = %s AND box_id = %s",
                                ids.check(rid, "turn"), box["id"])
        if not turn:
            raise Conflict(f"{rid} is not this box's turn")
        return {"cancel": turn["cancel"] is not None or turn["status"] != Status.IN_PROGRESS,
                "stop": turn["cancel"] == Reason.INVALID_OUTPUT.value}

    async def events(self, box: dict[str, Any], rid: str, batch: int, lines: list[str]) -> dict[str, Any]:
        """One batch of whole stdout lines, applied once. The reply says whether the turn has concluded and whether the
        box must stop the process (its output broke the schema)."""
        ended: TurnSummary | None = None
        failure = None
        published: list[dict[str, Any]] = []
        async with self.db.platform() as tx:
            turn = await self._turn(tx, box, rid)
            if turn["status"] != Status.IN_PROGRESS or batch <= turn["last_batch"]:
                return {"concluded": Status(turn["status"]).final, "stop": False}
            events: list[dict[str, Any]] = []
            logs: list[str] = []
            terminal = None
            for text in lines:
                try:
                    line = read_line(text)
                except OutputError as e:
                    failure = str(e)
                    break
                if line.kind == "event":
                    events.append(line.event)
                elif line.kind == "log":
                    logs.append(text + "\n")
                elif line.kind == "terminal":
                    terminal = line.event
                    break
            published += await record_events(tx, turn, events)
            await tx.run("UPDATE turns SET last_batch = %s, log_tail = %s WHERE id = %s",
                         batch, _tail(turn["log_tail"], "".join(logs)), rid)
            if failure is not None:
                await tx.run("UPDATE turns SET cancel = coalesce(cancel, %s) WHERE id = %s", Reason.INVALID_OUTPUT.value, rid)
                ended, more = await conclude(tx, self.objects, turn, Status.FAILED, reason=Reason.INVALID_OUTPUT, message=failure)
                published += more
            elif terminal is not None and not turn["cancel"]:
                try:
                    o = outcome(terminal, await completed_items(tx, turn["id"]))
                    ended, more = await conclude(tx, self.objects, turn, Status(o.status), output=o.output, usage=o.usage,
                                                 error=o.error, incomplete=o.incomplete, metadata=o.metadata)
                except OutputError as e:
                    ended, more = await conclude(tx, self.objects, turn, Status.FAILED, reason=Reason.INVALID_OUTPUT,
                                                 message=str(e))
                published += more
        await self.moments.publish(rid, published)
        if ended:
            await self.moments.end_stream(rid)
            await self.ext.turn_ended(ended)
        return {"concluded": ended is not None, "stop": failure is not None}

    @staticmethod
    async def _turn(tx: Tx, box: dict[str, Any], rid: str) -> dict[str, Any]:
        turn = await tx.one("SELECT * FROM turns WHERE id = %s FOR UPDATE", ids.check(rid, "turn"))
        if not turn:
            raise NotFound(f"no turn {rid}")
        if turn["box_id"] != box["id"]:
            raise Conflict(f"{rid} is not this box's turn")
        return turn

    async def finish(self, box: dict[str, Any], rid: str, report: dict[str, Any]) -> dict[str, Any]:
        """The process is gone. Report: outcome (exited | timed_out | stopped | failed), exit_code, stderr_tail, reason
        and error (when failed), applied_ops, cpu_seconds, memory_peak_mb, stdout_bytes, stderr_bytes, logs (whether
        it uploaded full logs). The reply says whether to snapshot."""
        outcome_name = report.get("outcome")
        if outcome_name not in OUTCOMES:
            raise Invalid(f"outcome is one of {', '.join(OUTCOMES)}", param="outcome")
        ended: TurnSummary | None = None
        events: list[dict[str, Any]] = []
        async with self.db.platform() as tx:
            turn = await self._turn(tx, box, rid)
            stderr = str(report.get("stderr_tail") or "")[-LOG_TAIL:]
            if not Status(turn["status"]).final:
                ended, events = await self._conclude_exit(tx, turn, str(outcome_name), report, stderr)
            await tx.run("UPDATE turns SET exit_code = %s, stderr_tail = %s, exited_at = now() WHERE id = %s",
                         report.get("exit_code") if isinstance(report.get("exit_code"), int) else None, stderr, rid)
            await tx.run("DELETE FROM turn_bindings WHERE turn_id = %s", rid)
            applied = [o for o in report.get("applied_ops") or [] if isinstance(o, str)]
            if applied:
                await tx.run("UPDATE container_ops SET applied_at = now() WHERE id = ANY(%s) AND session_id = %s",
                             applied, turn["session_id"])
            await tx.run("UPDATE boxes SET status = 'standby', turn_id = NULL, policy_version = policy_version + 1, "
                         "idle_since = now() WHERE id = %s", box["id"])
            session = await tx.get("SELECT store, status, harness, snapshot_id FROM sessions WHERE id = %s", turn["session_id"])
            final = await tx.get("SELECT * FROM turns WHERE id = %s", rid)
        await self.moments.publish(rid, events)
        if ended:
            await self.moments.end_stream(rid)
        await self._meter_turn(box, final, report)
        await self._harness_trace(final, session["harness"], str(outcome_name), report)
        if ended:
            await self.ext.turn_ended(ended)
        persist = bool(session["store"] and session["status"] == "active")
        if not persist:
            await self._checkpoint_unchanged(box, rid)
        return {"persist": persist}

    async def _conclude_exit(self, tx: Tx, turn: dict[str, Any], outcome_name: str, report: dict[str, Any], stderr: str
                             ) -> tuple[TurnSummary, list[dict[str, Any]]]:
        """A turn whose process ended without a terminal event."""
        if outcome_name == "failed":
            return await conclude(tx, self.objects, turn, Status.FAILED, reason=_reason(report.get("reason")),
                                  message=str(report.get("error") or "")[:4000])
        items = await completed_items(tx, turn["id"])
        if turn["cancel"]:
            reason = _reason(turn["cancel"])
            status = Status.FAILED if reason == Reason.INVALID_OUTPUT else Status.CANCELLED
            return await conclude(tx, self.objects, turn, status, reason=reason, output=items)
        if outcome_name == "timed_out":
            return await conclude(tx, self.objects, turn, Status.FAILED, reason=Reason.TIMED_OUT, output=items,
                                  message="the turn ran past its time limit")
        last = " | ".join(stderr.strip().splitlines()[-3:])
        return await conclude(tx, self.objects, turn, Status.FAILED, reason=Reason.EXITED_WITHOUT_RESULT, output=items,
                              message=f"run exited {report.get('exit_code')} without a terminal event"
                                      + (f"; stderr: {last[-1000:]}" if last else ""))

    async def _harness_trace(self, turn: dict[str, Any], harness: str, outcome_name: str, report: dict[str, Any]) -> None:
        def at(v: Any) -> float | None:
            return v.timestamp() if v else None
        data: dict[str, Any] = {
            "harness": harness, "session_id": turn["session_id"], "status": turn["status"], "hg_reason": turn["hg_reason"],
            "outcome": outcome_name, "exit_code": turn["exit_code"], "created_at": at(turn["created_at"]),
            "started_at": at(turn["started_at"]), "ended_at": at(turn["ended_at"]), "exited_at": at(turn["exited_at"]),
            "events": turn["events"], "stdout_bytes": report.get("stdout_bytes"), "stderr_bytes": report.get("stderr_bytes"),
            "cpu_seconds": report.get("cpu_seconds"), "memory_peak_mb": report.get("memory_peak_mb"),
            "request_key": turn["request_key"]}
        if report.get("logs"):
            prefix = org_key(turn["org_id"], "turns", turn["id"], "")
            data.update(stdout_key=prefix + "stdout.log", stderr_key=prefix + "stderr.log")
        else:
            data.update(log_tail=turn["log_tail"], stderr_tail=turn["stderr_tail"])
        try:
            await self.traces.write(turn["org_id"], turn["id"], "harness", data)
        except Exception:
            log.exception("recording the trace of %s failed", turn["id"])

    # ── snapshots ───────────────────────────────────────────────────────────────────────────────────────────────────
    async def blobs_missing(self, box: dict[str, Any], hashes: object) -> list[str]:
        if not isinstance(hashes, list) or not all(isinstance(h, str) and len(h) == 64 for h in hashes) or len(hashes) > 100_000:
            raise Invalid("hashes is a list of at most 100000 sha256 hex digests", param="hashes")
        return await self.snapshots.missing(box["org_id"], hashes)

    async def put_blob(self, box: dict[str, Any], digest: str, chunks: AsyncIterator[bytes]) -> int:
        """A blob's bytes, verified against its hash as they arrive, stored and recorded. Its size."""
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise Invalid("a blob is named by its sha256 hex digest", param="hash")
        h = hashlib.sha256()
        size = 0
        with tempfile.NamedTemporaryFile(prefix="hg-blob-") as tmp:
            async for chunk in chunks:
                size += len(chunk)
                if size > BLOB_MAX:
                    raise TooLarge(f"a blob is at most {BLOB_MAX >> 30} GiB")
                h.update(chunk)
                tmp.write(chunk)
            if h.hexdigest() != digest:
                raise Invalid("the bytes do not match the hash", param="hash")
            tmp.flush()
            await self.objects.store.put_file(blob_key(box["org_id"], digest), Path(tmp.name))
        await self.snapshots.record_blob(box["org_id"], digest, size)
        return size

    async def put_snapshot(self, box: dict[str, Any], rid: str, text: str) -> dict[str, Any]:
        """The manifest of the state after `rid`: recorded as the turn's checkpoint and the session's current state."""
        if len(text) > MANIFEST_MAX:
            raise TooLarge(f"a manifest is at most {MANIFEST_MAX >> 20} MiB")
        try:
            manifest = Manifest.loads(text)
        except ManifestError as e:
            raise Invalid(f"manifest: {e}", param="manifest") from None
        org_id = box["org_id"]
        async with self.db.tenant(org_id) as tx:
            turn = await self._turn(tx, box, rid)
            made = await self.snapshots.create(tx, org_id, turn["session_id"], rid, manifest)
            await self.snapshots.store_manifest(org_id, made["key"], manifest)
            await tx.run("UPDATE turns SET snapshot_id = %s WHERE id = %s", made["id"], rid)
            await tx.run("UPDATE sessions SET snapshot_id = %s, snapshot_seq = %s WHERE id = %s AND status = 'active' AND "
                         "snapshot_seq <= %s", made["id"], turn["seq"], turn["session_id"], turn["seq"])
        return {"snapshot_id": made["id"], "bytes": made["bytes"], "files": made["files"]}

    async def saved(self, box: dict[str, Any], rid: str, report: dict[str, Any]) -> None:
        """Nothing new to store: the state after the turn is the session's last snapshot (or the save failed, which
        is logged and leaves the session's snapshot as it was)."""
        if report.get("error"):
            log.warning("saving the state of %s after %s failed: %s", box["session_id"], rid, str(report["error"])[:500])
            return
        await self._checkpoint_unchanged(box, rid)

    async def _checkpoint_unchanged(self, box: dict[str, Any], rid: str) -> None:
        async with self.db.platform() as tx:
            await tx.run("UPDATE turns t SET snapshot_id = s.snapshot_id FROM sessions s WHERE t.id = %s AND s.id = t.session_id "
                         "AND t.box_id = %s AND t.snapshot_id IS NULL", rid, box["id"])

    def blob_stream(self, box: dict[str, Any], digest: str) -> AsyncIterator[bytes]:
        """A blob of the box's organization (the restore of a snapshot)."""
        return self.snapshots.blob_stream(box["org_id"], digest)

    # ── objects a box may read, logs it writes ──────────────────────────────────────────────────────────────────────
    async def object_stream(self, box: dict[str, Any], key: str) -> AsyncIterator[bytes]:
        """What a box may read: package archives; and for a session box, its organization's install archives and
        uploaded files, its own session's objects, and its own session's turns' objects. Anything else is refused."""
        if ".." in key or "\0" in key:
            raise Forbidden("not this box's object")
        org = org_key(box["org_id"], "")
        allowed = key.startswith("packages/")
        if box["kind"] == "session" and key.startswith(org):
            rest = key[len(org):]
            allowed = rest.startswith(("installs/", "files/", f"sessions/{box['session_id']}/"))
            if not allowed and rest.startswith("turns/"):
                rid = rest.split("/", 2)[1]
                async with self.db.platform() as tx:
                    allowed = await tx.one("SELECT 1 FROM turns WHERE id = %s AND session_id = %s", rid,
                                           box["session_id"]) is not None
        if not allowed:
            raise Forbidden("not this box's object")
        return self.objects.stream(key)

    async def put_log(self, box: dict[str, Any], rid: str, name: str, chunks: AsyncIterator[bytes]) -> None:
        if name not in ("stdout", "stderr"):
            raise Invalid("a log is stdout or stderr", param="name")
        async with self.db.platform() as tx:
            await self._turn(tx, box, rid)
        key = org_key(box["org_id"], "turns", rid, f"{name}.log")
        with tempfile.NamedTemporaryFile(prefix="hg-log-") as tmp:
            async for chunk in chunks:
                tmp.write(chunk)
            tmp.flush()
            await self.objects.put_file(key, Path(tmp.name), org_id=box["org_id"], kind="log")

    # ── installs ────────────────────────────────────────────────────────────────────────────────────────────────────
    async def put_archive(self, box: dict[str, Any], chunks: AsyncIterator[bytes]) -> int:
        assert box["kind"] == "install"
        async with self.db.tenant(box["org_id"]) as tx:
            install = await tx.get("SELECT * FROM installs WHERE id = %s", box["install_id"])
        key = install_archive_key(box["org_id"], install["digest"], self.s.sandbox_image)
        size = 0
        with tempfile.NamedTemporaryFile(prefix="hg-install-") as tmp:
            async for chunk in chunks:
                size += len(chunk)
                if size > INSTALL_ARCHIVE_MAX:
                    raise TooLarge("the install archive is too large")
                tmp.write(chunk)
            tmp.flush()
            await self.objects.store.put_file(key, Path(tmp.name))
        return size

    async def installed(self, box: dict[str, Any], report: dict[str, Any]) -> None:
        assert box["kind"] == "install"
        ok = bool(report.get("ok"))
        size = int(report.get("bytes") or 0)
        await self.installs.finish(box["org_id"], box["install_id"], ok=ok, size=size, log_tail=str(report.get("log_tail") or ""))
        async with self.db.platform() as tx:
            b = await tx.get("SELECT * FROM boxes WHERE id = %s", box["id"])
            await tx.run("UPDATE boxes SET status = 'gone', gone_at = now(), gone_reason = 'installed' WHERE id = %s", box["id"])
        start = b["ready_at"] or b["launched_at"] or b["created_at"]
        await self._meter_span(box["org_id"], b, Kind.INSTALL, start, datetime.now(UTC), cpu=report.get("cpu_seconds"))
        if ok:
            await self.admit_pending(box["org_id"])
        else:
            await self._fail_waiting(box["org_id"], b, Reason.INSTALL_FAILED, str(report.get("log_tail") or "")[-2000:])
        await self.moments.enqueue("stop", box_id=box["id"])

    # ── the end of a box ────────────────────────────────────────────────────────────────────────────────────────────
    async def bye(self, box: dict[str, Any]) -> None:
        await self._gone(box["id"], "bye", None, "")

    async def _gone(self, box_id: str, why: str, reason: Reason | None, message: str) -> None:
        """A box is over: its running turn fails (when `reason` is given), standby time is metered, the launcher
        stops what is left, and the organization's pending sessions are admitted."""
        ended: list[TurnSummary] = []
        published: list[tuple[str, list[dict[str, Any]]]] = []
        async with self.db.platform() as tx:
            b = await tx.one("SELECT * FROM boxes WHERE id = %s AND status <> 'gone' FOR UPDATE", box_id)
            if not b:
                return
            await tx.run("UPDATE boxes SET status = 'gone', gone_at = now(), gone_reason = %s, turn_id = NULL WHERE id = %s",
                         why[:200], box_id)
            if b["turn_id"]:
                turn = await tx.one("SELECT * FROM turns WHERE id = %s FOR UPDATE", b["turn_id"])
                if turn and not Status(turn["status"]).final:
                    summary, events = await conclude(tx, self.objects, turn, Status.FAILED, reason=reason or Reason.BOX_LOST,
                                                     message=message or "the sandbox running this turn is gone")
                    ended.append(summary)
                    published.append((turn["id"], events))
                    await tx.run("DELETE FROM turn_bindings WHERE turn_id = %s", turn["id"])
        for rid, events in published:
            await self.moments.publish(rid, events)
            await self.moments.end_stream(rid)
        for e in ended:
            await self.ext.turn_ended(e)
        if b["idle_since"] is not None:
            await self._meter_span(b["org_id"], b, Kind.STANDBY, b["idle_since"], datetime.now(UTC), session_id=b["session_id"])
        if reason is not None and b["kind"] == "session":
            await self._fail_waiting(b["org_id"], b, reason, message)
        if b["launcher_ref"]:
            await self.moments.enqueue("stop", box_id=box_id)
        await self.admit_pending(b["org_id"])

    async def _fail_waiting(self, org_id: str, box: dict[str, Any], reason: Reason, message: str) -> None:
        """Queued turns that waited on this box (its session's, or every session of a failed install) end failed."""
        ended: list[TurnSummary] = []
        published: list[tuple[str, list[dict[str, Any]]]] = []
        async with self.db.tenant(org_id) as tx:
            if box["kind"] == "session":
                rows = await tx.all("SELECT * FROM turns WHERE session_id = %s AND status = 'queued' FOR UPDATE",
                                    box["session_id"])
            else:
                install = await tx.get("SELECT digest FROM installs WHERE id = %s", box["install_id"])
                rows = await tx.all("SELECT t.* FROM turns t JOIN sessions s ON s.id = t.session_id WHERE t.status = 'queued' "
                                    "AND s.digest = %s AND s.org_id = %s FOR UPDATE OF t", install["digest"], org_id)
            for turn in rows:
                summary, events = await conclude(tx, self.objects, turn, Status.FAILED, reason=reason, message=message)
                ended.append(summary)
                published.append((turn["id"], events))
        for rid, events in published:
            await self.moments.publish(rid, events)
            await self.moments.end_stream(rid)
        for e in ended:
            await self.ext.turn_ended(e)

    async def lost(self) -> int:
        """Boxes whose lease expired: gone, their turns failed. How many."""
        async with self.db.platform() as tx:
            rows = await tx.all("SELECT id FROM boxes WHERE status <> 'gone' AND expires_at < now()")
        for r in rows:
            await self._gone(r["id"], "lease expired", Reason.BOX_LOST, "the sandbox running this turn stopped answering")
        return len(rows)

    async def end_for_session(self, sid: str) -> None:
        """The session is deleted: its live box, if any, is over."""
        async with self.db.platform() as tx:
            box_id = await tx.value("SELECT id FROM boxes WHERE session_id = %s AND status <> 'gone'", sid)
        if box_id:
            await self._gone(box_id, "session deleted", None, "")

    async def stop_requested(self, box_id: str, actor: str) -> dict[str, Any]:
        """An operator ends a box now (a stuck session, an abusive turn)."""
        await self._gone(ids.check(box_id, "box"), f"stopped by {actor}", Reason.CANCELLED,
                         "the sandbox was stopped by an operator")
        return {"id": box_id, "object": "box", "status": "gone"}

    # ── metering ────────────────────────────────────────────────────────────────────────────────────────────────────
    async def _meter_turn(self, box: dict[str, Any], turn: dict[str, Any], report: dict[str, Any]) -> None:
        start, end = turn["started_at"], turn["exited_at"] or turn["ended_at"] or datetime.now(UTC)
        if start is None:
            return
        await self._meter_span(box["org_id"], box, Kind.TURN, start, end, session_id=turn["session_id"], response_id=turn["id"],
                               harness=None, cpu=report.get("cpu_seconds"))

    async def _meter_span(self, org_id: str, box: dict[str, Any], kind: Kind, start: datetime, end: datetime, *,
                          session_id: str | None = None, response_id: str | None = None, harness: str | None = None,
                          cpu: Any = None) -> None:
        seconds = max((end - start).total_seconds(), 0.0)
        async with self.db.tenant(org_id) as tx:
            limits = OrgLimits.parse((await tx.get("SELECT limits FROM orgs WHERE id = %s", org_id))["limits"], self.s.defaults)
            if harness is None and session_id:
                harness = await tx.value("SELECT harness FROM sessions WHERE id = %s", session_id)
        base = {"org_id": org_id, "kind": kind.value, "harness": harness, "session_id": session_id, "response_id": response_id,
                "start": start.timestamp(), "end": end.timestamp()}
        records = [{**base, "id": ids.new("usage"), "meter": Meter.AGENT_SECONDS.value, "quantity": seconds},
                   {**base, "id": ids.new("usage"), "meter": Meter.MEMORY_GIB_SECONDS.value,
                    "quantity": (limits.memory_mb + limits.tmp_mb) / 1024 * seconds}]
        if isinstance(cpu, int | float) and cpu > 0:
            records.append({**base, "id": ids.new("usage"), "meter": Meter.CPU_SECONDS.value, "quantity": float(cpu)})
        await self.usage.record(records)

    # ── the door's questions ────────────────────────────────────────────────────────────────────────────────────────
    async def policy(self, token: str) -> dict[str, Any]:
        """The policy for a box token: what the box may reach now, and the bindings of its running turn."""
        if not token.startswith(ids.SECRET["box"]):
            raise Unauthenticated("a box token")
        async with self.db.platform() as tx:
            box = await tx.one("SELECT * FROM boxes WHERE token_digest = %s AND status <> 'gone'", ids.digest(token, self.pepper))
            if not box:
                raise NotFound("no live box holds this token")
            org = await tx.get("SELECT egress FROM orgs WHERE id = %s", box["org_id"])
            if box["kind"] == "install":
                install = await tx.get("SELECT digest FROM installs WHERE id = %s", box["install_id"])
                package = await tx.get("SELECT manifest FROM packages WHERE digest = %s", install["digest"])
                manifest = PackageManifest.model_validate(package["manifest"])
                allow = egress.Policy.of(org["egress"], manifest.install.egress).entries()
                return {"version": box["policy_version"], "org_id": box["org_id"], "turn_id": None, "allow": allow,
                        "bindings": [], "platform": self.s.platform_hosts}
            session = await tx.get("SELECT manifest, harness FROM sessions WHERE id = %s", box["session_id"])
            manifest = PackageManifest.model_validate(session["manifest"])
            turn = await tx.one("SELECT * FROM turns WHERE id = %s", box["turn_id"]) if box["turn_id"] else None
            if not turn or turn["status"] != Status.IN_PROGRESS:
                return {"version": box["policy_version"], "org_id": box["org_id"], "turn_id": None, "allow": [],
                        "bindings": [], "platform": self.s.platform_hosts}
            harness = await tx.one("SELECT secrets FROM harnesses WHERE org_id = %s AND name = %s",
                                   box["org_id"], session["harness"])
            provider = await tx.one("SELECT id, api, base_url, sealed_key FROM providers WHERE id = %s", turn["provider_id"]) \
                if turn["provider_id"] else None
            sealed = await tx.one("SELECT sealed FROM turn_bindings WHERE turn_id = %s", turn["id"])
        doc = json.loads(await self.vault.open(box["org_id"], f"turn/{turn['id']}/bindings", sealed["sealed"])) if sealed else {}
        bindings: list[dict[str, Any]] = list(doc.get("caller") or [])
        provider_host: list[str] = []
        for b in doc.get("platform") or []:
            if b.get("provider") and provider:
                wire = WIRES[provider["api"]]
                key = await self.vault.open(box["org_id"], key_context(provider["id"]), provider["sealed_key"])
                host = _host_entry(provider["base_url"])
                provider_host = [host]
                # the bare key: the CLI already wrote the header's shape around the placeholder ("Bearer <token>")
                bindings.append({"placeholder": b["placeholder"], "value": key, "hosts": [host], "header": wire.header})
            elif b.get("secret") and harness and b["secret"] in harness["secrets"]:
                spec = manifest.secrets.get(b["secret"])
                hosts = set(spec.hosts if spec else []) | {s.host() for s in manifest.mcp.values()
                                                            if b["secret"] in s.secret_refs()}
                bindings.append({"placeholder": b["placeholder"], "hosts": sorted(hosts),
                                 "value": await self.vault.open(box["org_id"], secret_context(session["harness"], b["secret"]),
                                                                harness["secrets"][b["secret"]])})
        allow = egress.Policy.of(org["egress"], manifest.run_entries(), provider_host).entries()
        return {"version": box["policy_version"], "org_id": box["org_id"], "turn_id": turn["id"], "allow": allow,
                "bindings": bindings, "platform": self.s.platform_hosts}

    async def door_records(self, token: str, records: list[dict[str, Any]]) -> None:
        """Connection records of one box, batched by the door: bytes into the ledger, the records into the turn's
        trace (or the box's standby, which has no turn and keeps only the bytes)."""
        async with self.db.platform() as tx:
            box = await tx.one("SELECT * FROM boxes WHERE token_digest = %s", ids.digest(token, self.pepper))
        if not box:
            return
        by_turn: dict[str | None, list[dict[str, Any]]] = {}
        for r in records[:10_000]:
            if isinstance(r, dict):
                by_turn.setdefault(r.get("turn") or None, []).append(r)
        now = time.time()
        usage = []
        for rid, recs in by_turn.items():
            out, inn = sum(int(r.get("out", 0)) for r in recs), sum(int(r.get("in", 0)) for r in recs)
            kind = Kind.TURN if rid else (Kind.INSTALL if box["kind"] == "install" else Kind.STANDBY)
            base = {"org_id": box["org_id"], "kind": kind.value, "session_id": box["session_id"], "response_id": rid,
                    "start": min(float(r.get("at", now)) for r in recs), "end": now}
            for meter, q in ((Meter.EGRESS_BYTES, out), (Meter.INGRESS_BYTES, inn)):
                if q > 0:
                    usage.append({**base, "id": ids.new("usage"), "meter": meter.value, "quantity": float(q)})
            if rid:
                clean = [{k: v for k, v in r.items() if k != "turn"} for r in recs]
                try:
                    await self.traces.write(box["org_id"], rid, "door", {"connections": clean})
                    for r in clean:
                        if r.get("intercepted") and r.get("method"):
                            await self.traces.write(box["org_id"], rid, "provider", {k: r[k] for k in r if k in
                                                    ("host", "port", "method", "path", "status", "out", "in", "s", "at")})
                except Exception:
                    log.exception("recording door records of %s failed", rid)
        if usage:
            await self.usage.record(usage)

    # ── listing (the platform scope) ────────────────────────────────────────────────────────────────────────────────
    async def listing(self) -> list[dict[str, Any]]:
        async with self.db.platform() as tx:
            rows = await tx.all("SELECT b.*, s.harness FROM boxes b LEFT JOIN sessions s ON s.id = b.session_id "
                                "WHERE b.status <> 'gone' ORDER BY b.created_at")
        return [{"id": r["id"], "object": "box", "org_id": r["org_id"], "kind": r["kind"], "session_id": r["session_id"],
                 "install_id": r["install_id"], "harness": r["harness"], "status": r["status"], "turn_id": r["turn_id"],
                 "launcher_ref": r["launcher_ref"], "created_at": int(r["created_at"].timestamp()),
                 "last_seen_at": int(r["last_seen_at"].timestamp())} for r in rows]



def _host_entry(base_url: str) -> str:
    u = urlsplit(base_url)
    assert u.hostname
    return egress.host_entry(u.hostname, u.port or (443 if u.scheme == "https" else 80))
