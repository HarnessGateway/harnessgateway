"""Everything of the core that an extension may import. An edition is written against this module and against
nothing else of the core: what is here stays where it is from one version to the next, and anything that is not
here may move. Nothing in the core imports it.

    from harnessgateway.edition import Api, Extension, Forbidden, Platform, Principal
"""

from harnessgateway import __version__
from harnessgateway.api.http import (
    BODY_MAX,
    Api,
    answered,
    calling,
    limit_of,
    listing,
    make_key,
    may,
    page_of,
    parse_object,
    permit,
    read_body,
    refused,
)
from harnessgateway.defaults import give
from harnessgateway.domain import permission
from harnessgateway.domain.change import HOST, PLATFORM, Change
from harnessgateway.domain.egress import Connection as Passed
from harnessgateway.domain.egress import EgressPolicy, HostEntry, entries, is_forbidden_address
from harnessgateway.domain.principal import OWNER, Principal
from harnessgateway.domain.sandbox import Mount, SandboxPlan
from harnessgateway.domain.sealing import KeySealer, Sealer
from harnessgateway.domain.snapshot import Manifest
from harnessgateway.driver.base import ChunkStore, Driver, DriverError, Measured, OnLine, SandboxExit
from harnessgateway.driver.docker import FOLDERS, Lines
from harnessgateway.driver.folders import LocalFolders
from harnessgateway.errors import Conflict, Forbidden, Invalid, NotFound, Refused, TooLarge, Unauthorized
from harnessgateway.extensions import Extension
from harnessgateway.ids import check_name, fingerprint, ulid
from harnessgateway.platform import Platform
from harnessgateway.settings import Settings, SettingsError
from harnessgateway.store.blobs import BlobMissing, BlobStore
from harnessgateway.store.db import Connection, Database

__all__ = [
    "BODY_MAX", "FOLDERS", "HOST", "OWNER", "PLATFORM", "Api", "BlobMissing", "BlobStore", "Change", "ChunkStore",
    "Conflict", "Connection", "Database", "Driver", "DriverError", "EgressPolicy", "Extension", "Forbidden",
    "HostEntry", "Invalid", "KeySealer", "Lines", "LocalFolders", "Manifest", "Measured", "Mount", "NotFound",
    "OnLine", "Passed", "Platform", "Principal", "Refused", "SandboxExit", "SandboxPlan", "Sealer", "Settings",
    "SettingsError", "TooLarge", "Unauthorized", "__version__", "answered", "calling", "check_name", "entries",
    "fingerprint", "give", "is_forbidden_address", "limit_of", "listing", "make_key", "may", "page_of", "parse_object",
    "permission", "permit", "read_body", "refused", "ulid",
]
