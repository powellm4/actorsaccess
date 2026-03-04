# tests/test_config.py
import os
import tempfile
import pytest
import yaml
from src.config import load_config, ConfigError


def _write_config(tmp_path, data):
    """Helper: write a dict as YAML to a temp file and return the path."""
    path = os.path.join(tmp_path, "config.yaml")
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


VALID_CONFIG = {
    "credentials": {"username": "testuser", "password": "testpass"},
    "filters": {
        "region": "Los Angeles",
        "gender": "male",
        "age_range": [25, 35],
        "union_status": "any",
        "paying_only": True,
        "exclude_reality_tv": True,
    },
    "submission": {
        "headshot_index": 0,
        "include_media": True,
        "include_size_card": True,
        "default_note": "",
    },
    "schedule": {"interval_hours": 4},
    "browser": {"headless": True},
    "logging": {"level": "INFO", "file": "logs/auto_apply.log"},
}


def test_load_valid_config(tmp_path):
    path = _write_config(str(tmp_path), VALID_CONFIG)
    cfg = load_config(path)
    assert cfg["credentials"]["username"] == "testuser"
    assert cfg["filters"]["region"] == "Los Angeles"
    assert cfg["filters"]["age_range"] == [25, 35]
    assert cfg["schedule"]["interval_hours"] == 4


def test_load_config_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nonexistent/config.yaml")


def test_load_config_missing_credentials(tmp_path):
    bad = {**VALID_CONFIG}
    del bad["credentials"]
    path = _write_config(str(tmp_path), bad)
    with pytest.raises(ConfigError, match="credentials"):
        load_config(path)


def test_load_config_missing_username(tmp_path):
    bad = {**VALID_CONFIG, "credentials": {"password": "pass"}}
    path = _write_config(str(tmp_path), bad)
    with pytest.raises(ConfigError, match="username"):
        load_config(path)


def test_load_config_defaults(tmp_path):
    """Minimal config should fill in defaults for optional fields."""
    minimal = {
        "credentials": {"username": "user", "password": "pass"},
    }
    path = _write_config(str(tmp_path), minimal)
    cfg = load_config(path)
    assert cfg["browser"]["headless"] is True
    assert cfg["schedule"]["interval_hours"] == 4
    assert cfg["filters"]["union_status"] == "any"
    assert cfg["submission"]["headshot_index"] == 0
