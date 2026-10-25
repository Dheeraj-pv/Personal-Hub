import pytest

from hub.config import ConfigError, load_settings
from hub.main import create_app


def test_missing_token_refuses_to_start():
    with pytest.raises(ConfigError, match="HUB_TOKEN"):
        load_settings({})


def test_short_token_refuses_to_start_and_does_not_echo_it():
    with pytest.raises(ConfigError) as e:
        load_settings({"HUB_TOKEN": "short-secret"})
    assert "short-secret" not in str(e.value)


def test_defaults_and_overrides(tmp_path):
    s = load_settings({"HUB_TOKEN": "x" * 32, "HUB_DB": str(tmp_path / "d.db"), "HUB_PORT": "9000"})
    assert (s.host, s.port) == ("127.0.0.1", 9000)
    assert "x" * 32 not in repr(s)
    d = load_settings({"HUB_TOKEN": "x" * 32})
    assert str(d.db_path).endswith("PersonalHub/data/hub.db") and d.port == 8787


def test_create_app_makes_private_data_dir(settings):
    create_app(settings)
    assert (settings.db_path.parent.stat().st_mode & 0o777) == 0o700


@pytest.mark.parametrize("tok", ["replace-with-a-random-string-of-at-least-32-characters",
                                 "Replace-With-" + "z" * 30, "changeme"])
def test_placeholder_token_refused_and_not_echoed(tok):
    with pytest.raises(ConfigError) as e:
        load_settings({"HUB_TOKEN": tok})
    assert tok not in str(e.value)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_allowed(host):
    assert load_settings({"HUB_TOKEN": "x" * 32, "HUB_HOST": host}).host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::"])
def test_non_loopback_host_refused_without_override(host):
    with pytest.raises(ConfigError, match="HUB_HOST"):
        load_settings({"HUB_TOKEN": "x" * 32, "HUB_HOST": host})


def test_non_loopback_host_with_override_warns_without_secrets(caplog):
    caplog.set_level("WARNING", logger="hub")
    s = load_settings({"HUB_TOKEN": "t" * 32, "HUB_HOST": "0.0.0.0", "HUB_ALLOW_NON_LOOPBACK": "1"})
    assert s.host == "0.0.0.0"
    assert "non-loopback" in caplog.text and "t" * 32 not in caplog.text
