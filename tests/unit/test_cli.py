"""Installing from the image alone: the Compose file it prints, the secrets it makes, and what the image says it is."""

import re
import tomllib
from pathlib import Path

import pytest

from harnessgateway import __version__, cli
from harnessgateway.settings import ProxySettings, Settings, SettingsError

ROOT = Path(__file__).parent.parent.parent
IMAGE = "ghcr.io/harnessgateway/harnessgateway:9.9.9"


def test_the_compose_file_names_the_image_it_came_from_and_builds_nothing(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HG_IMAGE", IMAGE)
    assert cli.main(["compose"]) == 0
    printed = capsys.readouterr().out
    assert printed == cli.compose({"HG_IMAGE": IMAGE}) and printed.count(f"${{HG_IMAGE:-{IMAGE}}}") == 1
    assert "harnessgateway:dev" not in printed and not re.search(r"^\s*build:", printed, re.M)
    assert "HG_EXTENSIONS" not in printed                          # the image says which extensions it carries
    serve, proxy = printed[printed.index("  serve:"):printed.index("  proxy:")], printed[printed.index("  proxy:"):printed.index("\nnetworks:")]
    assert "env_file: .env" in serve and "/readyz" in serve       # everything in .env reaches it, and it says when it can work
    assert "env_file" not in proxy and "HG_SECRET_KEY" not in proxy and "HG_DATABASE_URL" not in proxy and "DB_PASSWORD" not in proxy
    assert sorted(re.findall(r"^      (HG_\w+):", proxy, re.M)) == ["HG_LOG_LEVEL", "HG_PRIVATE_HOSTS", "HG_PROXY_KEY"]


def test_a_checkout_is_not_a_released_image_and_says_so(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HG_IMAGE", raising=False)
    assert cli.main(["compose"]) == 2
    assert "HG_IMAGE is not set: this is not a released image" in capsys.readouterr().err
    with pytest.raises(SettingsError):
        cli.compose({"HG_IMAGE": ""})


def test_the_secrets_are_new_each_time_and_are_settings_the_platform_takes(capsys: pytest.CaptureFixture[str]):
    assert cli.main(["secrets"]) == 0
    printed = capsys.readouterr().out
    made = dict(line.split("=", 1) for line in printed.splitlines())
    assert list(made) == ["HG_SECRET_KEY", "HG_PROXY_KEY", "HG_DB_PASSWORD", "HG_PORT", "HG_BIND", "HG_NAMESPACE"]
    settings = Settings.from_env({**made, "HG_DATABASE_URL": f"postgresql://hg:{made['HG_DB_PASSWORD']}@db:5432/hg"})
    assert settings.secret_key is not None and len(settings.secret_key) == 32 and settings.namespace == "hg"
    assert ProxySettings.from_env(made).proxy_key == made["HG_PROXY_KEY"] and re.fullmatch(r"[\w-]+", made["HG_DB_PASSWORD"])
    again = dict(line.split("=", 1) for line in cli.new_secrets().splitlines())
    assert all(again[name] != made[name] for name in ("HG_SECRET_KEY", "HG_PROXY_KEY", "HG_DB_PASSWORD"))


def test_one_version_wherever_it_is_named():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert project == __version__ and f"\nARG HG_IMAGE=ghcr.io/harnessgateway/harnessgateway:{project}\n" in dockerfile
    assert "HG_IMAGE=${HG_IMAGE} HG_EXTENSIONS=\n" in dockerfile   # the core's image carries no extension
    assert f"\n## {project}\n" in (ROOT / "CHANGELOG.md").read_text()
    assert f"It is version **{project}**" in (ROOT / "SPEC.md").read_text()
