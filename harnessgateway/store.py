"""Layer 3: what the platform keeps, per organization, as records, blobs and git repositories.
Nothing here runs anything.

    org/<name>                          the organization: created, limits, status
    member/<org>/<name>                 owner or member
    team/<org>/<name>                   a team: its members; a harness may belong to one team
    key/<org>/<id>                      an API key's record; its secret is shown once, its digest kept in login/
    provider/<org>/<name>               a model endpoint, its API shape, its key sealed by the vault
    harness/<org>/<name>                a package: its team (or none), its head commit, its created time
      git   <git>/<org>/<name>.git                               the repository, on the node that holds it
      blob  tenants/<org>/harnesses/<name>/repo.bundle           the repository, mirrored after every commit
      blob  tenants/<org>/harnesses/<name>/<sha>.tgz             a commit's files, packed for workers
    login/<digest>, deployer/key, worker/<id>, token/<digest>    platform-level: the empty tenant

A name is lowercase letters, digits, dots, dashes and underscores, at most 63, starting with a letter
or digit. The same name is one thing per kind per organization; organizations never collide.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import harness
from .blobs import Blobs
from .errors import Conflict, Invalid, NotFound
from .gitstore import Commit, Entry, Repo
from .limits import Limits
from .records import Records
from .runtimes.door import check_host
from .secrets import Vault

NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
ROLES = ("owner", "member")
APIS = ("anthropic-messages", "openai-responses", "openai-chat", "none")
KEY_PREFIX = "hg_"
PLATFORM = ""                                        # the tenant of platform-level records
RESERVED = {"catalog", "template", "platform", "deployer", "api", "internal", "auth", "app", "docs", "web"}


def check_name(name: Any, kind: str) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise Invalid(f"a {kind} name is lowercase letters, digits, dots, dashes and underscores, at most 63, "
                      f"starting with a letter or digit: {name!r}")
    return name


def digest_of(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def session_token(api: str) -> str:
    """A session's stand-in for its key under the gateway, shaped like a real one for CLIs that check."""
    return ("sk-ant-api03-hg-" if api == "anthropic-messages" else "sk-hg-") + secrets.token_hex(24)


@dataclass(frozen=True)
class Org:
    name: str
    created: float
    limits: dict[str, Any] = field(default_factory=lambda: Limits().as_dict())
    status: str = "active"                           # or suspended: its requests are refused
    title: str = ""

    @property
    def ceilings(self) -> Limits:
        return Limits.from_dict(self.limits)


@dataclass(frozen=True)
class Member:
    org: str
    name: str
    role: str
    created: float

    @property
    def owner(self) -> bool:
        return self.role == "owner"


@dataclass(frozen=True)
class Team:
    org: str
    name: str
    created: float
    members: list[str] = field(default_factory=list)
    title: str = ""


@dataclass(frozen=True)
class ApiKey:
    id: str
    org: str
    member: str
    label: str
    created: float
    revoked: bool = False


@dataclass(frozen=True)
class Provider:
    org: str
    name: str
    url: str
    api: str
    key: str = field(repr=False)                     # sealed

    @property
    def path(self) -> str:
        """The URL's path, which the gateway's URL carries too, so a CLI's base-URL idioms hold."""
        from urllib.parse import urlsplit
        return urlsplit(self.url).path.rstrip("/")

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "url": self.url, "api": self.api}


@dataclass(frozen=True)
class Harness:
    org: str
    name: str
    head: str                                        # the current commit
    created: float
    team: str = ""                                   # "" is the whole organization
    origin: str = ""                                 # the catalog package or harness it was forked from
    config: dict[str, Any] = field(default_factory=dict)   # harness.json at the head, as JSON

    def configuration(self) -> harness.Config:
        return harness.load_config_dict(self.config)

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "team": self.team, "head": self.head, "created": self.created, "origin": self.origin, **self.config}


@dataclass(frozen=True)
class Worker:
    id: str
    name: str
    created: float
    last_seen: float
    slots: int = 0
    running: int = 0
    version: str = ""


