"""What may be logged and shown of a turn's plan (spec §5.5): no token, no placeholder."""

import re

_MASKS = (
    (re.compile(r"(://[^:/@\s]+):[^@\s]+@"), r"\1:***@"),              # credentials in a URL
    (re.compile(r"hgs_[0-9a-f]{32}"), "hgs_***"),                      # a secret's placeholder
    (re.compile(r"(HG_STEP_TOKEN=)\S+"), r"\1***"),
)


def mask(text: str) -> str:
    """Text about a plan, with every token and placeholder in it hidden."""
    for pattern, replacement in _MASKS:
        text = pattern.sub(replacement, text)
    return text
