"""Spec §8.5, §13.9, §14.2: harnesses, their installs, and uploaded files, as they are stored."""

import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from harnessgateway import packaging
from harnessgateway.domain.sealing import Sealer
from harnessgateway.errors import Invalid, NotFound, TooLarge
from harnessgateway.store import files as files_module
from harnessgateway.store.blobs import BlobMissing, DirectoryBlobs
from harnessgateway.store.db import Database
from harnessgateway.store.files import Files
from harnessgateway.store.harnesses import Harnesses, package_key

SHELL = Path(__file__).parents[2] / "packages" / "shell"
DEMO = {
    "contract": 1, "name": "demo", "version": "1.0.0", "mind": ".demo", "apis": ["anthropic"],
    "settings": {"EFFORT": {"default": "medium"}, "STYLE": {}},
    "secrets": {"GITHUB_TOKEN": {"hosts": ["api.github.com"]}, "OTHER_TOKEN": {"hosts": ["other.example.com"]}},
}


def demo_archive(tmp_path: Path, **changes: object) -> bytes:
    folder = tmp_path / f"demo-{len(list(tmp_path.iterdir()))}"
    shutil.copytree(SHELL, folder)
    (folder / "harness.json").write_text(json.dumps({**DEMO, **changes}))
    return packaging.pack(folder)


@pytest.fixture
def harnesses(db: Database, blobs: DirectoryBlobs) -> Harnesses:
    return Harnesses(db, blobs, Sealer(bytes(range(32))))


async def test_a_package_is_added_as_a_harness(harnesses: Harnesses, blobs: DirectoryBlobs, org: str) -> None:
    archive = packaging.pack(SHELL)
    harness = await harnesses.add(org, archive)
    assert (harness.name, harness.manifest.version, harness.provider, harness.model) == ("shell", "1.0.0", None, None)
    assert await harnesses.get(org, "shell") == harness
    assert [h.name for h in await harnesses.all(org)] == ["shell"]
    assert await blobs.get(package_key(org, harness.digest)) == archive


async def test_an_invalid_package_stores_nothing(harnesses: Harnesses, org: str) -> None:
    with pytest.raises(Invalid, match="gzip-compressed tar"):
        await harnesses.add(org, b"not an archive")
    assert await harnesses.all(org) == []


async def test_defaults_settings_and_secrets_are_set_and_unset(harnesses: Harnesses, org: str, tmp_path: Path) -> None:
    await harnesses.add(org, demo_archive(tmp_path))
    changed = await harnesses.update(org, "demo", {
        "provider": "deepseek", "model": "deepseek-flash", "settings": {"EFFORT": "high", "STYLE": "terse"},
        "secrets": {"GITHUB_TOKEN": "the-token"}})
    assert (changed.provider, changed.model) == ("deepseek", "deepseek-flash")
    assert changed.setting_values() == {"EFFORT": "high", "STYLE": "terse"}
    assert changed.secrets_set == {"GITHUB_TOKEN"}
    assert "the-token" not in repr(changed)
    assert await harnesses.secret_values(org, "demo") == {"GITHUB_TOKEN": "the-token"}

    unset = await harnesses.update(org, "demo", {"model": None, "settings": {"EFFORT": None},
                                                "secrets": {"GITHUB_TOKEN": None}})
    assert (unset.provider, unset.model) == ("deepseek", None)
    assert unset.setting_values() == {"EFFORT": "medium", "STYLE": "terse"}          # back to the package's default
    assert unset.secrets_set == frozenset()