class Store:
    def __init__(self, records: Records, blobs: Blobs, vault: Vault, git_root: Path, allow_private: bool = False):
        self.records, self.blobs, self.vault, self.allow_private = records, blobs, vault, allow_private
        self.git_root = Path(git_root)

    # organizations
    def create_org(self, name: str, limits: dict[str, Any] | None = None, title: str = "") -> Org:
        check_name(name, "organization")
        if name in RESERVED:
            raise Invalid(f"{name} is a reserved name")
        if self.records.get("org", PLATFORM, name):
            raise Conflict(f"organization {name} exists")
        o = Org(name, time.time(), Limits.from_dict(limits).as_dict(), title=title[:80])
        self.records.put("org", PLATFORM, name, asdict(o))
        return o

    def org(self, name: str) -> Org:
        d = self.records.get("org", PLATFORM, name) if NAME.fullmatch(name or "") else None
        if d is None:
            raise NotFound(f"no organization {name}")
        return Org(**d)

    def orgs(self) -> list[Org]:
        return [Org(**d) for d in self.records.list("org", PLATFORM)]

    def update_org(self, name: str, *, limits: dict[str, Any] | None = None, status: str | None = None, title: str | None = None) -> Org:
        o = self.org(name)
        if status is not None and status not in ("active", "suspended"):
            raise Invalid("an organization's status is active or suspended")
        o = Org(o.name, o.created, Limits.from_dict(limits).as_dict() if limits is not None else o.limits, status or o.status,
                (title or "")[:80] if title is not None else o.title)
        self.records.put("org", PLATFORM, name, asdict(o))
        return o

    def delete_org(self, name: str) -> None:
        self.org(name)
        for h in self.harnesses(name):
            Repo(self.repo_path(name, h.name)).delete()
        for kind in ("member", "team", "key", "provider", "harness", "session", "response", "turn", "chunk", "usage", "trace", "rodeo", "invite"):
            for d in self.records.list(kind, name):
                self.records.delete(kind, name, d.get("id") or d.get("name") or d.get("key"))
        for login in self.records.where("login", "org", name, limit=10_000):
            self.records.delete("login", PLATFORM, login[1])
        self.blobs.delete(f"tenants/{name}")
        self.records.delete("org", PLATFORM, name)

    # members, teams and keys
    def create_member(self, org: str, name: str, role: str) -> Member:
        self.org(org)
        check_name(name, "member")
        if role not in ROLES:
            raise Invalid(f"a role is one of {', '.join(ROLES)}")
        if self.records.get("member", org, name):
            raise Conflict(f"member {name} exists")
        m = Member(org, name, role, time.time())
        self.records.put("member", org, name, asdict(m))
        return m

    def member(self, org: str, name: str) -> Member:
        d = self.records.get("member", org, name) if NAME.fullmatch(name or "") else None
        if d is None:
            raise NotFound(f"no member {name}")
        return Member(**d)

    def members(self, org: str) -> list[Member]:
        return [Member(**d) for d in self.records.list("member", org)]

    def set_role(self, org: str, name: str, role: str) -> Member:
        m = self.member(org, name)
        if role not in ROLES:
            raise Invalid(f"a role is one of {', '.join(ROLES)}")
        if m.owner and role != "owner" and sum(1 for x in self.members(org) if x.owner) == 1:
            raise Invalid("an organization keeps at least one owner")
        m = Member(org, name, role, m.created)
        self.records.put("member", org, name, asdict(m))
        return m

    def remove_member(self, org: str, name: str) -> None:
        m = self.member(org, name)
        if m.owner and sum(1 for x in self.members(org) if x.owner) == 1:
            raise Invalid("an organization keeps at least one owner")
        for t in self.teams(org):
            if name in t.members:
                self.put_team(org, t.name, [x for x in t.members if x != name], t.title)
        for k in self.keys(org):
            if k.member == name and not k.revoked:
                self.revoke_key(org, k.id)
        self.records.delete("member", org, name)

    def put_team(self, org: str, name: str, members: list[str], title: str = "") -> Team:
        self.org(org)
        check_name(name, "team")
        known = {m.name for m in self.members(org)}
        if unknown := sorted(set(members) - known):
            raise Invalid(f"not members of {org}: {', '.join(unknown)}")
        existing = self.records.get("team", org, name)
        t = Team(org, name, existing["created"] if existing else time.time(), sorted(set(members)), (title or (existing or {}).get("title", ""))[:80])
        self.records.put("team", org, name, asdict(t))
        return t

    def team(self, org: str, name: str) -> Team:
        d = self.records.get("team", org, name) if NAME.fullmatch(name or "") else None
        if d is None:
            raise NotFound(f"no team {name}")
        return Team(**d)

    def teams(self, org: str) -> list[Team]:
        return [Team(**d) for d in self.records.list("team", org)]

    def delete_team(self, org: str, name: str) -> None:
        self.team(org, name)
        if used := [h.name for h in self.harnesses(org) if h.team == name]:
            raise Conflict(f"team {name} owns harnesses: {', '.join(used)}; move or delete them first")
        self.records.delete("team", org, name)

    def in_team(self, who: Member, team: str) -> bool:
        """Owners are in every team; a member is in the teams that list them."""
        if not team or who.owner:
            return True
        try:
            return who.name in self.team(who.org, team).members
        except NotFound:
            return False

    def mint_key(self, org: str, member: str, label: str = "") -> tuple[ApiKey, str]:
        """A new key for a member; the secret is returned once and only its digest is kept."""
        self.member(org, member)
        secret = KEY_PREFIX + secrets.token_urlsafe(32)
        key = ApiKey(uuid.uuid4().hex[:12], org, member, label[:80], time.time())
        self.records.put("key", org, key.id, asdict(key))
        self.records.put("login", PLATFORM, digest_of(secret), {"org": org, "id": key.id})
        return key, secret

    def keys(self, org: str) -> list[ApiKey]:
        return sorted((ApiKey(**d) for d in self.records.list("key", org)), key=lambda k: k.created)

    def revoke_key(self, org: str, key_id: str) -> ApiKey:
        d = self.records.get("key", org, key_id) if re.fullmatch(r"[0-9a-f]{12}", key_id or "") else None
        if d is None:
            raise NotFound(f"no key {key_id}")
        k = ApiKey(**{**d, "revoked": True})
        self.records.put("key", org, key_id, asdict(k))
        return k

    def resolve_key(self, secret: str) -> Member | None:
        """The member a presented secret belongs to, if it is a live key of an active organization."""
        if not secret.startswith(KEY_PREFIX):
            return None
        login = self.records.get("login", PLATFORM, digest_of(secret))
        if not login:
            return None
        key = self.records.get("key", login["org"], login["id"])
        if key is None or key["revoked"]:
            return None
        try:
            if self.org(key["org"]).status != "active":
                return None
            return self.member(key["org"], key["member"])
        except NotFound:
            return None

    # providers
    def put_provider(self, org: str, name: str, url: str, api: str, key: str) -> Provider:
        self.org(org)
        check_name(name, "provider")
        if api not in APIS:
            raise Invalid(f"a provider's api is one of {', '.join(APIS)}")
        if not url.startswith("https://"):
            raise Invalid("a provider's url is https://…")
        try:
            check_host(url, self.allow_private)
        except ValueError as e:
            raise Invalid(f"a provider's url: {e}") from None
        if (api == "none") != (not key):
            raise Invalid("a provider has a key, unless its api is none")
        if any(c in key for c in "\r\n\0"):
            raise Invalid("a provider's key is one line")
        sealed = self.vault.seal(key, f"{org}/providers/{name}") if key else ""
        p = Provider(org, name, url.rstrip("/"), api, sealed)
        self.records.put("provider", org, name, asdict(p))
        return p

    def provider(self, org: str, name: str) -> Provider:
        d = self.records.get("provider", org, name) if NAME.fullmatch(name or "") else None
        if d is None:
            raise NotFound(f"no provider {name}")
        return Provider(**d)

    def providers(self, org: str) -> list[Provider]:
        return [Provider(**d) for d in self.records.list("provider", org)]

    def delete_provider(self, org: str, name: str) -> None:
        if not self.records.delete("provider", org, name):
            raise NotFound(f"no provider {name}")

    def open_key(self, p: Provider) -> str:
        """The provider's key, in the clear: only the gateway calls this, for one upstream request."""
        return self.vault.open(p.key, f"{p.org}/providers/{p.name}") if p.key else ""

    # harness packages: git repositories, mirrored as bundles, packed per commit for workers
    def repo_path(self, org: str, name: str) -> Path:
        return self.git_root / org / f"{name}.git"

    @staticmethod
    def harness_blob(org: str, name: str, sha: str) -> str:
        return f"tenants/{org}/harnesses/{name}/{sha}.tgz"

    @staticmethod
    def bundle_blob(org: str, name: str) -> str:
        return f"tenants/{org}/harnesses/{name}/repo.bundle"

    def repo(self, org: str, name: str) -> Repo:
        """The harness's repository on this node, restored from its mirrored bundle when this node has
        no copy (another replica took the last commit)."""
        h = self.harness(org, name)
        r = Repo(self.repo_path(org, name))
        if not r.exists or (h.head and r.head() != h.head and not r.resolve_quiet(h.head)):
            r = Repo.from_bundle(self.repo_path(org, name), self.blobs.get(self.bundle_blob(org, name)))
        return r

    def _validated(self, repo: Repo, sha: str, org: str) -> harness.Config:
        """The commit as a package: valid, with egress the platform allows; its files packed for workers."""
        with tempfile.TemporaryDirectory(prefix="hg-check-") as tmp:
            folder = Path(tmp) / "folder"
            repo.checkout(folder, sha)
            config = harness.validate(folder)
            for host in config.egress:
                try:
                    check_host(host, self.allow_private)
                except ValueError as e:
                    raise Invalid(f"harness.json egress: {e}") from None
            self.blobs.put(self.harness_blob(org, repo.path.stem, sha), harness.pack(folder))
        return config

    def _settle(self, org: str, name: str, repo: Repo, sha: str, team: str, origin: str, created: float | None) -> Harness:
        config = self._validated(repo, sha, org)
        existing = self.records.get("harness", org, name)
        h = Harness(org, name, sha, created if created is not None else (existing["created"] if existing else time.time()),
                    team, origin, config.as_json())
        self.records.put("harness", org, name, asdict(h))
        self.blobs.put(self.bundle_blob(org, name), repo.bundle())
        return h

    def _new_repo(self, org: str, name: str, team: str) -> Repo:
        o = self.org(org)
        check_name(name, "harness")
        if name in RESERVED:
            raise Invalid(f"{name} is a reserved name")
        if team:
            self.team(org, team)
        if self.records.get("harness", org, name):
            raise Conflict(f"harness {name} exists")
        return Repo.init(self.repo_path(org, name))

    def create_harness_from_archive(self, org: str, name: str, archive: bytes, author: str, team: str = "", origin: str = "",
                                    message: str = "import") -> Harness:
        """A new harness whose first commit is the archive's files."""
        o = self.org(org)
        if len(archive) > o.ceilings.upload_mb * 1024 * 1024:
            raise Invalid(f"the package is over the organization's upload limit of {o.ceilings.upload_mb} MB")
        repo = self._new_repo(org, name, team)
        try:
            with tempfile.TemporaryDirectory(prefix="hg-import-") as tmp:
                folder = Path(tmp) / "folder"
                folder.mkdir()
                harness.unpack(archive, folder)
                harness.validate(folder)
                sha = repo.import_tree(folder, message, author)
            return self._settle(org, name, repo, sha, team, origin, None)
        except Exception:
            repo.delete()
            raise

    def create_harness_from_folder(self, org: str, name: str, folder: Path, author: str, team: str = "", origin: str = "",
                                   message: str = "fork") -> Harness:
        """A new harness from a directory on this node (a catalog package)."""
        repo = self._new_repo(org, name, team)
        try:
            harness.validate(Path(folder))
            sha = repo.import_tree(Path(folder), message, author)
            return self._settle(org, name, repo, sha, team, origin, None)
        except Exception:
            repo.delete()
            raise

    def fork_harness(self, org: str, name: str, into: str, author: str, team: str = "") -> Harness:
        """A copy of a harness, history and all, as another harness of the organization."""
        src = self.harness(org, name)
        repo = self.repo(org, name)
        check_name(into, "harness")
        if into in RESERVED or self.records.get("harness", org, into):
            raise Conflict(f"harness {into} exists") if into not in RESERVED else Invalid(f"{into} is a reserved name")
        if team:
            self.team(org, team)
        forked = repo.fork(self.repo_path(org, into))
        try:
            return self._settle(org, into, forked, forked.head(), team, f"{org}/{name}", None)
        except Exception:
            forked.delete()
            raise

    def commit_harness(self, org: str, name: str, changes: dict[str, bytes | None], message: str, author: str,
                       parent: str | None = None) -> Harness:
        """Apply file changes as a commit; the whole package must still validate, else nothing moves."""
        h = self.harness(org, name)
        repo = self.repo(org, name)
        sha = repo.commit(changes, message, author, parent if parent is not None else h.head)
        try:
            return self._settle(org, name, repo, sha, h.team, h.origin, h.created)
        except Invalid:
            repo._git("update-ref", "refs/heads/main", h.head) if h.head else None   # the bad commit is unreachable
            raise

    def replace_harness(self, org: str, name: str, archive: bytes, message: str, author: str) -> Harness:
        """The archive's files as the next commit: the whole package, files not in it removed."""
        h = self.harness(org, name)
        o = self.org(org)
        if len(archive) > o.ceilings.upload_mb * 1024 * 1024:
            raise Invalid(f"the package is over the organization's upload limit of {o.ceilings.upload_mb} MB")
        repo = self.repo(org, name)
        with tempfile.TemporaryDirectory(prefix="hg-import-") as tmp:
            folder = Path(tmp) / "folder"
            folder.mkdir()
            harness.unpack(archive, folder)
            harness.validate(folder)
            sha = repo.import_tree(folder, message, author)
        if sha == h.head:
            return h
        try:
            return self._settle(org, name, repo, sha, h.team, h.origin, h.created)
        except Invalid:
            repo._git("update-ref", "refs/heads/main", h.head)
            raise

    def move_harness(self, org: str, name: str, team: str) -> Harness:
        h = self.harness(org, name)
        if team:
            self.team(org, team)
        h = Harness(**{**asdict(h), "team": team})
        self.records.put("harness", org, name, asdict(h))
        return h

    def harness(self, org: str, name: str) -> Harness:
        d = self.records.get("harness", org, name) if NAME.fullmatch(name or "") else None
        if d is None:
            raise NotFound(f"no harness {name}")
        return Harness(**d)

    def harnesses(self, org: str) -> list[Harness]:
        return [Harness(**d) for d in self.records.list("harness", org)]

    def visible_harnesses(self, who: Member) -> list[Harness]:
        return [h for h in self.harnesses(who.org) if self.in_team(who, h.team)]

    def harness_files(self, org: str, name: str, sha: str | None = None) -> list[Entry]:
        return self.repo(org, name).tree(sha or self.harness(org, name).head)

    def harness_file(self, org: str, name: str, path: str, sha: str | None = None) -> bytes:
        return self.repo(org, name).read(path, sha or self.harness(org, name).head)

    def harness_log(self, org: str, name: str, limit: int = 100) -> list[Commit]:
        return self.repo(org, name).log(limit)

    def harness_export(self, org: str, name: str, sha: str | None = None) -> bytes:
        return self.repo(org, name).export(sha or self.harness(org, name).head)

    def harness_config(self, org: str, name: str, sha: str) -> harness.Config:
        """harness.json as it was at a commit a session runs on."""
        h = self.harness(org, name)
        if sha == h.head:
            return h.configuration()
        return harness.load_config(self.repo(org, name).read("harness.json", sha).decode(errors="replace"))

    def delete_harness(self, org: str, name: str) -> None:
        self.harness(org, name)
        Repo(self.repo_path(org, name)).delete()
        self.blobs.delete(f"tenants/{org}/harnesses/{name}")
        self.records.delete("harness", org, name)

    # the platform's own
    def set_deployer_key(self) -> str:
        """A new deployer key, shown once; replaces the previous one."""
        secret = "hgd_" + secrets.token_urlsafe(32)
        self.records.put("deployer", PLATFORM, "key", {"digest": digest_of(secret), "created": time.time()})
        return secret

    def has_deployer_key(self) -> bool:
        return self.records.get("deployer", PLATFORM, "key") is not None

    def is_deployer(self, secret: str) -> bool:
        d = self.records.get("deployer", PLATFORM, "key")
        return bool(d) and secrets.compare_digest(d["digest"], digest_of(secret))

    def register_worker(self, name: str, slots: int, version: str) -> tuple[Worker, str]:
        secret = "hgw_" + secrets.token_urlsafe(32)
        w = Worker(uuid.uuid4().hex[:12], name, time.time(), time.time(), slots, 0, version)
        self.records.put("worker", PLATFORM, w.id, {**asdict(w), "digest": digest_of(secret)})
        return w, secret

    def worker_by_secret(self, secret: str) -> Worker | None:
        if not secret.startswith("hgw_"):
            return None
        for d in self.records.list("worker", PLATFORM):
            if secrets.compare_digest(d["digest"], digest_of(secret)):
                return Worker(**{k: v for k, v in d.items() if k != "digest"})
        return None

    def touch_worker(self, w: Worker, running: int) -> None:
        d = self.records.get("worker", PLATFORM, w.id)
        if d:
            self.records.put("worker", PLATFORM, w.id, {**d, "last_seen": time.time(), "running": running})

    def workers(self) -> list[Worker]:
        return [Worker(**{k: v for k, v in d.items() if k != "digest"}) for d in self.records.list("worker", PLATFORM)]

    def forget_worker(self, worker_id: str) -> None:
        self.records.delete("worker", PLATFORM, worker_id)

    def issue_token(self, org: str, provider: str, session: str, response: str, api: str, ttl_s: float) -> str:
        """A session token the gateway will swap for the provider's key, for one response."""
        secret = session_token(api)
        self.records.put("token", PLATFORM, digest_of(secret), {"org": org, "provider": provider, "session": session,
                                                                 "response": response, "expires": time.time() + ttl_s})
        return secret

    def resolve_token(self, secret: str) -> dict[str, Any] | None:
        d = self.records.get("token", PLATFORM, digest_of(secret))
        return d if d and d["expires"] > time.time() else None

    def revoke_token(self, secret: str) -> None:
        self.records.delete("token", PLATFORM, digest_of(secret))
