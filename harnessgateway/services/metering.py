"""Metering: what a turn cost, as the platform bills it. Compute is what the sandbox reserved for the
turn's wall time (cpus × seconds, memory × seconds), egress is the bytes the door and the gateway
carried out, storage is what the organization keeps (snapshots, packages, homes), sampled once a
day. Tokens are counted from the gateway's captures and shown, never billed. One usage record per
turn, under the organization; the gateway adds its calls and bytes to the same record as they happen.

    usage/<org>/<rid>           seconds, cpu_seconds, memory_gb_seconds, egress_bytes, snapshot_bytes, calls,
                                bytes_in, bytes_out, input_tokens, output_tokens, harness, team, member, session, status, at
    meter/<org>/storage:<day>   bytes kept, by kind, sampled once a day
"""
from __future__ import annotations

import re
import time
from typing import Any, Iterable

from ..backends.billing import Billing
from ..backends.blobs import Blobs
from ..backends.records import Records
from ..domain.orgs import Org, Orgs
from ..domain.sessions import Turn

SIZES = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
METERS = {                                           # what is billed, and what is only shown
    "cpu_seconds": "sandbox CPUs reserved × wall seconds of the turn",
    "memory_gb_seconds": "sandbox memory reserved (GB) × wall seconds of the turn",
    "egress_bytes": "bytes the sandbox sent out through the door and the gateway",
    "storage_gb_days": "snapshots, packages and homes kept, sampled daily",
}
SHOWN = {"input_tokens": "tokens the provider billed the organization for; shown, never billed here",
         "output_tokens": "tokens the provider billed the organization for; shown, never billed here"}
STORAGE_KINDS = ("sessions", "harnesses", "members")


def memory_bytes(size: str) -> int:
    """A Docker size (`4g`, `512m`) in bytes."""
    m = re.fullmatch(r"([0-9]+)([kmg]?)", size or "")
    return int(m.group(1)) * SIZES.get(m.group(2), 1) if m else 0


def day_of(at: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(at))


