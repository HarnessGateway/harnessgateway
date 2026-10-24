"""A provider: a model endpoint the organization pays for, its API shape, and its key, sealed the
moment it arrives. A turn never sees the key: it gets a session token, and the gateway swaps the
token for the key per request."""
from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..backends.egress import check_host
from ..backends.records import Records
from ..errors import Invalid, NotFound
from ..kernel.ids import check_name, digest_of, is_name
from ..kernel.vault import Sealer
from . import PLATFORM
from .orgs import Orgs

APIS = ("anthropic-messages", "openai-responses", "openai-chat", "none")


def session_token(api: str) -> str:
    """A turn's stand-in for its key under the gateway, shaped like a real one for CLIs that check."""
    return ("sk-ant-api03-hg-" if api == "anthropic-messages" else "sk-hg-") + secrets.token_hex(24)


@dataclass(frozen=True)
class Provider:
    org: str
    name: str
    url: str
    api: str
    key: str = field(repr=False)                     # sealed

    @property
    def path(self) -> str:
        """The URL's path, which the gateway's URL carries too, so a CLI's base-URL idioms hold."""
        return urlsplit(self.url).path.rstrip("/")

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "url": self.url, "api": self.api}


class Providers:
    def __init__(self, records: Records, vault: Sealer, orgs: Orgs, allow_private: bool = False):
        self.records, self.vault, self.orgs, self.allow_private = records, vault, orgs, allow_private

    def put(self, org: str, name: str, url: str, api: str, key: str) -> Provider:
        self.orgs.get(org)
        check_name(name, "provider")
        if api not in APIS:
            raise Invalid(f"a provider's api is one of {', '.join(APIS)}")
        if not url.startswith("https://"):
            raise Invalid("a provider's url is https://…")
        try:
            check_host(url, self.allow_private)
        except ValueError as e:
            raise Invalid(f"a provider's url: {e}") from None
        if (api == "none") != (not key):
            raise Invalid("a provider has a key, unless its api is none")
        if any(c in key for c in "\r\n\0"):
            raise Invalid("a provider's key is one line")
        sealed = self.vault.seal(key, f"{org}/providers/{name}") if key else ""
        p = Provider(org, name, url.rstrip("/"), api, sealed)
        self.records.put("provider", org, name, asdict(p))
        return p

    def get(self, org: str, name: str) -> Provider:
        d = self.records.get("provider", org, name) if is_name(name) else None
        if d is None:
            raise NotFound(f"no provider {name}")
        return Provider(**d)

    def list(self, org: str) -> list[Provider]:
        return [Provider(**d) for d in self.records.list("provider", org)]

    def delete(self, org: str, name: str) -> None:
        if not self.records.delete("provider", org, name):
            raise NotFound(f"no provider {name}")

    def open_key(self, p: Provider) -> str:
        """The provider's key, in the clear: only the gateway calls this, for one upstream request."""
        return self.vault.open(p.key, f"{p.org}/providers/{p.name}") if p.key else ""

    # session tokens: a turn's credential under the gateway
    def issue_token(self, org: str, provider: str, session: str, response: str, api: str, ttl_s: float) -> str:
        secret = session_token(api)
        self.records.put("token", PLATFORM, digest_of(secret), {"org": org, "provider": provider, "session": session,
                                                                 "response": response, "expires": time.time() + ttl_s})
        return secret

    def resolve_token(self, secret: str) -> dict[str, Any] | None:
        d = self.records.get("token", PLATFORM, digest_of(secret))
        return d if d and d["expires"] > time.time() else None

    def revoke_token(self, secret: str) -> None:
        self.records.delete("token", PLATFORM, digest_of(secret))
