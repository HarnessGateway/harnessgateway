"""Host entries, the egress decision, and the addresses that are never reached (spec §9.3, §11.3, §11.4)."""

import ipaddress
import re
from dataclasses import dataclass
from typing import Literal

from harnessgateway.errors import Invalid

DEFAULT_PORTS = (80, 443)
ENTRIES_MAX = 256

_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rf"(\*\.)?{_LABEL}(\.{_LABEL})*")
_METADATA = ipaddress.ip_address("169.254.169.254")


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class HostEntry:
    """One line of an allow or deny list: `host`, `*.suffix`, or either with `:port`."""

    host: str
    port: int | None = None

    @classmethod
    def parse(cls, value: object) -> "HostEntry":
        if not isinstance(value, str) or not value or value != value.strip():
            raise Invalid(f"a host entry is host, *.suffix or host:port: {value!r}")
        text = value.lower()
        host, colon, port_text = text.rpartition(":")
        if not colon or _address(text) is not None:          # no port, or a bare IPv6 address
            host, port_text = text, ""
        port = None
        if port_text:
            if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
                raise Invalid(f"a port is 1 to 65535: {value!r}")
            port = int(port_text)
        if _address(host) is None and not _HOSTNAME.fullmatch(host):
            raise Invalid(f"a host entry is host, *.suffix or host:port, with no scheme or path: {value!r}")
        return cls(host, port)

    def __str__(self) -> str:
        return self.host if self.port is None else f"{self.host}:{self.port}"

    def matches(self, host: str, port: int) -> bool:
        if port not in ((self.port,) if self.port is not None else DEFAULT_PORTS):
            return False
        host = host.lower()
        if self.host.startswith("*."):
            return _address(host) is None and host.endswith(self.host[1:])
        return host == self.host


def parse_entries(values: object, what: str, limit: int = ENTRIES_MAX) -> tuple[HostEntry, ...]:
    if not isinstance(values, list | tuple):
        raise Invalid(f"{what} is a list of host entries", param=what)
    if len(values) > limit:
        raise Invalid(f"{what} holds at most {limit} entries, not {len(values)}", param=what)
    try:
        return tuple(HostEntry.parse(v) for v in values)
    except Invalid as e:
        raise Invalid(f"{what}: {e.message}", param=what) from None