class Metering:
    def __init__(self, records: Records, blobs: Blobs, orgs: Orgs, billing: Billing | None = None):
        self.records, self.blobs, self.orgs, self.billing = records, blobs, orgs, billing

    def turn_ended(self, r: Turn, report: dict[str, Any]) -> dict[str, Any]:
        """The turn's usage record, completed with what the node reported."""
        org = self.orgs.get(r.org)
        ceilings = org.ceilings
        seconds = round(float(report.get("seconds", 0)), 3)           # milliseconds: a fast turn still costs something
        usage = self.records.get("usage", r.org, r.id) or {"calls": 0, "bytes_in": 0, "bytes_out": 0}
        tokens = {k: (r.usage or {}).get(k, 0) for k in ("input_tokens", "output_tokens", "total_tokens")}
        doc = {**usage, **tokens, "seconds": seconds, "at": r.ended or time.time(), "harness": r.harness, "team": r.team,
               "member": r.member, "status": r.status, "session": r.session,
               "cpu_seconds": round(ceilings.cpus * seconds, 3),
               "memory_gb_seconds": round(memory_bytes(ceilings.memory) / SIZES["g"] * seconds, 3),
               "egress_bytes": int(report.get("egress_bytes", 0)) + usage.get("bytes_out", 0) + usage.get("bytes_in", 0),
               "snapshot_bytes": int(report.get("snapshot_bytes", 0))}
        self.records.put("usage", r.org, r.id, doc)
        self._bill(org, {k: doc[k] for k in ("cpu_seconds", "memory_gb_seconds", "egress_bytes")}, doc["at"])
        return doc

    def sample_storage(self, org: str, at: float | None = None) -> dict[str, Any]:
        """What the organization keeps, in bytes, by kind; one sample per day, the latest wins."""
        at = at or time.time()
        sizes = {kind: self.blobs.size(f"tenants/{org}/{kind}") for kind in STORAGE_KINDS}
        doc = {"at": at, "day": day_of(at), **sizes, "bytes": sum(sizes.values())}
        self.records.put("meter", org, f"storage:{doc['day']}", doc)
        self._bill(self.orgs.get(org), {"storage_gb_days": doc["bytes"] / SIZES["g"]}, at)
        return doc

    def sample_due(self, at: float | None = None) -> list[str]:
        """Sample every organization that has no sample for today; the names sampled."""
        at = at or time.time()
        done = []
        for o in self.orgs.list():
            if self.records.get("meter", o.name, f"storage:{day_of(at)}") is None:
                self.sample_storage(o.name, at)
                done.append(o.name)
        return done

    def rows(self, org: str, since: float = 0, until: float | None = None, team: str = "", member: str = "", harness: str = "") -> list[dict[str, Any]]:
        return [u for u in self.records.list("usage", org) if since <= u.get("at", 0) <= (until or float("inf"))
                and (not team or u.get("team") == team) and (not member or u.get("member") == member)
                and (not harness or u.get("harness") == harness)]

    def summary(self, org: str, days: int = 0, team: str = "", member: str = "", harness: str = "", now: float | None = None) -> dict[str, Any]:
        """The organization's usage over the last `days` (all time with 0), narrowed to a team, a
        member or a harness: totals, a row per day, per harness, per team, per member and per
        session, with the latest storage sample and what each meter means."""
        now = now or time.time()
        since = now - days * 86400 if days > 0 else 0
        rows = self.rows(org, since, now, team, member, harness)
        samples = self.records.list("meter", org, "storage:")
        if not samples or samples[-1]["day"] != day_of(now):            # a new organization is not kept waiting for the daily pass
            samples = [self.sample_storage(org, now)]
        return {"days": days, "totals": totals(rows), "daily": daily(rows, days, now),
                "harnesses": grouped(rows, "harness"), "teams": grouped(rows, "team"), "members": grouped(rows, "member"),
                "sessions": grouped(rows, "session"), "storage": samples[-1] if samples else None,
                "meters": METERS, "shown": SHOWN}

    def _bill(self, org: Org, readings: dict[str, float], at: float) -> None:
        if self.billing and self.billing.enabled:
            self.billing.report(org.billing_id, readings, at)


def totals(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    return {"turns": len(rows), "seconds": round(sum(u.get("seconds", 0) for u in rows), 3),
            "cpu_seconds": round(sum(u.get("cpu_seconds", 0) for u in rows), 3),
            "memory_gb_seconds": round(sum(u.get("memory_gb_seconds", 0) for u in rows), 3),
            "egress_bytes": sum(u.get("egress_bytes", 0) for u in rows),
            "calls": sum(u.get("calls", 0) for u in rows), "bytes_in": sum(u.get("bytes_in", 0) for u in rows),
            "bytes_out": sum(u.get("bytes_out", 0) for u in rows),
            "input_tokens": sum(u.get("input_tokens", 0) for u in rows), "output_tokens": sum(u.get("output_tokens", 0) for u in rows)}


def grouped(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Totals per distinct value of `key`, the busiest first."""
    by: dict[str, list] = {}
    for u in rows:
        by.setdefault(u.get(key) or "", []).append(u)
    return [{key: k, **totals(us)} for k, us in sorted(by.items(), key=lambda kv: (-len(kv[1]), kv[0]))]


def daily(rows: list[dict[str, Any]], days: int, now: float) -> list[dict[str, Any]]:
    """A row per day, oldest first: the last `days`, or every day since the first row."""
    by_day: dict[str, list] = {}
    for u in rows:
        by_day.setdefault(day_of(u.get("at", 0)), []).append(u)
    span = days if days > 0 else max(1, int((now - min((u.get("at", now) for u in rows), default=now)) // 86400) + 1)
    return [{"day": day_of(now - i * 86400), **totals(by_day.get(day_of(now - i * 86400), []))} for i in range(span - 1, -1, -1)]
