"""The first boot makes what a fresh platform lacks, once, and the base image is pulled, or built
when it cannot be."""
import asyncio

from harnessgateway.__main__ import first_boot
from harnessgateway.api.app import Service
from harnessgateway.config import Settings
from harnessgateway.backends.sandboxes import docker


def service(tmp_path, vault, **kw):
    s = Settings(data_dir=tmp_path, **kw)
    return Service(s, vault), s


def test_first_boot_makes_the_deployer_key_and_the_first_org_once(tmp_path, vault):
    svc, s = service(tmp_path, vault, org="acme", owner="chase")
    lines = first_boot(svc, s)
    assert [line.split(":")[0] for line in lines] == ["deployer key", "org acme created with owner chase", "chase's API key"]
    deployer, key = lines[0].split(": ")[1], lines[2].split(": ")[1]
    assert deployer.startswith("hgd_") and svc.keys.is_deployer(deployer)
    who = svc.keys.resolve(key)
    assert who and who.org == "acme" and who.name == "chase" and who.owner
    assert first_boot(svc, s) == []                  # the second boot makes nothing
    assert svc.keys.is_deployer(deployer) and [t.name for t in svc.orgs.list()] == ["acme"]


def test_first_boot_with_sign_up_open_makes_no_org(tmp_path, vault):
    svc, s = service(tmp_path, vault, signup="open")
    assert [line.split(":")[0] for line in first_boot(svc, s)] == ["deployer key"]
    assert svc.orgs.list() == []


def test_first_boot_keeps_a_deployer_key_given_in_the_environment(tmp_path, vault):
    svc, s = service(tmp_path, vault, deployer_key="hgd_from-the-environment")
    assert [line.split(":")[0] for line in first_boot(svc, s)] == ["org main created with owner owner", "owner's API key"]
    assert not svc.keys.has_deployer() and svc.principals.is_deployer("hgd_from-the-environment")


def test_a_worker_alone_boots_nothing(tmp_path, vault):
    svc, s = service(tmp_path, vault, roles=("worker",))
    assert first_boot(svc, s) == [] and not svc.keys.has_deployer() and svc.orgs.list() == []


def test_the_base_image_is_pulled_or_built(monkeypatch):
    calls = []

    async def pull(reference):
        calls.append(("pull", reference))
        if "ghcr.io" in reference:
            raise RuntimeError(f"image {reference} could not be pulled:\nno route")
        return reference

    async def build_base():
        calls.append(("build", ""))
        return "hg-base:built"

    monkeypatch.setattr(docker, "pull", pull)
    monkeypatch.setattr(docker, "build_base", build_base)
    assert asyncio.run(docker.base_image()) == "hg-base:built"
    assert calls == [("pull", docker.BASE_IMAGE), ("build", "")]


def test_a_node_without_an_image_takes_the_shipped_one(monkeypatch):
    async def base_image():
        return "hg-base:shipped"

    async def pull(reference):
        return reference

    async def identity(reference):
        return "id-" + reference, "amd64"

    async def inspect_self():
        return docker.Host()
    monkeypatch.setattr(docker, "base_image", base_image)
    monkeypatch.setattr(docker, "pull", pull)
    monkeypatch.setattr(docker, "identity", identity)
    monkeypatch.setattr(docker, "inspect_self", inspect_self)
    node = docker.DockerNode("", "0123456789ab")
    assert asyncio.run(node.prepare()) == "id-hg-base:shipped" and node.image == "hg-base:shipped"
    node = docker.DockerNode("python:3.12-slim", "0123456789ab")
    assert asyncio.run(node.prepare()) == "id-python:3.12-slim"


def test_doctor_tries_every_backend_and_reports_one_line_each(tmp_path, vault, monkeypatch):
    from harnessgateway.__main__ import doctor_checks
    from harnessgateway.kernel.vault import Vault
    monkeypatch.setenv("HG_MASTER_KEY", Vault.generate())
    monkeypatch.delenv("HG_MASTER_KEY_FILE", raising=False)
    settings = Settings(data_dir=tmp_path / "data", sandbox="process")
    results = doctor_checks(settings)
    assert [name for name, _, _ in results] == ["records", "kms", "blobs", "events", "sandbox"]
    assert all(state.startswith("ok") for _, _, state in results), results
    assert dict((n, w) for n, w, _ in results) == {"records": "sqlite", "kms": "local", "blobs": "dir", "events": "polling", "sandbox": "process"}
    # the probes leave nothing behind
    from harnessgateway.backends.records import open_records
    assert open_records(settings.records_url).list("doctor", "") == []
