"""Spec §18.4: settings come from the environment; a missing or invalid one stops the program and is named."""

import base64
from pathlib import Path

import pytest

import harnessgateway
from harnessgateway.settings import ProxySettings, Settings, SettingsError

KEY = base64.b64encode(bytes(range(32))).decode()
REQUIRED = {"HG_DATABASE_URL": "postgresql://u:p@db/hg", "HG_SECRET_KEY": KEY, "HG_PROXY_KEY": "proxy-secret"}


def test_with_only_the_required_settings_the_rest_take_their_defaults() -> None:
    s = Settings.from_env(REQUIRED)
    assert s.database_url == "postgresql://u:p@db/hg"
    assert s.secret_key == bytes(range(32))
    assert s.proxy_key == "proxy-secret"
    assert s.data == "/data"
    assert (s.port, s.internal_port) == (8000, 8001)
    assert (s.driver, s.runtime, s.max_turns, s.volume_ttl_s, s.log_level) == ("docker", "auto", 8, 3600, "info")
    assert s.platform_image == f"ghcr.io/harnessgateway/harnessgateway:{harnessgateway.__version__}"
    assert s.sandbox_image == f"ghcr.io/harnessgateway/sandbox:{harnessgateway.__version__}"
    assert s.internal_url == "http://serve:8001"
    assert s.private_hosts == ()
    assert s.extensions == ()


def test_every_setting_can_be_given() -> None:
    s = Settings.from_env({
        **REQUIRED, "HG_DATA": "/var/hg", "HG_DRIVER": "kubernetes", "HG_RUNTIME": "runsc",
        "HG_PLATFORM_IMAGE": "p:1", "HG_SANDBOX_IMAGE": "s:1", "HG_INTERNAL_URL": "http://10.0.0.5:8001/",
        "HG_MAX_TURNS": "32", "HG_VOLUME_TTL_S": "60", "HG_PRIVATE_HOSTS": "ollama.lan:11434, 192.168.1.20",
        "HG_EXTENSIONS": "acme.ext:Enterprise, acme.cloud:Cloud", "HG_LOG_LEVEL": "debug",
        "HG_PORT": "9000", "HG_INTERNAL_PORT": "9001",
    })
    assert (s.port, s.internal_port) == (9000, 9001)
    assert (s.data, s.driver, s.runtime, s.platform_image, s.sandbox_image) == (
        "/var/hg", "kubernetes", "runsc", "p:1", "s:1")
    assert s.internal_url == "http://10.0.0.5:8001"
    assert (s.max_turns, s.volume_ttl_s, s.log_level) == (32, 60, "debug")
    assert [(h.host, h.port) for h in s.private_hosts] == [("ollama.lan", 11434), ("192.168.1.20", None)]
    assert s.extensions == ("acme.ext:Enterprise", "acme.cloud:Cloud")


def test_every_missing_required_setting_is_named_at_once() -> None:
    with pytest.raises(SettingsError) as e:
        Settings.from_env({})
    for name in REQUIRED:
        assert name in str(e.value)


@pytest.mark.parametrize(("name", "value"), [
    ("HG_SECRET_KEY", "not base64 !!"),
    ("HG_SECRET_KEY", base64.b64encode(b"too short").decode()),
    ("HG_RUNTIME", "kata"),
    ("HG_MAX_TURNS", "0"),
    ("HG_MAX_TURNS", "many"),
    ("HG_VOLUME_TTL_S", "-1"),
    ("HG_PRIVATE_HOSTS", "https://x"),
    ("HG_EXTENSIONS", "no-colon"),
    ("HG_LOG_LEVEL", "loud"),
    ("HG_INTERNAL_URL", "serve:8001"),
    ("HG_PORT", "0"),
    ("HG_INTERNAL_PORT", "http"),
])
def test_an_invalid_setting_is_named(name: str, value: str) -> None:
    with pytest.raises(SettingsError) as e:
        Settings.from_env({**REQUIRED, name: value})
    assert name in str(e.value)


def test_the_error_never_shows_a_secret_s_value() -> None:
    with pytest.raises(SettingsError) as e:
        Settings.from_env({**REQUIRED, "HG_SECRET_KEY": "c2VjcmV0LXZhbHVl"})
    assert "c2VjcmV0LXZhbHVl" not in str(e.value)


def test_the_proxy_needs_only_its_key_and_holds_no_database_address() -> None:
    proxy = ProxySettings.from_env({"HG_PROXY_KEY": "k", "HG_PRIVATE_HOSTS": "ollama.lan:11434"})
    assert (proxy.proxy_key, proxy.internal_url, proxy.port, proxy.log_level) == (
        "k", "http://serve:8001", 8080, "info")
    assert [(h.host, h.port) for h in proxy.private_hosts] == [("ollama.lan", 11434)]
    assert not hasattr(proxy, "database_url")
    with pytest.raises(SettingsError, match="HG_PROXY_KEY is required"):
        ProxySettings.from_env({})


def test_extra_authorities_are_read_from_a_file_that_must_exist(tmp_path: Path) -> None:
    file = tmp_path / "corporate.pem"
    file.write_text("-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n")
    assert Settings.from_env({**REQUIRED, "HG_EXTRA_CA": str(file)}).extra_ca == file.read_text()
    assert ProxySettings.from_env({"HG_PROXY_KEY": "k", "HG_EXTRA_CA": str(file)}).extra_ca == file.read_text()
    assert Settings.from_env(REQUIRED).extra_ca == ""
    with pytest.raises(SettingsError, match="HG_EXTRA_CA"):
        Settings.from_env({**REQUIRED, "HG_EXTRA_CA": str(tmp_path / "absent.pem")})
    (tmp_path / "empty.pem").write_text("not a certificate")
    with pytest.raises(SettingsError, match="HG_EXTRA_CA"):
        Settings.from_env({**REQUIRED, "HG_EXTRA_CA": str(tmp_path / "empty.pem")})
