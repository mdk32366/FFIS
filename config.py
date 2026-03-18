"""
Configuration loader for Flat File Scrubber
Loads secrets from secrets.json with safe defaults
"""

import json
import os
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "secrets.json"

DEFAULT_CONFIG = {
    "imap": {
        "host": "imap.gmail.com",
        "port": 993,
        "use_ssl": True,
        "folder": "INBOX",
    },
    "api": {
        "endpoint_url": "",
        "method": "POST",
        "headers": {
            "Authorization": "Bearer YOUR_TOKEN",
            "Content-Type": "application/json",
        },
        "batch_size": 200,
    },
    "smtp": {
        "host": "smtp.gmail.com",
        "port": 587,
        "from_email": "",
        "app_password": "",
    },
}


def load_config():
    """
    Load configuration from secrets.json.
    Falls back to defaults if file doesn't exist.
    """
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                user_config = json.load(f)
            # Merge user config with defaults (user config overrides)
            config = DEFAULT_CONFIG.copy()
            for key in config:
                if key in user_config:
                    config[key].update(user_config[key])
            return config
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not read secrets.json ({e}). Using defaults.")
            return DEFAULT_CONFIG
    else:
        return DEFAULT_CONFIG


def get_imap_config():
    """Get IMAP configuration"""
    config = load_config()
    return config.get("imap", DEFAULT_CONFIG["imap"])


def get_api_config():
    """Get API configuration"""
    config = load_config()
    return config.get("api", DEFAULT_CONFIG["api"])


def get_smtp_config():
    """Get SMTP configuration"""
    config = load_config()
    return config.get("smtp", DEFAULT_CONFIG["smtp"])
