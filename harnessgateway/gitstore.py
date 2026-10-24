"""Layer 2: a package as a git repository. Every harness an organization owns is a bare repository
with one branch, `main`; a save is a commit; a fork is a clone; an export is `git archive`. The
platform drives git's plumbing so no working tree is needed, and it moves a whole repository as a
bundle (one file) between the node that holds it and the blob store.

Nothing here knows what a package contains: `harness.validate` runs on an export before a commit
is accepted, in the layer above.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import Invalid, NotFound

BRANCH = "main"
REF = f"refs/heads/{BRANCH}"
SHA = re.compile(r"[0-9a-f]{40}")
MODE_FILE, MODE_EXEC = "100644", "100755"


def checked_sha(sha: str) -> str:
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise Invalid(f"a commit is 40 hex digits, not {sha!r}")
    return sha


def checked_path(rel: str) -> str:
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts or rel.startswith("/") or any("\0" in p for p in parts) or ".git" in parts:
        raise Invalid(f"a file's path is relative, inside the package and not under .git: {rel!r}")
    return "/".join(parts)


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    at: float
    message: str


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    executable: bool


class Repo:
    """One bare repository."""

    def __init__(self, path: Path):
        self.path = Path(path)

    # the git command
    def _git(self, *args: str, stdin: bytes | None = None, env: dict[str, str] | None = None, check: bool = True) -> bytes:
        full_env = {"GIT_DIR": str(self.path), "HOME": str(self.path), "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null", "PATH": os.environ.get("PATH", os.defpath), **(env or {})}
        proc = subprocess.run(["git", *args], input=stdin, capture_output=True, env=full_env)
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args[:2])} failed: {proc.stderr.decode(errors='replace').strip()[-500:]}")
        return proc.stdout

    @classmethod
    def init(cls, path: Path) -> Repo:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        repo = cls(path)
        subprocess.run(["git", "init", "--bare", "--quiet", f"--initial-branch={BRANCH}", str(path)], check=True, capture_output=True)
        return repo

    @property
    def exists(self) -> bool:
        return (self.path / "HEAD").is_file()

    def head(self) -> str:
        """The current commit, or '' before the first."""
        out = self._git("rev-parse", "--verify", "--quiet", REF, check=False).decode().strip()
        return out if SHA.fullmatch(out) else ""

    def resolve(self, sha: str | None) -> str:
        if not sha:
            head = self.head()
            if not head:
                raise NotFound("the repository has no commit yet")
            return head
        checked_sha(sha)
        if self._git("cat-file", "-t", sha, check=False).decode().strip() != "commit":
            raise NotFound(f"no commit {sha}")
        return sha

    def resolve_quiet(self, sha: str) -> bool:
        """Whether `sha` is a commit here."""
        return bool(SHA.fullmatch(sha or "")) and self._git("cat-file", "-t", sha, check=False).decode().strip() == "commit"

    # reading
    def tree(self, sha: str | None = None) -> list[Entry]:
        out = self._git("ls-tree", "-r", "-l", "-z", self.resolve(sha)).decode()
        entries = []
        for line in out.split("\0"):
            if not line:
                continue
            meta, path = line.split("\t", 1)
            mode, kind, _, size = meta.split()
            if kind == "blob":
                entries.append(Entry(path, int(size), mode == MODE_EXEC))
        return sorted(entries, key=lambda e: e.path)

    def read(self, rel: str, sha: str | None = None) -> bytes:
        rel = checked_path(rel)
        resolved = self.resolve(sha)
        if subprocess.run(["git", "cat-file", "-e", f"{resolved}:{rel}"], capture_output=True,
                          env={"GIT_DIR": str(self.path), "PATH": os.environ.get("PATH", os.defpath)}).returncode != 0:
            raise NotFound(f"no file {rel}")
        return self._git("cat-file", "-p", f"{resolved}:{rel}")

    def export(self, sha: str | None = None) -> bytes:
        """The commit's files as a gzipped tar, paths relative to the package."""
        return self._git("archive", "--format=tar.gz", self.resolve(sha))

    def checkout(self, into: Path, sha: str | None = None) -> str:
        """The commit's files extracted into `into` (made empty first); the commit's sha."""
        resolved = self.resolve(sha)
        into = Path(into)
        shutil.rmtree(into, ignore_errors=True)
        into.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(self.export(resolved)), mode="r:gz") as tar:
            tar.extractall(into, filter="data")
        return resolved

    def log(self, limit: int = 100) -> list[Commit]:
        if not self.head():
            return []
        out = self._git("log", f"--max-count={int(limit)}", "--format=%H%x00%an%x00%at%x00%B%x01", REF).decode(errors="replace")
        commits = []
        for record in out.split("\x01"):
            record = record.strip("\n")
            if not record:
                continue
            sha, author, at, message = record.split("\0", 3)
            commits.append(Commit(sha, author, float(at), message.strip()))
        return commits

    # writing
    def commit(self, changes: dict[str, bytes | None], message: str, author: str, parent: str | None = None) -> str:
        """A commit on top of `parent` (the head, by default) applying `changes`: a path to bytes writes
        or replaces the file, a path to None removes it. Its sha. Conflict-free by construction: the
        head must still be `parent` when the ref moves, else Invalid."""
        if not changes:
            raise Invalid("nothing to commit")
        head = self.head()
        base = parent if parent is not None else head
        if base and base != head:
            raise Invalid("the package changed since you read it; load it again")
        scratch = tempfile.mkdtemp(prefix="hg-index-")  # the index, and an empty work tree update-index insists on
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index"), "GIT_WORK_TREE": scratch}
        try:
            if base:
                self._git("read-tree", base, env=env)
            for rel, data in changes.items():
                rel = checked_path(rel)
                if data is None:
                    self._git("update-index", "--force-remove", "--", rel, env=env)
                    continue
                if not isinstance(data, bytes):
                    raise Invalid("a file's content is bytes")
                blob = self._git("hash-object", "-w", "--stdin", stdin=data).decode().strip()
                mode = MODE_EXEC if rel.endswith(".sh") or data.startswith(b"#!") else MODE_FILE
                self._git("update-index", "--add", "--cacheinfo", f"{mode},{blob},{rel}", env=env)
            tree = self._git("write-tree", env=env).decode().strip()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        name, email = split_author(author)
        when = f"{int(time.time())} +0000"
        ident = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": when,
                 "GIT_COMMITTER_NAME": "HarnessGateway", "GIT_COMMITTER_EMAIL": "platform@harnessgateway.ai", "GIT_COMMITTER_DATE": when}
        parents = ["-p", base] if base else []
        sha = self._git("commit-tree", tree, *parents, "-m", message.strip() or "update", env=ident).decode().strip()
        if base:
            self._git("update-ref", REF, sha, base)
        else:
            self._git("update-ref", REF, sha)
        return sha

    def reset(self, sha: str) -> None:
        """Move the branch back to `sha`, a commit here: what was committed after it stays in the
        object store, unreachable."""
        self._git("update-ref", REF, self.resolve(sha))

    def import_tree(self, folder: Path, message: str, author: str) -> str:
        """Every file under `folder` as the next commit: files absent from the folder are removed."""
        wanted = {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*"))
                  if p.is_file() and not p.is_symlink() and ".git" not in p.parts and "__pycache__" not in p.parts}
        if not wanted:
            raise Invalid("the package has no files")
        changes: dict[str, bytes | None] = dict(wanted)
        if self.head():
            for e in self.tree():
                if e.path not in wanted:
                    changes[e.path] = None
            current = {e.path: self.read(e.path) for e in self.tree()}
            if all(current.get(p) == b for p, b in wanted.items()) and len(current) == len(wanted):
                return self.head()                   # nothing changed: the head stands
        return self.commit(changes, message, author)

    # moving whole repositories
    def bundle(self) -> bytes:
        """The whole repository as one file."""
        if not self.head():
            raise NotFound("the repository has no commit yet")
        with tempfile.TemporaryDirectory(prefix="hg-bundle-") as tmp:
            out = Path(tmp) / "repo.bundle"
            self._git("bundle", "create", str(out), "--all")
            return out.read_bytes()

    @classmethod
    def from_bundle(cls, path: Path, data: bytes) -> Repo:
        path = Path(path)
        shutil.rmtree(path, ignore_errors=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="hg-bundle-") as tmp:
            src = Path(tmp) / "repo.bundle"
            src.write_bytes(data)
            proc = subprocess.run(["git", "clone", "--bare", "--quiet", str(src), str(path)], capture_output=True,
                                  env={"PATH": os.environ.get("PATH", os.defpath), "HOME": tmp, "GIT_CONFIG_NOSYSTEM": "1"})
            if proc.returncode != 0:
                raise Invalid(f"not a git bundle: {proc.stderr.decode(errors='replace').strip()[-300:]}")
        repo = cls(path)
        repo._git("symbolic-ref", "HEAD", REF)
        return repo

    def fork(self, into: Path) -> Repo:
        """A copy with the same history, as a new repository."""
        into = Path(into)
        shutil.rmtree(into, ignore_errors=True)
        into.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--bare", "--quiet", str(self.path), str(into)], check=True, capture_output=True,
                       env={"PATH": os.environ.get("PATH", os.defpath), "HOME": str(into.parent), "GIT_CONFIG_NOSYSTEM": "1"})
        repo = Repo(into)
        repo._git("symbolic-ref", "HEAD", REF)
        return repo

    def delete(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def split_author(author: str) -> tuple[str, str]:
    """'Name <email>' or a bare name; a platform address when no email is given."""
    m = re.fullmatch(r"\s*(.*?)\s*<([^<>]+)>\s*", author or "")
    if m:
        return (m.group(1) or m.group(2).split("@")[0]), m.group(2)
    name = (author or "member").strip() or "member"
    return name, f"{re.sub(r'[^a-z0-9._-]+', '-', name.lower()).strip('-') or 'member'}@harnessgateway.local"


def from_tar(archive: bytes, path: Path, message: str, author: str, unpack) -> Repo:
    """A new repository whose first commit is the archive's files; `unpack` extracts the archive."""
    repo = Repo(path)
    if not repo.exists:
        repo = Repo.init(path)
    with tempfile.TemporaryDirectory(prefix="hg-import-") as tmp:
        folder = Path(tmp) / "folder"
        folder.mkdir()
        unpack(archive, folder)
        repo.import_tree(folder, message, author)
    return repo
