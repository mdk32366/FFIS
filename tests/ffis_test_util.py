"""Shared helpers for the hardening tests (localhost only, fake credentials only)."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_BLOB = "08c83b14381b9270320bddf9a928d7ee79592a58"
FAKE_SMTP_PASSWORD = "FAKE-smtp-pw-2b8d41aa-NOT-REAL"
FAKE_SMTP_USER = "ffis-fake-sender@example.invalid"
FAKE_ALERT_TO = "ffis-fake-alerts@example.invalid"

sys.path.insert(0, os.path.join(REPO, "tests"))
from fake_imap import FAKE_PASSWORD, FAKE_USER  # noqa: E402

# Env vars that must never leak into a test subprocess (real creds / egress).
_STRIP = ("IMAP_", "SMTP_", "ANTHROPIC", "SF_", "SNOWFLAKE", "API_ENDPOINT", "FFIS_",
          "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "SALESFORCE")


def base_module_dir() -> str:
    """Write the UNMODIFIED base blob 08c83b14 (from git) alone into a temp dir."""
    d = tempfile.mkdtemp(prefix="ffis-base-")
    data = subprocess.check_output(["git", "-C", REPO, "cat-file", "-p", BASE_BLOB])
    sha = subprocess.check_output(["git", "-C", REPO, "hash-object", "--stdin"], input=data).decode().strip()
    assert sha == BASE_BLOB, sha
    with open(os.path.join(d, "ffis_email_pipeline.py"), "wb") as f:
        f.write(data)
    return d


def clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_STRIP)}
    env.update({
        "IMAP_USER": FAKE_USER, "IMAP_PASSWORD": FAKE_PASSWORD,
        "IMAP_HOST": "127.0.0.1", "IMAP_USE_SSL": "false", "IMAP_FOLDER": "INBOX",
        "SMTP_HOST": "127.0.0.1", "SMTP_PORT": "1", "SMTP_FROM_EMAIL": FAKE_SMTP_USER,
        "SMTP_APP_PASSWORD": FAKE_SMTP_PASSWORD,
        "FFIS_DEDUP_BACKEND": "none", "FFIS_EXPORT_GOOD": "false", "FFIS_PIPELINE_REPLY": "true",
        "FFIS_IMAP_STARTTLS": "false", "PYTHONUNBUFFERED": "1",
    })
    env.update({k: str(v) for k, v in extra.items()})
    return env
