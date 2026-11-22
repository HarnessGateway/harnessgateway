"""The little of HTTP the egress proxy has to understand: the head of a request, which is where a secret goes,
and how it says no."""

import asyncio
import ssl
from collections.abc import Sequence
from dataclasses import dataclass

from harnessgateway.domain.egress import BoundSecret

HEAD_MAX = 64 * 1024


@dataclass
class Head:
    """The first lines of a request: what it asks for, and its headers, in order."""

    method: str
    target: str
    version: str
    headers: list[tuple[str, str]]

    def header(self, name: str) -> str:
        return next((value for key, value in self.headers if key.lower() == name), "")

    def drop(self, *names: str) -> None:
        self.headers = [(key, value) for key, value in self.headers if key.lower() not in names]

    def encode(self) -> bytes:
        lines = [f"{self.method} {self.target} {self.version}", *(f"{key}: {value}" for key, value in self.headers)]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


async def read_head(reader: asyncio.StreamReader) -> Head | None:
    """The head of the next request; None when the client went away, or sent something that is not one."""
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, ssl.SSLError):
        return None
    first, *rest = raw[:-4].decode("latin-1").split("\r\n")
    start, fields = first.split(" "), [line.partition(":") for line in rest]
    if len(start) != 3 or not start[2].startswith("HTTP/") or not all(colon for _, colon, _ in fields):
        return None
    return Head(start[0], start[1], start[2], [(key.strip(), value.strip()) for key, _, value in fields])


def put_secrets(head: Head, secrets: Sequence[BoundSecret]) -> None:
    """Put a host's secrets into a request bound for it. Each placeholder in the headers and in the query (never
    in the body, which is not read) becomes its secret, and a secret with a header is set as that header. The
    host is then asked to close the connection once it has answered: one request is read per connection, so the
    proxy never has to understand an answer in order to find the next request."""
    def real(text: str) -> str:
        for secret in secrets:
            text = text.replace(secret.placeholder, secret.value)
        return text
    path, question, query = head.target.partition("?")
    head.target = path + question + real(query)
    head.headers = [(key, real(value)) for key, value in head.headers]
    for secret in secrets:
        if secret.header:
            head.drop(secret.header.lower())
            head.headers.append((secret.header, secret.prefix + secret.value))
    if "upgrade" not in head.header("connection").lower():      # an upgraded connection is the client's to end
        head.drop("connection", "keep-alive")
        head.headers.append(("Connection", "close"))


def refusal(status: str, why: str, *headers: str) -> bytes:
    """An answer from the proxy itself, in plain words a person reading an agent's log can act on."""
    body = (why + "\n").encode()
    head = [f"HTTP/1.1 {status}", "Content-Type: text/plain; charset=utf-8", f"Content-Length: {len(body)}",
            "Connection: close", *headers]
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body
