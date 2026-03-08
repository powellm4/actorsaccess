# tests/cn/test_config.py
import os
import pytest
from src.cn.config import load_cn_config, CnConfigError


def test_load_config_from_file(tmp_path):
    cfg_file = tmp_path / "cn_config.yaml"
    cfg_file.write_text("""
credentials:
  email: "test@example.com"
  password: "pass123"
max_pages: 3
""")
    cfg = load_cn_config(str(cfg_file))
    assert cfg["credentials"]["email"] == "test@example.com"
    assert cfg["credentials"]["password"] == "pass123"
    assert cfg["max_pages"] == 3
    assert cfg["browser"]["headless"] is False


def test_env_vars_override_config(tmp_path, monkeypatch):
    cfg_file = tmp_path / "cn_config.yaml"
    cfg_file.write_text("""
credentials:
  email: "file@example.com"
  password: "filepass"
""")
    monkeypatch.setenv("CN_EMAIL", "env@example.com")
    monkeypatch.setenv("CN_PASSWORD", "envpass")
    cfg = load_cn_config(str(cfg_file))
    assert cfg["credentials"]["email"] == "env@example.com"
    assert cfg["credentials"]["password"] == "envpass"


def test_missing_credentials_raises(tmp_path):
    cfg_file = tmp_path / "cn_config.yaml"
    cfg_file.write_text("max_pages: 3\n")
    with pytest.raises(CnConfigError):
        load_cn_config(str(cfg_file))