@pytest.mark.parametrize(("changes", "message"), [
    ({"settings": {"UNKNOWN": "x"}}, r"settings\.UNKNOWN is not declared by the package; declared: EFFORT, STYLE"),
    ({"secrets": {"UNKNOWN": "x"}}, r"secrets\.UNKNOWN is not declared"),
    ({"settings": {"EFFORT": 5}}, r"settings\.EFFORT is a string"),
    ({"provider": "Not A Name"}, "provider"),
    ({"model": ""}, "model"),
    ({"digest": "x"}, "digest cannot be changed"),
    ({"settings": ["EFFORT"]}, "settings is an object"),
])
async def test_a_change_that_is_not_allowed_is_refused(
    harnesses: Harnesses, org: str, tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    await harnesses.add(org, demo_archive(tmp_path))
    with pytest.raises(Invalid, match=message):
        await harnesses.update(org, "demo", changes)


async def test_replacing_a_harness_keeps_what_the_new_manifest_still_declares(
    harnesses: Harnesses, org: str, tmp_path: Path
) -> None:
    first = await harnesses.add(org, demo_archive(tmp_path))
    await harnesses.update(org, "demo", {
        "provider": "deepseek", "settings": {"EFFORT": "high", "STYLE": "terse"},
        "secrets": {"GITHUB_TOKEN": "the-token", "OTHER_TOKEN": "other"}})

    replaced = await harnesses.add(org, demo_archive(
        tmp_path, version="2.0.0", settings={"EFFORT": {"default": "low"}},
        secrets={"GITHUB_TOKEN": {"hosts": ["api.github.com"]}}))
    assert replaced.manifest.version == "2.0.0"
    assert replaced.digest != first.digest
    assert replaced.provider == "deepseek"
    assert replaced.setting_values() == {"EFFORT": "high"}
    assert await harnesses.secret_values(org, "demo") == {"GITHUB_TOKEN": "the-token"}
    assert [h.name for h in await harnesses.all(org)] == ["demo"]


async def test_a_harness_is_deleted(harnesses: Harnesses, org: str) -> None:
    await harnesses.add(org, packaging.pack(SHELL))
    await harnesses.delete(org, "shell")
    with pytest.raises(NotFound, match="shell"):
        await harnesses.get(org, "shell")
    with pytest.raises(NotFound):
        await harnesses.delete(org, "shell")
    with pytest.raises(NotFound):
        await harnesses.update(org, "shell", {"model": "x"})


async def test_an_install_is_one_per_package_and_image(harnesses: Harnesses, org: str) -> None:
    harness = await harnesses.add(org, packaging.pack(SHELL))
    first = await harnesses.install_for(org, harness.digest, "sha256:image-a")
    assert (first.status, first.log_tail) == ("pending", "")
    assert first.id.startswith("inst_")
    assert (await harnesses.install_for(org, harness.digest, "sha256:image-a")).id == first.id
    other = await harnesses.install_for(org, harness.digest, "sha256:image-b")
    assert other.id != first.id
    assert [i.id for i in await harnesses.installs(org, harness.digest)] == [first.id, other.id]


async def test_an_install_s_token_lives_only_while_it_is_being_installed(harnesses: Harnesses, org: str) -> None:
    harness = await harnesses.add(org, packaging.pack(SHELL))
    install = await harnesses.install_for(org, harness.digest, "sha256:image-a")
    await harnesses.begin_install(org, install.id, step_token_hash="hash-of-step", proxy_token_hash="hash-of-proxy")
    assert await harnesses.install_of_token("hash-of-step") == (org, install.id, package_key(org, harness.digest))
    assert await harnesses.install_of_token("hash-of-another") is None

    await harnesses.end_install(org, install.id, ready=False, log="x" * 100_000 + "the end")
    failed = await harnesses.install(org, install.id)
    assert failed.status == "failed"
    assert len(failed.log_tail) == 64 * 1024
    assert failed.log_tail.endswith("the end")
    assert await harnesses.install_of_token("hash-of-step") is None

    await harnesses.begin_install(org, install.id, step_token_hash="again", proxy_token_hash="again-proxy")
    assert (await harnesses.install(org, install.id)).status == "pending"
    await harnesses.end_install(org, install.id, ready=True, log="installed")
    assert (await harnesses.install(org, install.id)).status == "ready"


# ── files ───────────────────────────────────────────────────────────────────────────────────────────

async def parts(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


@pytest.fixture
def files(db: Database, blobs: DirectoryBlobs) -> Files:
    return Files(db, blobs)


async def test_a_file_is_stored_read_listed_and_deleted(files: Files, blobs: DirectoryBlobs, org: str) -> None:
    first = await files.create(org, filename="report.pdf", purpose="user_data", parts=parts(b"%PDF", b"-1.7"))
    second = await files.create(org, filename="data.csv", purpose="user_data", parts=parts(b"a,b"))
    assert (first.filename, first.purpose, first.bytes) == ("report.pdf", "user_data", 8)
    assert first.id.startswith("file_")
    assert await files.get(org, first.id) == first
    assert b"".join([piece async for piece in files.content(first)]) == b"%PDF-1.7"
    assert [f.id for f in await files.page(org)] == [second.id, first.id]
    assert [f.id for f in await files.page(org, after=second.id)] == [first.id]
    assert [f.id for f in await files.page(org, limit=1)] == [second.id]

    await files.delete(org, first.id)
    with pytest.raises(NotFound):
        await files.get(org, first.id)
    with pytest.raises(BlobMissing):
        await blobs.get(first.object_key)
    with pytest.raises(NotFound):
        await files.delete(org, first.id)


@pytest.mark.parametrize("filename", ["", "a/b", "..", ".", "x" * 256, "a\x00b"])
async def test_a_file_name_that_is_not_a_plain_name_is_refused(files: Files, org: str, filename: str) -> None:
    with pytest.raises(Invalid, match="file name"):
        await files.create(org, filename=filename, purpose="user_data", parts=parts(b"x"))


async def test_a_file_over_the_limit_is_refused_and_leaves_nothing(
    files: Files, org: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files_module, "FILE_MAX", 10)
    with pytest.raises(TooLarge, match="at most"):
        await files.create(org, filename="big", purpose="user_data", parts=parts(b"123456", b"7890123"))
    assert await files.page(org) == []
    assert [p for p in (tmp_path / "blobs").rglob("*") if p.is_file()] == []
