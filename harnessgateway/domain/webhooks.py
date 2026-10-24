"""Webhooks: where an organization wants to hear that a turn ended. An owner registers a URL and
gets a signing secret once; every final response is posted there, signed with it, so an agent
need not hold a connection open or poll. The secret is kept sealed; the URL must be public."""
from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from ..backends.records import Records
from ..errors import Invalid, NotFound
from ..kernel.ids import short_id
from ..kernel.vault import Sealer
from .orgs import Orgs

SECRET_PREFIX = "whsec_"
MAX_PER_ORG = 20


@dataclass(frozen=True)
class Webhook:
    id: str
    org: str
    url: str
    label: str
    created: float
    secret: str                                      # sealed
    last_status: int = 0                             # the last delivery's HTTP status, 0 before any
    last_at: float = 0
    failures: int = 0                                # deliveries given up on, in a row

    def public(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if k != "secret"}


class Webhooks:
    def __init__(self, records: Records, vault: Sealer, orgs: Orgs):
        self.records, self.vault, self.orgs = records, vault, orgs

    def create(self, org: str, url: str, label: str = "") -> tuple[Webhook, str]:
        """A webhook and its secret, shown once."""
        self.orgs.get(org)
        parts = urlsplit(url or "")
        if parts.scheme not in ("http", "https") or not parts.hostname or len(url) > 2000:
            raise Invalid("a webhook URL is http(s)://host/path")
        if len(self.list(org)) >= MAX_PER_ORG:
            raise Invalid(f"an organization has at most {MAX_PER_ORG} webhooks")
        secret = SECRET_PREFIX + secrets.token_urlsafe(32)
        w = Webhook(short_id(), org, url, label[:80], time.time(), "")
        w = Webhook(**{**asdict(w), "secret": self.vault.seal(secret, f"webhook:{org}:{w.id}")})
        self.records.put("webhook", org, w.id, asdict(w))
        return w, secret

    def get(self, org: str, wid: str) -> Webhook:
        d = self.records.get("webhook", org, wid) if wid else None
        if d is None:
            raise NotFound(f"no webhook {wid}")
        return Webhook(**d)

    def secret_of(self, w: Webhook) -> str:
        return self.vault.open(w.secret, f"webhook:{w.org}:{w.id}")

    def list(self, org: str) -> list[Webhook]:
        return sorted((Webhook(**d) for d in self.records.list("webhook", org)), key=lambda w: w.created)

    def delete(self, org: str, wid: str) -> None:
        self.get(org, wid)
        self.records.delete("webhook", org, wid)

    def delivered(self, w: Webhook, status: int, ok: bool) -> None:
        """What the last delivery came to; a webhook that keeps failing shows it."""
        if self.records.get("webhook", w.org, w.id) is None:
            return                                   # deleted meanwhile
        self.records.put("webhook", w.org, w.id, asdict(Webhook(**{**asdict(w), "last_status": status, "last_at": time.time(),
                                                                   "failures": 0 if ok else w.failures + 1})))
