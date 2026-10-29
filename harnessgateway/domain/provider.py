"""Provider API shapes (SPEC §7.3): which paths the gateway relays for each, and how each carries its key."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Wire:
    paths: tuple[str, ...]           # path prefixes relayed
    header: str                      # where the key goes
    bearer: bool = False

    def relays(self, path: str) -> bool:
        return any(path == p or path.startswith(p.rstrip("/") + "/") or path.startswith(p + "?") for p in self.paths)

    def auth(self, key: str) -> tuple[str, str]:
        return self.header, (f"Bearer {key}" if self.bearer else key)

    def base(self, url: str) -> str:
        """A base URL without an API version segment a caller may have pasted: harnesses call <base><path>."""
        for suffix in ("/v1beta", "/v1"):
            url = url.removesuffix(suffix)
        return url


WIRES = {
    "anthropic": Wire(("/v1/messages", "/v1/models"), "x-api-key"),
    "openai": Wire(("/v1/responses", "/v1/chat/completions", "/v1/completions", "/v1/models", "/v1/embeddings"),
                   "authorization", bearer=True),
    "google": Wire(("/v1beta/models", "/v1/models", "/v1beta/openai"), "x-goog-api-key"),
}
PROVIDER_APIS = tuple(WIRES)
