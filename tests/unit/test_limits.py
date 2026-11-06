"""An organization's limits (SPEC §7.4) and the settings that carry their defaults."""
import pytest

from harnessgateway.config import ConfigError, Settings
from harnessgateway.domain.limits import OrgLimits

SECRET = "dGVzdC1zZWNyZXQta2V5LW9mLXRoaXJ0eS10d28tYnl0ZXM="


def test_limits_parse_over_defaults():
    base = OrgLimits.from_env({"vcpus": "1", "standby_s": "30"})
    assert (base.vcpus, base.standby_s, base.memory_mb) == (1.0, 30, 4096)
    got = OrgLimits.parse({"memory_mb": 1024}, base)
    assert (got.vcpus, got.memory_mb) == (1.0, 1024)
    with pytest.raises(ValueError, match="limits.vcpus"):
        OrgLimits.parse({"vcpus": 0})
    with pytest.raises(ValueError, match="unknown HG_DEFAULT_"):
        OrgLimits.from_env({"node_pool": "x"})


def test_settings_name_every_problem_at_once():
    with pytest.raises(ConfigError) as e:
        Settings.load(("api",), env={"HG_LOG_LEVEL": "loud", "HG_SANDBOX_RUNTIME": "podman"})
    text = str(e.value)
    for needle in ("HG_DATABASE_URL", "HG_BLOB_URL", "HG_SECRET_KEY", "HG_DOOR_KEY", "HG_LOG_LEVEL", "HG_SANDBOX_RUNTIME"):
        assert needle in text


def test_settings_load_and_hide_secrets():
    s = Settings.load(("api",), env={"HG_DATABASE_URL": "postgresql://x", "HG_BLOB_URL": "file:///tmp/x",
                                     "HG_SECRET_KEY": SECRET, "HG_DOOR_KEY": "hgd_x", "HG_API_URL": "http://api:8000/",
                                     "HG_DEFAULT_STANDBY_S": "5"})
    assert s.api_url == "http://api:8000" and s.platform_hosts == ["api:8000"] and s.defaults.standby_s == 5
    assert "secret_key" not in s.describe() and "door_key" not in s.describe()
    assert len(s.pepper) == 32
