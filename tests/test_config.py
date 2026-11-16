import json

import pytest

from airpy.config import DEFAULTS, Config


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIRPY_HOME", str(tmp_path))
    return tmp_path


def test_defaults_and_generated_values(home):
    config = Config()
    for key in ("latency_ms", "auto_connect", "pair_mode", "share_via"):
        assert config.get(key) == DEFAULTS[key]
    assert len(config.get("share_token")) >= 8
    assert config.get("receive_dir").endswith("AirPy")
    assert (home / "config.json").exists()


def test_token_is_stable_across_restarts(home):
    assert Config().get("share_token") == Config().get("share_token")


def test_set_persists(home):
    Config().set("latency_ms", 321)
    assert Config().get("latency_ms") == 321


def test_unknown_key_is_rejected(home):
    with pytest.raises(KeyError):
        Config().set("no_such_key", 1)


def test_values_of_wrong_type_are_ignored(home):
    (home / "config.json").write_text(json.dumps({"latency_ms": "fast", "auto_connect": 1}))
    config = Config()
    assert config.get("latency_ms") == DEFAULTS["latency_ms"]
    assert config.get("auto_connect") == DEFAULTS["auto_connect"]


def test_corrupt_file_falls_back_to_defaults(home):
    (home / "config.json").write_text("{not json")
    assert Config().get("latency_ms") == DEFAULTS["latency_ms"]


@pytest.mark.parametrize("old, expected", [("standard", 1500), ("low", 750), ("ultra", 350)])
def test_old_latency_profiles_are_migrated(home, old, expected):
    (home / "config.json").write_text(json.dumps({"latency": old}))
    assert Config().get("latency_ms") == expected


def test_get_returns_copies(home):
    config = Config()
    config.get("selected").append("x")
    config.get("credentials")["a"] = "b"
    assert config.get("selected") == []
    assert config.get("credentials") == {}
