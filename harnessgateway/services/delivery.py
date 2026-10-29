"""Turns from the node's side (SPEC §6.2): the package's output in, the turn's end and the session's saved state reported.

    events   stdout lines in numbered batches: each read against the Responses schema, numbered and stored; the
             terminal event concludes the turn at once, so the caller has its answer before the process exits.
             Output the schema refuses fails the turn and stops it.
    finish   the process is gone: a turn without a terminal event is concluded from the outcome; the turn's token is
             revoked and its caller bindings deleted; the session is released; links to save its state are returned.
    saved    the state is saved (or was unchanged, or could not be): the session's snapshot advances if this save is
             newer than the one it points at.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any

from .. import ids
from ..db import Database, Tx
from ..domain import contract
from ..domain.turn import Reason, Status
from ..errors import Conflict, Invalid, NotFound
from ..extensions import Extensions, TurnSummary
from ..limits import LOG_TAIL
from .objects import Objects, org_key
from .traces import Traces
from .turns import completed_items, conclude, record_events

OUTCOMES = ("exited", "timed_out", "stopped", "failed")
SAVE_LINK_S = 2 * 3600
log = logging.getLogger("harnessgateway.delivery")


def _tail(previous: str, more: str) -> str:
    return (previous + more)[-LOG_TAIL:]


def _reason(value: object) -> Reason:
    try:
        return Reason(str(value))
    except ValueError:
        raise Invalid(f"unknown reason {value!r}", param="reason") from None


class Delivery:
    def __init__(self, db: Database, objects: Objects, traces: Traces, extensions: Extensions):
        self.db, self.objects, self.traces, self.ext = db, objects, traces, extensions

    @staticmethod
    async def _turn(tx: Tx, node_id: str, rid: str) -> dict[str, Any]:
        turn = await tx.one("SELECT * FROM turns WHERE id = %s FOR UPDATE", ids.check(rid, "turn"))
        if not turn:
            raise NotFound(f"no turn {rid}")
        if turn["node_id"] != node_id:
            raise Conflict(f"{rid} is not this node's turn")
        return turn

    # ── events ─────────────────────────────────────────────────────────────────────────────────────────────────────
    async def events(self, node_id: str, rid: str, batch: int, lines: list[str]) -> dict[str, Any]:
        """One batch of whole stdout lines, applied once. The reply says whether the turn has concluded and whether the
        node must stop the process (its output broke the schema)."""
        ended: TurnSummary | None = None
        failure = None
        async with self.db.platform() as tx:
            turn = await self._turn(tx, node_id, rid)
            if turn["status"] != Status.IN_PROGRESS or batch <= turn["last_batch"]:
                return {"concluded": Status(turn["status"]).final, "stop": False}
            events: list[dict[str, Any]] = []
            logs: list[str] = []
            terminal = None
            for text in lines:
                try:
                    line = contract.read_line(text)
                except contract.OutputError as e:
                    failure = str(e)
                    break
                if line.kind == "event":
                    events.append(line.event)
                elif line.kind == "log":
                    logs.append(text + "\n")
                elif line.kind == "terminal":
                    terminal = line.event
                    break
            await record_events(tx, turn, events)
            await tx.run("UPDATE turns SET last_batch = %s, log_tail = %s WHERE id = %s",
                         batch, _tail(turn["log_tail"], "".join(logs)), rid)
            if failure is not None:
                await tx.run("UPDATE turns SET cancel = coalesce(cancel, %s) WHERE id = %s", Reason.INVALID_OUTPUT.value, rid)
                ended = await conclude(tx, self.objects, turn, Status.FAILED, reason=Reason.INVALID_OUTPUT, message=failure)
            elif terminal is not None and not turn["cancel"]:
                ended = await self._from_terminal(tx, turn, terminal)
        if ended:
            await self.ext.turn_ended(ended)
        return {"concluded": ended is not None, "stop": failure is not None}

    async def _from_terminal(self, tx: Tx, turn: dict[str, Any], terminal: dict[str, Any]) -> TurnSummary:
        try:
            o = contract.outcome(terminal, await completed_items(tx, turn["id"]))
        except contract.OutputError as e:
            return await conclude(tx, self.objects, turn, Status.FAILED, reason=Reason.INVALID_OUTPUT, message=str(e))
        return await conclude(tx, self.objects, turn, Status(o.status), output=o.output, usage=o.usage, error=o.error,
                              incomplete=o.incomplete, metadata=o.metadata)

    # ── finish ─────────────────────────────────────────────────────────────────────────────────────────────────────
    async def finish(self, node_id: str, rid: str, report: dict[str, Any]) -> dict[str, Any]:
        """Report: outcome (exited | timed_out | stopped | failed), exit_code, stderr_tail, reason and error (when
        failed), applied_ops, persist (whether the node holds state to save), logs (whether it uploaded full logs),
        door (the door's connection records)."""
        outcome = report.get("outcome")
        if outcome not in OUTCOMES:
            raise Invalid(f"outcome is one of {', '.join(OUTCOMES)}", param="outcome")
        ended: TurnSummary | None = None
        async with self.db.platform() as tx:
            turn = await self._turn(tx, node_id, rid)
            stderr = str(report.get("stderr_tail") or "")[-LOG_TAIL:]
            if not Status(turn["status"]).final:
                ended = await self._conclude_exit(tx, turn, str(outcome), report, stderr)
            await tx.run("UPDATE turns SET exit_code = %s, stderr_tail = %s, exited_at = now() WHERE id = %s",
                         report.get("exit_code") if isinstance(report.get("exit_code"), int) else None, stderr, rid)
            await tx.run("UPDATE turn_tokens SET revoked_at = now() WHERE turn_id = %s AND revoked_at IS NULL", rid)
            await tx.run("DELETE FROM turn_bindings WHERE turn_id = %s", rid)
            applied = [o for o in report.get("applied_ops") or [] if isinstance(o, str)]
            if applied:
                await tx.run("UPDATE container_ops SET applied_at = now() WHERE id = ANY(%s) AND session_id = %s",
                             applied, turn["session_id"])
            session = await tx.get("SELECT * FROM sessions WHERE id = %s FOR UPDATE", turn["session_id"])
            saving = session["saving_turn_id"]
            if session["running_turn_id"] == rid:
                if report.get("persist") and session["store"] and session["status"] == "active":
                    saving = rid
                await tx.run("UPDATE sessions SET running_turn_id = NULL, saving_turn_id = %s, last_active_at = now() "
                             "WHERE id = %s", saving, session["id"])
            links = self._save_links(turn) if saving == rid else None
            final = await tx.get("SELECT * FROM turns WHERE id = %s", rid)
            harness = session["harness"]
        await self._harness_trace(final, harness, str(outcome), report)
        if ended:
            await self.ext.turn_ended(ended)
        return {"snapshot": links}

    async def _conclude_exit(self, tx: Tx, turn: dict[str, Any], outcome: str, report: dict[str, Any],
                             stderr: str) -> TurnSummary:
        """A turn whose process ended without a terminal event."""
        if outcome == "failed":
            return await conclude(tx, self.objects, turn, Status.FAILED, reason=_reason(report.get("reason")),
                                  message=str(report.get("error") or "")[:4000])
        items = await completed_items(tx, turn["id"])
        if turn["cancel"]:
            reason = _reason(turn["cancel"])
            status = Status.FAILED if reason == Reason.INVALID_OUTPUT else Status.CANCELLED
            return await conclude(tx, self.objects, turn, status, reason=reason, output=items)
        if outcome == "timed_out":
            return await conclude(tx, self.objects, turn, Status.FAILED, reason=Reason.TIMED_OUT, output=items,
                                  message="the turn ran past its time limit")
        last = " | ".join(stderr.strip().splitlines()[-3:])
        return await conclude(tx, self.objects, turn, Status.FAILED, reason=Reason.EXITED_WITHOUT_RESULT, output=items,
                              message=f"run.sh exited {report.get('exit_code')} without a terminal event"
                                      + (f"; stderr: {last[-1000:]}" if last else ""))

    def _save_links(self, turn: dict[str, Any]) -> dict[str, Any]:
        stem = f"{turn['seq']:08d}-{secrets.token_hex(4)}"
        archive = org_key(turn["org_id"], "sessions", turn["session_id"], "snapshots", f"{stem}.tgz")
        listing = org_key(turn["org_id"], "sessions", turn["session_id"], "snapshots", f"{stem}.json")
        return {"key": archive, "files_key": listing, "put": self.objects.link_put(archive, SAVE_LINK_S),
                "files_put": self.objects.link_put(listing, SAVE_LINK_S)}

    async def _harness_trace(self, turn: dict[str, Any], harness: str, outcome: str, report: dict[str, Any]) -> None:
        def at(v: Any) -> float | None:
            return v.timestamp() if v else None
        data: dict[str, Any] = {
            "harness": harness, "session_id": turn["session_id"], "status": turn["status"], "hg_reason": turn["hg_reason"],
            "outcome": outcome, "exit_code": turn["exit_code"], "created_at": at(turn["created_at"]),
            "started_at": at(turn["started_at"]), "ended_at": at(turn["ended_at"]), "exited_at": at(turn["exited_at"]),
            "events": turn["events"], "stdout_bytes": report.get("stdout_bytes"), "stderr_bytes": report.get("stderr_bytes"),
            "request_key": turn["request_key"]}
        if report.get("logs"):
            prefix = org_key(turn["org_id"], "turns", turn["id"], "")
            data.update(stdout_key=prefix + "stdout.log", stderr_key=prefix + "stderr.log")
            for name in ("stdout", "stderr"):
                await self.objects.record(prefix + f"{name}.log", turn["org_id"], "log", int(report.get(f"{name}_bytes") or 0))
        else:
            data.update(log_tail=turn["log_tail"], stderr_tail=turn["stderr_tail"])
        try:
            await self.traces.write(turn["org_id"], turn["id"], "harness", data)
            connections = [c for c in report.get("door") or [] if isinstance(c, dict)][:10_000]
            if connections:
                await self.traces.write(turn["org_id"], turn["id"], "door", {"connections": connections})
        except Exception:
            log.exception("recording the trace of %s failed", turn["id"])

    # ── saved ──────────────────────────────────────────────────────────────────────────────────────────────────────
    async def saved(self, node_id: str, rid: str, report: dict[str, Any]) -> None:
        """Report: snapshot {key, files_key, bytes} when a new one was uploaded; none when the state was unchanged or
        could not be saved (error says why), so the session keeps its last."""
        saved = report.get("snapshot") if isinstance(report.get("snapshot"), dict) else None
        stale: list[str] = []
        async with self.db.platform() as tx:
            turn = await self._turn(tx, node_id, rid)
            session = await tx.get("SELECT * FROM sessions WHERE id = %s FOR UPDATE", turn["session_id"])
            prefix = org_key(turn["org_id"], "sessions", session["id"], "snapshots", "")
            if saved and not all(isinstance(saved.get(k), str) and saved[k].startswith(prefix) for k in ("key", "files_key")):
                raise Invalid("the snapshot keys are not this session's", param="snapshot")
            if saved:
                size = int(saved.get("bytes") or 0)
                await tx.run("INSERT INTO objects (key, org_id, kind, bytes) VALUES (%s, %s, 'snapshot', %s), "
                             "(%s, %s, 'snapshot', 0) ON CONFLICT (key) DO NOTHING",
                             saved["key"], turn["org_id"], size, saved["files_key"], turn["org_id"])
                if turn["seq"] > session["snapshot_seq"] and session["status"] == "active":
                    stale = [k for k in (session["snapshot_key"], session["files_key"]) if k]
                    await tx.run("UPDATE sessions SET snapshot_key = %s, files_key = %s, snapshot_bytes = %s, "
                                 "snapshot_seq = %s WHERE id = %s", saved["key"], saved["files_key"], size, turn["seq"],
                                 session["id"])
                else:
                    stale = [saved["key"], saved["files_key"]]
            if session["saving_turn_id"] == rid:
                await tx.run("UPDATE sessions SET saving_turn_id = NULL WHERE id = %s", session["id"])
        if report.get("error"):
            log.warning("saving the state of %s after %s failed: %s", turn["session_id"], rid, str(report["error"])[:500])
        for key in stale:
            await self.objects.delete(key)
