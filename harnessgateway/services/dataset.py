"""The dataset: the traces of the organizations that share them, as the deployer exports them. A
row per session: what was asked, what came back, how the member steered, and every exchange the
sandbox had with the model provider through the gateway, verbatim. Who it was is replaced by a
pseudonym that is the same across exports and means nothing outside this deployment.

Only sessions of organizations that share now, created after sharing was turned on, are in it; an
enterprise organization's never are. Provider keys are not in captures (the gateway puts them in
after recording); what a member typed into a prompt is in it as typed."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Iterator

from ..backends.blobs import Blobs
from ..backends.records import Records
from ..domain.orgs import Org, Orgs
from ..domain.sessions import Session, turn_blob
from ..errors import NotFound
from ..kernel.vault import Sealer

CAPTURE_MAX = 2 * 1024 * 1024                        # per captured body in a row; the rest is said to be cut


class Dataset:
    def __init__(self, records: Records, blobs: Blobs, orgs: Orgs, vault: Sealer):
        self.records, self.blobs, self.orgs = records, blobs, orgs
        self._secret = vault.derive("dataset")

    def pseudonym(self, kind: str, value: str) -> str:
        """`kind_<16 hex>`: stable for the same value on this deployment, unrelated to it elsewhere."""
        return f"{kind}_{hmac.new(self._secret, f'{kind}:{value}'.encode(), hashlib.sha256).hexdigest()[:16]}" if value else ""

    def sessions(self, org: Org, since: float = 0, until: float | None = None) -> list[Session]:
        until = until or time.time()
        start = max(since, org.trace_sharing_since)
        found = [Session(**d) for d in self.records.list("session", org.name)]
        return sorted((s for s in found if start <= s.created < until and not s.running), key=lambda s: s.created)

    def summary(self, since: float = 0, until: float | None = None) -> dict[str, Any]:
        orgs = self.orgs.sharing()
        counts = [(o, self.sessions(o, since, until)) for o in orgs]
        return {"orgs": len(orgs), "sessions": sum(len(ss) for _, ss in counts), "turns": sum(len(s.responses) for _, ss in counts for s in ss),
                "since": since, "until": until or time.time()}

    def rows(self, since: float = 0, until: float | None = None) -> Iterator[dict[str, Any]]:
        for o in self.orgs.sharing():
            for s in self.sessions(o, since, until):
                yield self.row(o, s)

    def row(self, org: Org, s: Session) -> dict[str, Any]:
        trace = self.records.list("trace", org.name, f"{s.id}:")
        return {"session": s.id, "org": self.pseudonym("org", org.name), "member": self.pseudonym("member", f"{org.name}/{s.member}"),
                "team": self.pseudonym("team", f"{org.name}/{s.team}") if s.team else "", "harness": s.harness, "version": s.version, "created": s.created,
                "plan": org.plan, "turns": [self.turn(org, s, rid, trace) for rid in s.responses]}

    def turn(self, org: Org, s: Session, rid: str, trace: list[dict[str, Any]]) -> dict[str, Any]:
        doc = self.records.get("response", org.name, rid) or {}
        events = [{"at": t["at"], "kind": t["kind"], **({"body": t["body"]} if t["kind"] in ("interrupt", "cancel", "disconnect") else {})}
                  for t in trace if t.get("response") == rid and t["kind"] != "request"]
        response = next((t["body"] for t in reversed(trace) if t.get("response") == rid and t["kind"] == "response"), None)
        return {"response": rid, "turn": doc.get("turn"), "status": doc.get("status"), "provider_api": self.provider_api(org.name, doc.get("provider", "")),
                "model": doc.get("model"), "created": doc.get("created"), "ended": doc.get("ended"),
                "request": self.blob_json(turn_blob(org.name, s.id, rid, "request.json")), "response_object": response, "events": events,
                "usage": numbers_of(self.records.get("usage", org.name, rid) or {}), "captures": self.captures(org.name, s.id, rid)}

    def provider_api(self, org: str, name: str) -> str:
        p = self.records.get("provider", org, name) if name else None
        return (p or {}).get("api", "")

    def captures(self, org: str, sid: str, rid: str) -> list[dict[str, Any]]:
        prefix = turn_blob(org, sid, rid, "provider")
        out = []
        for key in self.blobs.list(prefix):
            c = self.blob_json(key) or {}
            out.append({"name": key[len(prefix) + 1:], "at": c.get("at"), "method": c.get("method"), "path": c.get("path"), "status": c.get("status"),
                        "request": cut(c.get("request", "")), "response": cut(c.get("response", ""))})
        return out

    def blob_json(self, key: str) -> Any:
        try:
            return json.loads(self.blobs.get(key))
        except (NotFound, ValueError):
            return None


def numbers_of(usage: dict[str, Any]) -> dict[str, Any]:
    """The usage record's measurements; who and what it names is in the row already, or nobody's business."""
    return {k: v for k, v in usage.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def cut(text: str) -> dict[str, Any]:
    if len(text) <= CAPTURE_MAX:
        return {"text": text}
    return {"text": text[:CAPTURE_MAX], "cut": len(text) - CAPTURE_MAX}
