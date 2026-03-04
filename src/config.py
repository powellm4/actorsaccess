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

    # Credentials: env vars take priority over config file
    if "credentials" not in cfg:
        cfg["credentials"] = {}
    if os.environ.get("AA_USERNAME"):
        cfg["credentials"]["username"] = os.environ["AA_USERNAME"]
    if os.environ.get("AA_PASSWORD"):
        cfg["credentials"]["password"] = os.environ["AA_PASSWORD"]

    if not cfg["credentials"].get("username"):
        raise ConfigError("Missing credentials.username (set in config.yaml or AA_USERNAME env var)")
    if not cfg["credentials"].get("password"):
        raise ConfigError("Missing credentials.password (set in config.yaml or AA_PASSWORD env var)")

    # Apply defaults for optional sections
    for section, defaults in DEFAULTS.items():
        if section not in cfg:
            cfg[section] = {}
        for key, value in defaults.items():
            if key not in cfg[section]:
                cfg[section][key] = value

    return cfg
