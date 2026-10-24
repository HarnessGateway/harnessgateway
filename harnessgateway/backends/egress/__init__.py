"""Backend: a sandbox's only way out. `door.py` is a standard-library script run beside every
sandbox: a CONNECT proxy to the package's egress hosts (public addresses only, checked after
resolution) and a forwarder to the platform's gateway for provider calls. This package re-exports the
host rule so the control plane refuses a bad host before any turn runs."""
from .door import DOOR_PORT, GATEWAY_PORT, READY, check_host, is_public, normalize

__all__ = ["DOOR_PORT", "GATEWAY_PORT", "READY", "check_host", "is_public", "normalize"]
