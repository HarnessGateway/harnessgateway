"""What a sandbox may reach: host entries, the decision for one connection, and what the egress proxy is told
about a running sandbox."""

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from harnessgateway.errors import Invalid

DEFAULT_PORTS = (80, 443)                      # what an entry without a port allows
ENTRIES_MAX = 256
_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rf"(\*\.)?{_LABEL}(\.{_LABEL})*")

# Where dependencies and tools are fetched from. A limited environment allows them as one set unless it says
# not; a package's install always may reach them. No general storage service is here: its wildcard would reach
# anyone's bucket.
PACKAGE_MANAGER_HOSTS = (
    "registry.npmjs.org", "registry.yarnpkg.com", "nodejs.org", "jsr.io", "npm.jsr.io",
    "pypi.org", "files.pythonhosted.org", "astral.sh",
    "github.com", "api.github.com", "codeload.github.com", "raw.githubusercontent.com",
    "objects.githubusercontent.com", "release-assets.githubusercontent.com", "ghcr.io",
    "pkg-containers.githubusercontent.com",
    "gitlab.com", "bitbucket.org",
    "crates.io", "index.crates.io", "static.crates.io", "static.rust-lang.org", "sh.rustup.rs",
    "proxy.golang.org", "sum.golang.org", "go.dev", "dl.google.com",
    "rubygems.org", "index.rubygems.org",
    "repo.maven.apache.org", "repo1.maven.org", "plugins.gradle.org", "services.gradle.org",
    "packagist.org", "repo.packagist.org", "api.nuget.org",
    "deb.debian.org", "security.debian.org", "archive.ubuntu.com", "security.ubuntu.com", "ports.ubuntu.com",
    "dl-cdn.alpinelinux.org",
    "registry-1.docker.io", "auth.docker.io", "production.cloudflare.docker.com",
)


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class HostEntry:
    """One entry of an allow or deny list: `host`, `*.suffix`, or either with `:port`."""

    host: str
    port: int | None = None

    @classmethod
    def parse(cls, value: object) -> "HostEntry":
        if not isinstance(value, str) or not value or value != value.strip() or "/" in value:
            raise Invalid(f"a host entry is host, *.suffix or host:port, with no scheme and no path: {value!r}")
        text = value.lower()
        host, colon, port = text.rpartition(":")
        if not colon or _address(text) is not None:          # no port, or a bare IPv6 address
            host, port = text, ""
        if port and not (port.isdigit() and 1 <= int(port) <= 65535):
            raise Invalid(f"a port is 1 to 65535: {value!r}")
        if _address(host) is None and not _HOSTNAME.fullmatch(host):
            raise Invalid(f"a host entry is host, *.suffix or host:port: {value!r}")
        return cls(host, int(port) if port else None)

    def __str__(self) -> str:
        return self.host if self.port is None else f"{self.host}:{self.port}"

    def matches(self, host: str, port: int) -> bool:
        if port not in (DEFAULT_PORTS if self.port is None else (self.port,)):
            return False
        host = host.lower()
        if self.host.startswith("*."):
            return _address(host) is None and host.endswith(self.host[1:])
        return host == self.host


def entries(values: object) -> tuple[str, ...]:
    """A list of host entries, each checked and written one way. A ValueError says what is wrong with it: whoever
    asked knows which list it was."""
    if not isinstance(values, list | tuple) or len(values) > ENTRIES_MAX:
        raise ValueError(f"a list of at most {ENTRIES_MAX} host entries is needed")
    try:
        return tuple(str(HostEntry.parse(value)) for value in values)
    except Invalid as refusal:
        raise ValueError(refusal.message) from None


def is_forbidden_address(text: str) -> bool:
    """Whether an address must never be connected to, whatever a policy allows: private, local, or a cloud's
    metadata service (which is link-local). It is what keeps a sandbox off the network its host is on."""
    address = ipaddress.ip_address(text)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return (address.is_private or address.is_loopback or address.is_link_local or address.is_multicast
            or address.is_unspecified or address.is_reserved)


def _listed(names: tuple[str, ...], host: str, port: int) -> bool:
    return any(HostEntry.parse(name).matches(host, port) for name in names)


@dataclass(frozen=True)
class EgressPolicy:
    """An environment's network, as one decision: closed (what is allowed here and nothing else, which for a
    closed environment is only its turn's provider), limited (what is allowed), or open (anything not denied)."""

    mode: Literal["closed", "limited", "open"]
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    def decide(self, host: str, port: int) -> str:
        """Empty when the connection may be made; otherwise why not, in words for whoever set the policy."""
        if _listed(self.deny, host, port):
            return f"{host}:{port} is on the environment's deny list"
        if self.mode == "open" or _listed(self.allow, host, port):
            return ""
        if self.mode == "closed":
            return f"{host}:{port} is not reachable from a closed environment"
        return f"{host}:{port} is not in the environment's network.allow"


@dataclass(frozen=True)
class BoundSecret:
    """A secret's value, the placeholder a sandbox holds for it, and the hosts it may be sent to. With a header,
    it is also set as that header on every request to those hosts."""

    hosts: tuple[str, ...]
    value: str
    placeholder: str
    header: str | None = None
    prefix: str = ""


@dataclass(frozen=True)
class Policy:
    """Everything the egress proxy is told about one running sandbox: a turn's, or an install's."""

    org_id: str
    subject_id: str                            # the turn, or the install
    environment: str
    egress: EgressPolicy
    secrets: tuple[BoundSecret, ...] = field(default=())

    def secrets_for(self, host: str, port: int) -> list[BoundSecret]:
        return [secret for secret in self.secrets if _listed(secret.hosts, host, port)]

    def to_json(self) -> dict[str, Any]:
        return {"org_id": self.org_id, "subject_id": self.subject_id, "environment": self.environment,
                "mode": self.egress.mode, "allow": list(self.egress.allow), "deny": list(self.egress.deny),
                "secrets": [{**vars(secret), "hosts": list(secret.hosts)} for secret in self.secrets]}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Policy":
        return cls(data["org_id"], data["subject_id"], data["environment"],
                   EgressPolicy(data["mode"], tuple(data["allow"]), tuple(data["deny"])),
                   tuple(BoundSecret(**{**secret, "hosts": tuple(secret["hosts"])}) for secret in data["secrets"]))
