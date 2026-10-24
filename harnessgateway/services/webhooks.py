"""Delivery: every final response is posted to the organization's webhooks as it ends, signed, from a
thread pool so the node that reported it never waits. A delivery that fails is retried with a
growing pause, then given up on and counted on the webhook. The body is the response object the
API would return, under an event envelope; the signature is HMAC-SHA256 of `timestamp.body`."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import httpx

from ..domain.sessions import Sessions, Turn
from ..domain.webhooks import Webhook, Webhooks
from ..kernel.ids import new_id

SIGNATURE = "X-HG-Signature"
TIMESTAMP = "X-HG-Timestamp"
DELIVERY = "X-HG-Delivery"
EVENT = "X-HG-Event"
PAUSES_S = (1.0, 5.0, 25.0)                          # between attempts; four attempts in all
TIMEOUT_S = 10.0


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str, tolerance_s: float = 300) -> bool:
    """What a receiver does: the signature matches and the timestamp is recent."""
    try:
        fresh = abs(time.time() - float(timestamp)) <= tolerance_s
    except ValueError:
        return False
    return fresh and hmac.compare_digest(sign(secret, timestamp, body), signature)


class Notifier:
    def __init__(self, webhooks: Webhooks, sessions: Sessions, transport: httpx.BaseTransport | None = None, pauses: tuple = PAUSES_S, workers: int = 4):
        self.webhooks, self.sessions, self.pauses = webhooks, sessions, pauses
        self._http = httpx.Client(timeout=TIMEOUT_S, transport=transport, follow_redirects=False)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="webhook")
        self._pending: list[Future] = []

    def turn_ended(self, r: Turn, report: dict[str, Any] | None = None) -> list[Future]:
        """Post the final response to every webhook of the organization; returns the deliveries in flight."""
        hooks = self.webhooks.list(r.org)
        if not hooks:
            return []
        event = {"id": new_id("evt"), "type": f"response.{r.status}", "created": int(time.time()), "data": self.sessions.response_object(r)}
        return [self.submit(w, event) for w in hooks]

    def ping(self, w: Webhook) -> Future:
        return self.submit(w, {"id": new_id("evt"), "type": "ping", "created": int(time.time()), "data": {"webhook": w.id}})

    def submit(self, w: Webhook, event: dict[str, Any]) -> Future:
        self._pending = [f for f in self._pending if not f.done()]
        f = self._pool.submit(self.deliver, w, event)
        self._pending.append(f)
        return f

    def deliver(self, w: Webhook, event: dict[str, Any]) -> int:
        """Attempts until one is accepted (2xx) or the pauses run out; the last status, 0 for no answer."""
        body = json.dumps(event, separators=(",", ":")).encode()
        secret = self.webhooks.secret_of(w)
        status = 0
        for attempt, pause in enumerate((*self.pauses, None)):
            ts = str(int(time.time()))
            headers = {"Content-Type": "application/json", SIGNATURE: sign(secret, ts, body), TIMESTAMP: ts, DELIVERY: event["id"], EVENT: event["type"],
                       "User-Agent": "harnessgateway-webhooks"}
            try:
                status = self._http.post(w.url, content=body, headers=headers).status_code
            except httpx.HTTPError:
                status = 0
            if 200 <= status < 300:
                self.webhooks.delivered(w, status, True)
                return status
            if pause is None:
                break
            time.sleep(pause)
        self.webhooks.delivered(w, status, False)
        return status

    def drain(self, timeout_s: float = 30) -> None:
        """Wait for the deliveries in flight (tests, shutdown)."""
        for f in list(self._pending):
            f.result(timeout=timeout_s)
        self._pending = []
