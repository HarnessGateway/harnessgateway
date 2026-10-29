"""The shape of every name a caller or package chooses: harnesses, providers, MCP labels, node pools."""
from __future__ import annotations

import re

NAME_PATTERN = r"[a-z0-9][a-z0-9._-]{0,62}"
NAME = re.compile(NAME_PATTERN)
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
RESERVED_ENV = frozenset({"HOME", "PATH", "LANG", "USER", "SHELL", "PWD", "TMPDIR", "PYTHONPATH", "HTTP_PROXY",
                          "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE",
                          "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO"})


def env_name_ok(name: str) -> bool:
    return bool(ENV_NAME.fullmatch(name)) and not name.startswith("HG_") and name not in RESERVED_ENV
