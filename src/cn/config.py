# src/cn/config.py
import os
import yaml


class CnConfigError(Exception):
    pass


DEFAULTS = {
    "submission": {"default_note": ""},
    "browser": {"headless": False},
    "logging": {"level": "INFO", "file": "logs/cn_auto_apply.log"},
}


def load_cn_config(path: str) -> dict:
    if not os.path.exists(path):
        raise CnConfigError(f"Config file not found: {path}")

    with open(path, "r") as f:
        cfg = yaml.safe_load(f) or {}

    if "credentials" not in cfg:
        cfg["credentials"] = {}
    if os.environ.get("CN_EMAIL"):
        cfg["credentials"]["email"] = os.environ["CN_EMAIL"]
    if os.environ.get("CN_PASSWORD"):
        cfg["credentials"]["password"] = os.environ["CN_PASSWORD"]

    if not cfg["credentials"].get("email"):
        raise CnConfigError("Missing credentials.email (set in cn_config.yaml or CN_EMAIL env var)")
    if not cfg["credentials"].get("password"):
        raise CnConfigError("Missing credentials.password (set in cn_config.yaml or CN_PASSWORD env var)")

    for section, defaults in DEFAULTS.items():
        if section not in cfg:
            cfg[section] = {}
        if isinstance(defaults, dict):
            for key, value in defaults.items():
                if key not in cfg[section]:
                    cfg[section][key] = value
        else:
            cfg[section] = defaults

    if "max_pages" not in cfg:
        cfg["max_pages"] = 5

    return cfg
