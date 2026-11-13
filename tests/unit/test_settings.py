"""Spec §18.4: settings come from the environment; a missing or invalid one stops the program and is named."""

import base64
from pathlib import Path

import pytest

import harnessgateway
from harnessgateway.settings import Settings, SettingsError

KEY = base64.b64encode(bytes(range(32))).decode()
REQUIRED = {"HG_DATABASE_URL": "postgresql://u:p@db/hg", "HG_SECRET_KEY": KEY, "HG_PROXY_KEY": "proxy-secret"}


def test_with_only_the_required_settings_the_rest_take_their_defaults() -> None:
    s = Settings.from_env(REQUIRED)
    assert s.database_url == "postgresql://u:p@db/hg"
    assert s.secret_key == bytes(range(32))
    assert s.proxy_key == "proxy-secret"
    assert s.data == Path("/data")
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
    })
    assert (s.data, s.driver, s.runtime, s.platform_image, s.sandbox_image) == (
        Path("/var/hg"), "kubernetes", "runsc", "p:1", "s:1")
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
])
def test_an_invalid_setting_is_named(name: str, value: str) -> None:
    with pytest.raises(SettingsError) as e:
        Settings.from_env({**REQUIRED, name: value})
    assert name in str(e.value)


def test_the_error_never_shows_a_secret_s_value() -> None:
    with pytest.raises(SettingsError) as e:
        Settings.from_env({**REQUIRED, "HG_SECRET_KEY": "c2VjcmV0LXZhbHVl"})
    assert "c2VjcmV0LXZhbHVl" not in str(e.value)
