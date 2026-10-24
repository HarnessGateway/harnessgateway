"""A harness: a package the organization owns, as a repository with a history. It belongs to the
organization or to one team; every commit is validated as a package before it becomes the head; a
session runs the commit it started on. Repositories live on the node that holds them and are
mirrored as bundles in the blob store, so another replica restores one when it has no copy.

    harness/<org>/<name>                a package: its team (or none), its head commit, its created time
      git   <git>/<org>/<name>.git                               the repository, on the node that holds it
      blob  tenants/<org>/harnesses/<name>/repo.bundle           the repository, mirrored after every commit
      blob  tenants/<org>/harnesses/<name>/<sha>.tgz             a commit's files, packed for workers
"""
from __future__ import annotations

import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..backends.blobs import Blobs
from ..backends.egress import check_host
from ..backends.records import Records
from ..backends.repos import Commit, Entry, Repository
from ..backends.repos.git import Repo
from ..contract import package
from ..errors import Conflict, Invalid, NotFound
from ..kernel.ids import check_name, is_name
from .members import Member
from .orgs import RESERVED, Orgs
from .teams import Teams


@dataclass(frozen=True)
class Harness:
    org: str
    name: str
    head: str                                        # the current commit
    created: float
    team: str = ""                                   # "" is the whole organization
    origin: str = ""                                 # the catalog package or harness it was forked from
    config: dict[str, Any] = field(default_factory=dict)   # harness.json at the head, as JSON

    def configuration(self) -> package.Config:
        return package.load_config_dict(self.config)

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "team": self.team, "head": self.head, "created": self.created, "origin": self.origin, **self.config}


