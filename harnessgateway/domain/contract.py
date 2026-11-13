"""The fixed facts of a sandbox that packages and the platform both rely on (spec §7.1, §7.3, §10.1)."""

from typing import Literal

ApiName = Literal["anthropic", "openai-responses", "openai-chat"]

CONTRACT_VERSION = 1
SANDBOX_UID = 10000

MIND = "/mind"
WORK = "/work"
TURN = "/turn"
HARNESS = "/harness"
CA_BUNDLE = "/etc/hg/ca.pem"
STATE_DIR = ".harnessgateway"                      # under the mind: where packages keep their own small state

PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
NO_PROXY_VARIABLES = ("NO_PROXY", "no_proxy")
CA_VARIABLES = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "NODE_EXTRA_CA_CERTS",
                "PIP_CERT")

_RESERVED = frozenset({"HOME", *PROXY_VARIABLES, *NO_PROXY_VARIABLES, *CA_VARIABLES})


def is_reserved(name: str) -> bool:
    """Whether the platform sets this variable itself, so nobody else may (spec §7.3)."""
    return name.startswith("HG_") or name in _RESERVED
