# src/backstage/config.py
"""Configuration loading for Backstage auto-apply."""

import os
import yaml

DEFAULTS = {
    "submission": {"default_note": ""},
    "browser": {"headless": False},
    "logging": {"level": "INFO", "file": "logs/backstage_auto_apply.log"},
}


class BackstageConfigError(Exception):
    pass


def load_backstage_config(path: str = "backstage_config.yaml") -> dict:
    """Load Backstage config from YAML, with env var overrides for credentials."""
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        cfg = {}

    # Apply defaults
    for section, defaults in DEFAULTS.items():
        if section not in cfg:
            cfg[section] = {}
        for key, val in defaults.items():
            cfg[section].setdefault(key, val)

    # Credentials from env vars take priority
    if "credentials" not in cfg:
        cfg["credentials"] = {}
    cfg["credentials"]["email"] = os.environ.get(
        "BACKSTAGE_EMAIL", cfg["credentials"].get("email", "")
    )
    cfg["credentials"]["password"] = os.environ.get(
        "BACKSTAGE_PASSWORD", cfg["credentials"].get("password", "")
    )

    if not cfg["credentials"]["email"] or not cfg["credentials"]["password"]:
        raise BackstageConfigError(
            "Backstage credentials not set. Use BACKSTAGE_EMAIL/BACKSTAGE_PASSWORD env vars or backstage_config.yaml"
        )

    return cfg
