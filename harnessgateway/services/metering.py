"""Metering: what a turn cost, as the platform bills it. Compute is what the sandbox reserved for the
turn's wall time (cpus × seconds, memory × seconds), egress is the bytes the door and the gateway
carried out, storage is what the organization keeps (snapshots, packages, homes), sampled. Tokens are
counted from the gateway's captures and shown, never billed. One usage record per turn, under the
organization; the gateway adds its calls and bytes to the same record as they happen.

    usage/<org>/<rid>     seconds, cpu_seconds, memory_gb_seconds, egress_bytes, calls, bytes_in, bytes_out,
                          input_tokens, output_tokens, harness, team, member, status, at
    meter/<org>/storage:<day>   bytes kept, sampled once a day
"""
from __future__ import annotations

import re
import time
from typing import Any

from ..backends.blobs import Blobs
from ..backends.records import Records
from ..domain.orgs import Orgs
from ..domain.sessions import Turn

SIZES = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}


def memory_bytes(size: str) -> int:
    """A Docker size (`4g`, `512m`) in bytes."""
    m = re.fullmatch(r"([0-9]+)([kmg]?)", size or "")
    return int(m.group(1)) * SIZES.get(m.group(2), 1) if m else 0


class Metering:
    def __init__(self, records: Records, blobs: Blobs, orgs: Orgs):
        self.records, self.blobs, self.orgs = records, blobs, orgs

    def turn_ended(self, r: Turn, report: dict[str, Any]) -> dict[str, Any]:
        """The turn's usage record, completed with what the node reported."""
        ceilings = self.orgs.get(r.org).ceilings
        seconds = round(float(report.get("seconds", 0)), 1)
        usage = self.records.get("usage", r.org, r.id) or {"calls": 0, "bytes_in": 0, "bytes_out": 0}
        tokens = {k: (r.usage or {}).get(k, 0) for k in ("input_tokens", "output_tokens", "total_tokens")}
        doc = {**usage, **tokens, "seconds": seconds, "at": r.ended or time.time(), "harness": r.harness, "team": r.team,
               "member": r.member, "status": r.status, "session": r.session,
               "cpu_seconds": round(ceilings.cpus * seconds, 1),
               "memory_gb_seconds": round(memory_bytes(ceilings.memory) / SIZES["g"] * seconds, 1),
               "egress_bytes": int(report.get("egress_bytes", 0)) + usage.get("bytes_out", 0) + usage.get("bytes_in", 0),
               "snapshot_bytes": int(report.get("snapshot_bytes", 0))}
        self.records.put("usage", r.org, r.id, doc)
        return doc

    def sample_storage(self, org: str) -> dict[str, Any]:
        """What the organization keeps today, in bytes, by kind; one sample per day."""
        day = time.strftime("%Y-%m-%d", time.gmtime())
        sizes = {"sessions": 0, "harnesses": 0, "members": 0}
        for key in self.blobs.list(f"tenants/{org}"):
            kind = key.split("/")[2] if key.count("/") >= 2 else ""
            if kind in sizes:
                sizes[kind] += len(self.blobs.get(key))
        doc = {"at": time.time(), "day": day, **sizes, "bytes": sum(sizes.values())}
        self.records.put("meter", org, f"storage:{day}", doc)
        return doc

    def summary(self, org: str, since: float = 0, team: str = "", member: str = "") -> dict[str, Any]:
        """The organization's usage since `since`, optionally one team's or one member's: totals and a
        row per harness, with the latest storage sample."""
        rows = [u for u in self.records.list("usage", org) if u.get("at", 0) >= since
                and (not team or u.get("team") == team) and (not member or u.get("member") == member)]
        by_harness: dict[str, list] = {}
        for u in rows:
            by_harness.setdefault(u.get("harness", ""), []).append(u)
        samples = self.records.list("meter", org, "storage:")
        return {"totals": totals(rows), "harnesses": [{"harness": h, **totals(us)} for h, us in sorted(by_harness.items())],
                "storage": samples[-1] if samples else None}


def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"turns": len(rows), "seconds": round(sum(u.get("seconds", 0) for u in rows), 1),
            "cpu_seconds": round(sum(u.get("cpu_seconds", 0) for u in rows), 1),
            "memory_gb_seconds": round(sum(u.get("memory_gb_seconds", 0) for u in rows), 1),
            "egress_bytes": sum(u.get("egress_bytes", 0) for u in rows),
            "calls": sum(u.get("calls", 0) for u in rows), "bytes_in": sum(u.get("bytes_in", 0) for u in rows),
            "bytes_out": sum(u.get("bytes_out", 0) for u in rows),
            "input_tokens": sum(u.get("input_tokens", 0) for u in rows), "output_tokens": sum(u.get("output_tokens", 0) for u in rows)}
