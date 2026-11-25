"""The sandbox contract: the fixed facts every harness package and the platform both rely on.

A package is written against these and nothing else of the platform, so changing any of them is a new contract
version, which the platform reports.
"""

from typing import Literal

VERSION = 3
ApiName = Literal["anthropic", "openai-responses", "openai-chat", "gemini"]     # the provider APIs a harness may speak

SANDBOX_UID = 10000                            # who everything in a sandbox runs as: never root
MIND = "/mind"                                 # $HOME: where an agent CLI keeps its own state, session to session
WORK = "/work"                                 # the working directory: what the agent is asked to work on
TURN = "/turn"                                 # this turn's inputs, read-only
HARNESS = "/harness"                           # the installed package, read-only while a turn runs
GIVEN = "/etc/hg"                              # what the platform gives every sandbox, read-only:
CA_BUNDLE = f"{GIVEN}/ca.pem"                  # the certificates it trusts
STATE_DIR = ".harnessgateway"                  # under the mind: where a package keeps small values of its own
MEMORY = "memory"                              # in a package's state: its note of where its harness keeps its memory
STARTUP = f"{TURN}/startup.sh"                 # the environment's startup script, when it has one
STARTUP_FAILED = 78                            # what a sandbox exits with when that script fails
INSTALL_TIMEOUT_S = 900                        # how long a package's install may take
STOP_GRACE_S = 15                              # how long a stopped turn is given to end
CACHE = ".cache"                               # under the mind: never saved with a session, as its name says

PROXY_HOST = "egress"                          # the egress proxy's name on the sandbox network
PROXY_PORT = 8080
PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
NO_PROXY_VARIABLES = ("NO_PROXY", "no_proxy")
NODE_PROXY_VARIABLE = "NODE_USE_ENV_PROXY"     # makes Node's own HTTP clients honour the proxy variables
CA_VARIABLES = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "NODE_EXTRA_CA_CERTS",
                "PIP_CERT")
_SET_BY_THE_PLATFORM = frozenset({"HOME", *PROXY_VARIABLES, *NO_PROXY_VARIABLES, NODE_PROXY_VARIABLE, *CA_VARIABLES})


def is_reserved(variable: str) -> bool:
    """Whether the platform sets this variable in every sandbox, so no definition may."""
    return variable.startswith("HG_") or variable in _SET_BY_THE_PLATFORM
