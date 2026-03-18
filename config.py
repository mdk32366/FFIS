"""
Configuration loader for Flat File Scrubber
Loads settings from .env file, secrets.json, and environment variables
with sensible defaults as fallback.
"""

import json
import os
from pathlib import Path

# ──────────────────────────────────────────────
# FILE PATHS
# ──────────────────────────────────────────────
ENV_FILE = Path(__file__).parent / ".env"
SECRETS_FILE = Path(__file__).parent / "secrets.json"

# ──────────────────────────────────────────────
# LOAD .ENV FILE
# ──────────────────────────────────────────────
def load_env_file():
    """Load environment variables from .env file if it exists."""
    if ENV_FILE.exists():
        try:
            with open(ENV_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    # Skip comments and empty lines
                    if line and not line.startswith("#"):
                        if "=" in line:
                            key, value = line.split("=", 1)
                            key = key.strip()
                            value = value.strip()
                            # Only set if not already in environment
                            if key not in os.environ:
                                os.environ[key] = value
        except IOError as e:
            print(f"Warning: Could not read .env file ({e})")

# Load .env file on import
load_env_file()

# ──────────────────────────────────────────────
# ENVIRONMENT VARIABLE HELPERS
# ──────────────────────────────────────────────
def getenv_str(key: str, default: str = "") -> str:
    """Get string environment variable."""
    return os.getenv(key, default)

def getenv_int(key: str, default: int = 0) -> int:
    """Get integer environment variable."""
    val = os.getenv(key, str(default))
    try:
        return int(val)
    except (ValueError, TypeError):
        return default

def getenv_bool(key: str, default: bool = False) -> bool:
    """Get boolean environment variable."""
    val = os.getenv(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")

def getenv_list(key: str, default_list: list = None) -> list:
    """Get comma-separated list as environment variable."""
    if default_list is None:
        default_list = []
    val = os.getenv(key, ",".join(default_list) if default_list else "")
    if not val:
        return default_list
    return [item.strip() for item in val.split(",")]

# ──────────────────────────────────────────────
# STREAMLIT UI CONFIGURATION
# ──────────────────────────────────────────────
def get_streamlit_config():
    """Get Streamlit page configuration."""
    return {
        "page_title": getenv_str("STREAMLIT_PAGE_TITLE", "Flat File Scrubber"),
        "page_icon": getenv_str("STREAMLIT_PAGE_ICON", "🧹"),
        "layout": getenv_str("STREAMLIT_LAYOUT", "wide"),
        "initial_sidebar_state": getenv_str("STREAMLIT_SIDEBAR_STATE", "expanded"),
    }

# ──────────────────────────────────────────────
# SALESFORCE CONFIGURATION
# ──────────────────────────────────────────────
def get_salesforce_objects():
    """Get list of supported Salesforce objects."""
    default_objects = [
        "Account",
        "Contact",
        "Lead",
        "Opportunity",
        "Account to Account Relationship",
        "Account to Contact Relationship",
        "User",
        "Snowflake Table - DEFAULT",
    ]
    return getenv_list("SALESFORCE_OBJECTS", default_objects)

def get_required_fields():
    """Get required fields for each Salesforce object."""
    return {
        "Account":      ["Name"],
        "Contact":      ["LastName", "AccountId"],
        "Lead":         ["LastName", "Company"],
        "Opportunity":  ["Name", "StageName", "CloseDate", "AccountId"],
        "Account to Account Relationship": ["ParentId", "ChildId"],
        "Account to Contact Relationship": ["AccountId", "ContactId"],
        "User":         ["LastName", "Username", "Email", "ProfileId", "TimeZoneSidKey", "LocaleSidKey", "EmailEncodingKey", "LanguageLocaleKey"],
        "Snowflake Table - DEFAULT": [],
    }

def get_special_chars_pattern():
    """Get regex pattern for special characters to sanitize."""
    default_pattern = r'[\*\n\^\$\#\@\!\%\&\(\)\[\]\{\}\<\>\?\/\\|`~"\';:]'
    return getenv_str("SPECIAL_CHARS_PATTERN", default_pattern)

# ──────────────────────────────────────────────
# APPLICATION BEHAVIOR
# ──────────────────────────────────────────────
def get_undo_history_limit():
    """Get maximum number of undo history steps."""
    return getenv_int("UNDO_HISTORY_LIMIT", 20)

def get_api_request_timeout():
    """Get API request timeout in seconds."""
    return getenv_int("API_REQUEST_TIMEOUT", 30)

# ──────────────────────────────────────────────
# LOAD SECRETS FROM JSON
# ──────────────────────────────────────────────
def _load_secrets_json():
    """
    Load secrets from secrets.json file.
    Falls back to empty dict if file doesn't exist.
    """
    if SECRETS_FILE.exists():
        try:
            with open(SECRETS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not read secrets.json ({e})")
            return {}
    return {}

# ──────────────────────────────────────────────
# IMAP CONFIGURATION (Email Intake)
# ──────────────────────────────────────────────
def get_imap_config():
    """Get IMAP configuration from env + secrets + defaults."""
    secrets = _load_secrets_json()
    imap_secrets = secrets.get("imap", {})
    
    return {
        "host": getenv_str("IMAP_HOST", imap_secrets.get("host", "imap.gmail.com")),
        "port": getenv_int("IMAP_PORT", imap_secrets.get("port", 993)),
        "use_ssl": getenv_bool("IMAP_USE_SSL", imap_secrets.get("use_ssl", True)),
        "folder": getenv_str("IMAP_FOLDER", imap_secrets.get("folder", "INBOX")),
    }

# ──────────────────────────────────────────────
# API CONFIGURATION (Data Export)
# ──────────────────────────────────────────────
def get_api_config():
    """Get API configuration from env + secrets + defaults."""
    secrets = _load_secrets_json()
    api_secrets = secrets.get("api", {})
    
    default_headers = {
        "Authorization": "Bearer YOUR_TOKEN",
        "Content-Type": "application/json",
    }
    
    return {
        "endpoint_url": getenv_str("API_ENDPOINT_URL", api_secrets.get("endpoint_url", "")),
        "method": getenv_str("API_METHOD", api_secrets.get("method", "POST")),
        "headers": api_secrets.get("headers", default_headers),
        "batch_size": getenv_int("API_BATCH_SIZE", api_secrets.get("batch_size", 200)),
    }

# ──────────────────────────────────────────────
# SMTP CONFIGURATION (Email Export)
# ──────────────────────────────────────────────
def get_smtp_config():
    """Get SMTP configuration from env + secrets + defaults."""
    secrets = _load_secrets_json()
    smtp_secrets = secrets.get("smtp", {})
    
    return {
        "host": getenv_str("SMTP_HOST", smtp_secrets.get("host", "smtp.gmail.com")),
        "port": getenv_int("SMTP_PORT", smtp_secrets.get("port", 587)),
        "from_email": getenv_str("SMTP_FROM_EMAIL", smtp_secrets.get("from_email", "")),
        "app_password": getenv_str("SMTP_APP_PASSWORD", smtp_secrets.get("app_password", "")),
    }
