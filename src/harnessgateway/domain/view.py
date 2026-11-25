"""A link to a session's files that a browser can open by itself.

A page shows an image, plays a video or runs a page an agent built by giving the browser an address, and a browser
sends no API key with such a request. So the address is the permission: it holds a token that names one session
of one organization until a time, signed with a key of the installation's own. What is served through it is served
sandboxed (`POLICY`): what an agent made runs in an origin of its own, and can neither read the page that shows
it nor call the API as anyone.
"""

import base64
import binascii
import hashlib
import hmac
import mimetypes
import re

from harnessgateway.ids import is_id

LASTS_S = 3600                                 # how long a link opens what it names
# No origin (so no storage, no cookies and nothing of the page around it), and otherwise free: it is the agent's
# page, and may load what it likes.
POLICY = ("sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads; "
          "default-src * data: blob: 'unsafe-inline' 'unsafe-eval'")
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")
_TEXT = ("application/json", "text/javascript", "image/svg+xml")
# What `mimetypes` does not know, or knows differently from one host to the next: source is shown, never run.
_TYPES = {"md": "text/markdown", "markdown": "text/markdown", "mmd": "text/plain", "ts": "text/plain",
          "tsx": "text/plain", "jsx": "text/plain", "py": "text/plain", "sh": "text/plain", "yaml": "text/plain",
          "yml": "text/plain", "toml": "text/plain", "go": "text/plain", "rs": "text/plain",
          "ipynb": "application/json", "js": "text/javascript", "mjs": "text/javascript", "webp": "image/webp",
          "avif": "image/avif", "m4a": "audio/mp4", "opus": "audio/ogg", "mov": "video/quicktime", "csv": "text/csv",
          "tsv": "text/tab-separated-values"}


def _signature(key: bytes, said: str) -> str:
    return hmac.new(key, b"view\x00" + said.encode(), hashlib.sha256).hexdigest()     # for this purpose only


def sign(key: bytes, org_id: str, session_id: str, *, expires_at: int) -> str:
    """The token of a link to one session's files, good until `expires_at`."""
    said = f"{org_id}\n{session_id}\n{expires_at}"
    return f"{base64.urlsafe_b64encode(said.encode()).decode().rstrip('=')}.{_signature(key, said)}"


def read(key: bytes, token: str, *, now: float) -> tuple[str, str] | None:
    """The organization and the session a token names; None when it was not made here, was changed, or is past
    its time."""
    encoded, _, signature = token.partition(".")
    try:
        said = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        org_id, session_id, expires_at = said.split("\n")
        valid = hmac.compare_digest(_signature(key, said), signature) and int(expires_at) >= now
    except (binascii.Error, ValueError):
        return None
    return (org_id, session_id) if valid and is_id(session_id, "session") else None


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The one span a Range header asks for, first and last byte inclusive; None for the whole file (no header,
    or one that is not served: several spans, another unit). ValueError when the span lies outside the file."""
    match = _RANGE.fullmatch((header or "").strip())
    if not match or not (match[1] or match[2]) or size == 0:
        return None
    if match[1]:
        first, last = int(match[1]), min(int(match[2]), size - 1) if match[2] else size - 1
    else:                                          # the last n bytes
        first, last = max(size - int(match[2]), 0), size - 1
        if int(match[2]) == 0:
            raise ValueError("an empty suffix")
    if first >= size or first > last:
        raise ValueError(f"bytes {first}-{last} of {size}")
    return first, last


def content_type(path: str) -> str:
    """What a browser is told a file is, from its name."""
    kind = (_TYPES.get(path.rpartition(".")[2].lower()) or mimetypes.guess_type(path)[0]
            or "application/octet-stream")
    return f"{kind}; charset=utf-8" if kind.startswith("text/") or kind in _TEXT else kind
