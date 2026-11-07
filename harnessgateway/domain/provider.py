"""Provider API shapes (SPEC §9.4): the base URL a package calls for each, and the header that carries its key. The key
itself is a placeholder in the box; the door swaps it on the provider's host (SPEC §8.3)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Wire:
    header: str                      # where the key goes; the door swaps the placeholder for the key inside it

    def base(self, url: str) -> str:
        """A base URL without an API version segment a caller may have pasted: harnesses call <base><path>."""
        for suffix in ("/v1beta", "/v1"):
            url = url.removesuffix(suffix)
        return url


WIRES = {
    "anthropic": Wire("x-api-key"),
    "openai": Wire("authorization"),
    "google": Wire("x-goog-api-key"),
}
PROVIDER_APIS = tuple(WIRES)