class Harnesses:
    def __init__(self, records: Records, blobs: Blobs, git_root: Path, orgs: Orgs, teams: Teams, allow_private: bool = False):
        self.records, self.blobs, self.git_root = records, blobs, Path(git_root)
        self.orgs, self.teams, self.allow_private = orgs, teams, allow_private

    # where things are
    def repo_path(self, org: str, name: str) -> Path:
        return self.git_root / org / f"{name}.git"

    @staticmethod
    def blob_key(org: str, name: str, sha: str) -> str:
        return f"tenants/{org}/harnesses/{name}/{sha}.tgz"

    @staticmethod
    def bundle_key(org: str, name: str) -> str:
        return f"tenants/{org}/harnesses/{name}/repo.bundle"

    def repo(self, org: str, name: str) -> Repository:
        """The harness's repository on this node, restored from its mirrored bundle when this node has
        no copy (another replica took the last commit)."""
        h = self.get(org, name)
        r = Repo(self.repo_path(org, name))
        if not r.exists or (h.head and r.head() != h.head and not r.resolve_quiet(h.head)):
            r = Repo.from_bundle(self.repo_path(org, name), self.blobs.get(self.bundle_key(org, name)))
        return r

    # reading
    def get(self, org: str, name: str) -> Harness:
        d = self.records.get("harness", org, name) if is_name(name) else None
        if d is None:
            raise NotFound(f"no harness {name}")
        return Harness(**d)

    def list(self, org: str) -> list[Harness]:
        return [Harness(**d) for d in self.records.list("harness", org)]

    def visible(self, who: Member) -> list[Harness]:
        return [h for h in self.list(who.org) if self.teams.includes(who, h.team)]

    def usable(self, who: Member, name: str) -> Harness:
        """The harness, if the member may run it: the organization's, or one of their teams'."""
        h = self.get(who.org, name)
        if not self.teams.includes(who, h.team):
            raise NotFound(f"no harness {name}")
        return h

    def editable(self, who: Member, name: str) -> Harness:
        """The harness, if the member may change it: an owner changes any; a member changes their
        teams'; an organization-wide harness is an owner's."""
        h = self.usable(who, name)
        if not who.owner and not h.team:
            raise Invalid("an organization-wide harness is changed by an owner; team harnesses by their team")
        return h

    def files(self, org: str, name: str, sha: str | None = None) -> list[Entry]:
        return self.repo(org, name).tree(sha or self.get(org, name).head)

    def file(self, org: str, name: str, path: str, sha: str | None = None) -> bytes:
        return self.repo(org, name).read(path, sha or self.get(org, name).head)

    def log(self, org: str, name: str, limit: int = 100) -> list[Commit]:
        return self.repo(org, name).log(limit)

    def export(self, org: str, name: str, sha: str | None = None) -> bytes:
        return self.repo(org, name).export(sha or self.get(org, name).head)

    def config_at(self, org: str, name: str, sha: str) -> package.Config:
        """harness.json as it was at a commit a session runs on."""
        h = self.get(org, name)
        if sha == h.head:
            return h.configuration()
        return package.load_config(self.repo(org, name).read("harness.json", sha).decode(errors="replace"))

    # writing
    def _validated(self, repo: Repository, sha: str, org: str, name: str) -> package.Config:
        """The commit as a package: valid, with egress the platform allows; its files packed for workers."""
        with tempfile.TemporaryDirectory(prefix="hg-check-") as tmp:
            folder = Path(tmp) / "folder"
            repo.checkout(folder, sha)
            config = package.validate(folder)
            for host in config.egress:
                try:
                    check_host(host, self.allow_private)
                except ValueError as e:
                    raise Invalid(f"harness.json egress: {e}") from None
            self.blobs.put(self.blob_key(org, name, sha), package.pack(folder))
        return config

    def _settle(self, org: str, name: str, repo: Repository, sha: str, team: str, origin: str, created: float | None) -> Harness:
        config = self._validated(repo, sha, org, name)
        existing = self.records.get("harness", org, name)
        h = Harness(org, name, sha, created if created is not None else (existing["created"] if existing else time.time()),
                    team, origin, config.as_json())
        self.records.put("harness", org, name, asdict(h))
        self.blobs.put(self.bundle_key(org, name), repo.bundle())
        return h

    def _new(self, org: str, name: str, team: str) -> Repository:
        self.orgs.get(org)
        check_name(name, "harness")
        if name in RESERVED:
            raise Invalid(f"{name} is a reserved name")
        if team:
            self.teams.get(org, team)
        if self.records.get("harness", org, name):
            raise Conflict(f"harness {name} exists")
        return Repo.init(self.repo_path(org, name))

    def _within_upload(self, org: str, archive: bytes) -> None:
        o = self.orgs.get(org)
        if len(archive) > o.ceilings.upload_mb * 1024 * 1024:
            raise Invalid(f"the package is over the organization's upload limit of {o.ceilings.upload_mb} MB")

    def create_from_archive(self, org: str, name: str, archive: bytes, author: str, team: str = "", origin: str = "",
                            message: str = "import") -> Harness:
        """A new harness whose first commit is the archive's files."""
        self._within_upload(org, archive)
        repo = self._new(org, name, team)
        try:
            with tempfile.TemporaryDirectory(prefix="hg-import-") as tmp:
                folder = Path(tmp) / "folder"
                folder.mkdir()
                package.unpack(archive, folder)
                package.validate(folder)
                sha = repo.import_tree(folder, message, author)
            return self._settle(org, name, repo, sha, team, origin, None)
        except Exception:
            repo.delete()
            raise

    def create_from_folder(self, org: str, name: str, folder: Path, author: str, team: str = "", origin: str = "",
                           message: str = "fork") -> Harness:
        """A new harness from a directory on this node (a catalog package)."""
        repo = self._new(org, name, team)
        try:
            package.validate(Path(folder))
            sha = repo.import_tree(Path(folder), message, author)
            return self._settle(org, name, repo, sha, team, origin, None)
        except Exception:
            repo.delete()
            raise

    def fork(self, org: str, name: str, into: str, author: str, team: str = "") -> Harness:
        """A copy of a harness, history and all, as another harness of the organization."""
        repo = self.repo(org, name)
        check_name(into, "harness")
        if into in RESERVED:
            raise Invalid(f"{into} is a reserved name")
        if self.records.get("harness", org, into):
            raise Conflict(f"harness {into} exists")
        if team:
            self.teams.get(org, team)
        forked = repo.fork(self.repo_path(org, into))
        try:
            return self._settle(org, into, forked, forked.head(), team, f"{org}/{name}", None)
        except Exception:
            forked.delete()
            raise

    def commit(self, org: str, name: str, changes: dict[str, bytes | None], message: str, author: str, parent: str | None = None) -> Harness:
        """Apply file changes as a commit; the whole package must still validate, else nothing moves."""
        h = self.get(org, name)
        repo = self.repo(org, name)
        sha = repo.commit(changes, message, author, parent if parent is not None else h.head)
        try:
            return self._settle(org, name, repo, sha, h.team, h.origin, h.created)
        except Invalid:
            if h.head:
                repo.reset(h.head)                    # the bad commit is unreachable
            raise

    def replace(self, org: str, name: str, archive: bytes, message: str, author: str) -> Harness:
        """The archive's files as the next commit: the whole package, files not in it removed."""
        h = self.get(org, name)
        self._within_upload(org, archive)
        repo = self.repo(org, name)
        with tempfile.TemporaryDirectory(prefix="hg-import-") as tmp:
            folder = Path(tmp) / "folder"
            folder.mkdir()
            package.unpack(archive, folder)
            package.validate(folder)
            sha = repo.import_tree(folder, message, author)
        if sha == h.head:
            return h
        try:
            return self._settle(org, name, repo, sha, h.team, h.origin, h.created)
        except Invalid:
            repo.reset(h.head)
            raise

    def move(self, org: str, name: str, team: str) -> Harness:
        h = self.get(org, name)
        if team:
            self.teams.get(org, team)
        h = Harness(**{**asdict(h), "team": team})
        self.records.put("harness", org, name, asdict(h))
        return h

    def delete(self, org: str, name: str) -> None:
        self.get(org, name)
        Repo(self.repo_path(org, name)).delete()
        self.blobs.delete(f"tenants/{org}/harnesses/{name}")
        self.records.delete("harness", org, name)

    def delete_all(self, org: str) -> None:
        for h in self.list(org):
            Repo(self.repo_path(org, h.name)).delete()
