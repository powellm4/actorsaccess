# src/config.py
import os
import yaml


class ConfigError(Exception):
    pass


DEFAULTS = {
    "filters": {
        "region": "",
        "gender": "any",
        "age_range": [18, 99],
        "union_status": "any",
        "paying_only": False,
        "exclude_reality_tv": False,
    },
    "submission": {
        "headshot_index": 0,
        "include_media": False,
        "include_size_card": False,
        "default_note": "",
    },
    "schedule": {"interval_hours": 4},
    "browser": {"headless": True},
    "logging": {"level": "INFO", "file": "logs/auto_apply.log"},
}


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        raise ConfigError(f"Config file not found: {path}")

    with open(path, "r") as f:
        cfg = yaml.safe_load(f) or {}

    # Validate required fields
    if "credentials" not in cfg:
        raise ConfigError("Missing required section: credentials")
    if "username" not in cfg["credentials"]:
        raise ConfigError("Missing required field: credentials.username")
    if "password" not in cfg["credentials"]:
        raise ConfigError("Missing required field: credentials.password")

    # Apply defaults for optional sections
    for section, defaults in DEFAULTS.items():
        if section not in cfg:
            cfg[section] = {}
        for key, value in defaults.items():
            if key not in cfg[section]:
                cfg[section][key] = value

    return cfg