# The package-manager set (spec §9.4): where dependencies and tools are fetched from. A limited environment may
# allow it as one; a package's install always has it. No cloud platform is in it: an entry such as a storage
# service's wildcard would reach anybody's bucket.
PACKAGE_MANAGER_HOSTS = tuple(HostEntry.parse(name) for name in (
    # version control
    "github.com", "www.github.com", "api.github.com", "gist.github.com", "codeload.github.com", "npm.pkg.github.com",
    "raw.githubusercontent.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com",
    "pkg-npm.githubusercontent.com", "avatars.githubusercontent.com", "camo.githubusercontent.com",
    "gitlab.com", "www.gitlab.com", "registry.gitlab.com", "bitbucket.org", "www.bitbucket.org", "api.bitbucket.org",
    # container registries
    "registry-1.docker.io", "auth.docker.io", "index.docker.io", "hub.docker.com", "www.docker.com",
    "production.cloudflare.docker.com", "production.cloudfront.docker.com", "download.docker.com",
    "gcr.io", "*.gcr.io", "ghcr.io", "mcr.microsoft.com", "*.data.mcr.microsoft.com", "public.ecr.aws",
    # JavaScript
    "registry.npmjs.org", "www.npmjs.com", "www.npmjs.org", "npmjs.com", "npmjs.org", "yarnpkg.com",
    "registry.yarnpkg.com", "jsr.io", "npm.jsr.io", "nodejs.org", "www.nodejs.org",
    # Python
    "pypi.org", "www.pypi.org", "files.pythonhosted.org", "pythonhosted.org", "test.pypi.org", "pypi.python.org",
    "pypa.io", "www.pypa.io", "astral.sh", "repo.anaconda.com", "conda.anaconda.org", "anaconda.org",
    "www.anaconda.com", "anaconda.com", "continuum.io",
    # Ruby
    "rubygems.org", "www.rubygems.org", "api.rubygems.org", "index.rubygems.org", "ruby-lang.org",
    "www.ruby-lang.org", "rubyforge.org", "www.rubyforge.org", "rubyonrails.org", "www.rubyonrails.org", "rvm.io",
    "get.rvm.io",
    # Rust
    "crates.io", "www.crates.io", "index.crates.io", "static.crates.io", "rustup.rs", "static.rust-lang.org",
    "www.rust-lang.org",
    # Go
    "proxy.golang.org", "sum.golang.org", "index.golang.org", "golang.org", "www.golang.org", "goproxy.io",
    "pkg.go.dev",
    # the JVM
    "maven.org", "repo.maven.org", "central.maven.org", "repo1.maven.org", "repo.maven.apache.org",
    "maven.google.com", "jcenter.bintray.com", "gradle.org", "www.gradle.org", "services.gradle.org",
    "plugins.gradle.org", "plugins-artifacts.gradle.org", "kotlinlang.org", "www.kotlinlang.org", "spring.io",
    "repo.spring.io",
    # PHP, .NET, Dart, Elixir, Perl, CocoaPods, Haskell, Swift
    "packagist.org", "www.packagist.org", "repo.packagist.org", "nuget.org", "www.nuget.org", "api.nuget.org",
    "pub.dev", "api.pub.dev", "hex.pm", "www.hex.pm", "cpan.org", "www.cpan.org", "metacpan.org",
    "www.metacpan.org", "api.metacpan.org", "cocoapods.org", "www.cocoapods.org", "cdn.cocoapods.org",
    "haskell.org", "www.haskell.org", "hackage.haskell.org", "swift.org", "www.swift.org",
    # Linux distributions
    "deb.debian.org", "security.debian.org", "ubuntu.com", "*.ubuntu.com", "ppa.launchpad.net", "launchpad.net",
    "www.launchpad.net", "dl-cdn.alpinelinux.org", "*.nixos.org",
    # development tools
    "dl.k8s.io", "pkgs.k8s.io", "k8s.io", "www.k8s.io", "releases.hashicorp.com", "apt.releases.hashicorp.com",
    "rpm.releases.hashicorp.com", "archive.releases.hashicorp.com", "hashicorp.com", "www.hashicorp.com",
    "apache.org", "www.apache.org", "archive.apache.org", "downloads.apache.org", "eclipse.org", "www.eclipse.org",
    "download.eclipse.org", "developer.apple.com", "developer.android.com", "pkg.stainless.com",
    "binaries.prisma.sh",
    # mirrors, fonts, schemas, the Model Context Protocol
    "sourceforge.net", "*.sourceforge.net", "packagecloud.io", "*.packagecloud.io", "fonts.googleapis.com",
    "fonts.gstatic.com", "json-schema.org", "www.json-schema.org", "json.schemastore.org", "www.schemastore.org",
    "*.modelcontextprotocol.io",
))


def is_forbidden_address(text: str) -> bool:
    """Whether a resolved address must never be connected to, whatever a policy allows."""
    address = ipaddress.ip_address(text)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return (
        address == _METADATA
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class EgressPolicy:
    """What one turn's or one install's sandbox may connect to. Nothing is reached unless a rule here allows it:
    `open` allows every host; `limited` and `closed` allow the hosts in `allow`, which for `closed` is the
    turn's provider and nothing else. A host in `deny` is refused whatever allows it."""

    mode: Literal["closed", "limited", "open"]
    allow: tuple[HostEntry, ...]
    deny: tuple[HostEntry, ...]

    def decide(self, host: str, port: int) -> Decision:
        where = f"{host}:{port}"
        if any(entry.matches(host, port) for entry in self.deny):
            return Decision(False, f"{where} is on the environment's deny list")
        if self.mode == "open" or any(entry.matches(host, port) for entry in self.allow):
            return Decision(True)
        if self.mode == "closed":
            return Decision(False, f"{where} is not allowed: a closed environment reaches the turn's provider only")
        return Decision(False, f"{where} is not allowed: add {host} to the environment's network.allow")
