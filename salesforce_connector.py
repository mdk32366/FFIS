"""
salesforce_connector.py
=======================
Salesforce integration for FFIS. Supports TWO auth methods, auto-selected by
which credentials are configured:

  1. Username / Password / Security Token  (best for short-lived Developer Orgs)
  2. External App / Connected App, OAuth 2.0 client_credentials (permanent orgs)

Priority: if client_id + client_secret are present, use the External App flow;
otherwise fall back to username/password/token. Override with SF_AUTH_METHOD.

Config (secrets.json 'salesforce' block or env vars):
  Common:
    SF_DOMAIN          'login' (prod/dev) or 'test' (sandbox). Default 'login'.
    SF_AUTH_METHOD     optional: 'password' | 'client_credentials' | 'auto'
  Username-password-token:
    SF_USERNAME, SF_PASSWORD, SF_SECURITY_TOKEN
  External App (client_credentials):
    SF_CLIENT_ID, SF_CLIENT_SECRET, SF_LOGIN_URL (default https://login.salesforce.com)

Install: pip install simple-salesforce
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

try:
    from simple_salesforce import Salesforce
    from simple_salesforce.exceptions import SalesforceAuthenticationFailed
    SF_AVAILABLE = True
except ImportError:
    SF_AVAILABLE = False

SECRETS_FILE = Path(__file__).parent / "secrets.json"


def get_salesforce_config() -> Dict[str, Any]:
    """Merge secrets.json 'salesforce' block with env vars (env wins if set)."""
    cfg: Dict[str, Any] = {}
    if SECRETS_FILE.exists():
        try:
            with open(SECRETS_FILE) as f:
                cfg = dict(json.load(f).get("salesforce", {}) or {})
        except (json.JSONDecodeError, IOError):
            cfg = {}

    def pick(key: str, env: str, default: str = "") -> str:
        return os.getenv(env, cfg.get(key, default)) or default

    return {
        "auth_method":    pick("auth_method", "SF_AUTH_METHOD", "auto"),
        "domain":         pick("domain", "SF_DOMAIN", "login"),
        # username/password/token
        "username":       pick("username", "SF_USERNAME"),
        "password":       pick("password", "SF_PASSWORD"),
        "security_token": pick("security_token", "SF_SECURITY_TOKEN"),
        # external app / connected app
        "client_id":      pick("client_id", "SF_CLIENT_ID"),
        "client_secret":  pick("client_secret", "SF_CLIENT_SECRET"),
        "login_url":      pick("login_url", "SF_LOGIN_URL", "https://login.salesforce.com"),
    }


def _resolve_method(cfg: Dict[str, Any]) -> Optional[str]:
    """Decide which auth flow to use based on config + available creds."""
    m = (cfg.get("auth_method") or "auto").lower()
    has_cc = bool(cfg.get("client_id") and cfg.get("client_secret"))
    has_pw = bool(cfg.get("username") and cfg.get("password"))
    if m == "client_credentials":
        return "client_credentials" if has_cc else None
    if m == "password":
        return "password" if has_pw else None
    # auto: prefer External App when present, else password
    if has_cc:
        return "client_credentials"
    if has_pw:
        return "password"
    return None


def _connect_password(cfg: Dict[str, Any]) -> "Salesforce":
    """Username + password + security token (Developer Orgs)."""
    return Salesforce(
        username=cfg["username"],
        password=cfg["password"],
        security_token=cfg.get("security_token", ""),
        domain=cfg.get("domain", "login"),
    )


def _connect_client_credentials(cfg: Dict[str, Any]) -> "Salesforce":
    """External App OAuth 2.0 client_credentials (permanent orgs)."""
    login_url = cfg.get("login_url", "https://login.salesforce.com").rstrip("/")
    token_url = f"{login_url}/services/oauth2/token"
    resp = requests.post(token_url, data={
        "grant_type": "client_credentials",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
    }, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Salesforce token request failed ({resp.status_code}): {resp.text[:300]}"
        )
    body = resp.json()
    return Salesforce(instance_url=body["instance_url"], session_id=body["access_token"])


def connect_salesforce() -> Optional["Salesforce"]:
    """
    Return an authenticated Salesforce session, or None if unconfigured.
    Chooses the flow via _resolve_method(). Raises RuntimeError on auth failure.
    """
    if not SF_AVAILABLE:
        raise ImportError("simple-salesforce not installed. Run: pip install simple-salesforce")
    cfg = get_salesforce_config()
    method = _resolve_method(cfg)
    if method is None:
        return None  # nothing configured — app keeps running without SF
    try:
        if method == "client_credentials":
            return _connect_client_credentials(cfg)
        return _connect_password(cfg)
    except SalesforceAuthenticationFailed as e:
        raise RuntimeError(f"Salesforce auth failed ({method}): {e}") from e
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Salesforce connection error ({method}): {e}") from e


def sf_query(soql: str, sf: Optional["Salesforce"] = None) -> List[Dict[str, Any]]:
    """Run SOQL, returning all records (handles pagination via query_all)."""
    if sf is None:
        sf = connect_salesforce()
    if sf is None:
        return []
    return sf.query_all(soql).get("records", [])


def sf_get_existing_keys(object_type: str, key_fields: List[str],
                         sf: Optional["Salesforce"] = None, limit: int = 50000) -> set:
    """
    Set of normalized composite keys ('a||b', lower/trimmed) for existing records.
    Matches the pipeline's _key_series format so it drops into duplicate_mask().
    """
    if sf is None:
        sf = connect_salesforce()
    if sf is None:
        return set()
    fields = ", ".join(key_fields)
    soql = f"SELECT {fields} FROM {object_type} LIMIT {limit}"
    out = set()
    for rec in sf_query(soql, sf):
        out.add("||".join(str(rec.get(k) or "").strip().lower() for k in key_fields))
    return out


def test_connection() -> Dict[str, Any]:
    """Health check for the UI / CLI. Never raises."""
    cfg = get_salesforce_config()
    method = _resolve_method(cfg)
    if method is None:
        return {"ok": False, "reason": "Salesforce not configured.",
                "hint": "Set SF_USERNAME/SF_PASSWORD/SF_SECURITY_TOKEN (dev org) "
                        "or SF_CLIENT_ID/SF_CLIENT_SECRET (External App)."}
    if not SF_AVAILABLE:
        return {"ok": False, "reason": "simple-salesforce not installed."}
    try:
        sf = connect_salesforce()
        org = sf_query("SELECT Id, Name FROM Organization LIMIT 1", sf)
        return {"ok": True, "auth_method": method,
                "org": org[0]["Name"] if org else "(unknown)",
                "instance": getattr(sf, "sf_instance", "?")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "auth_method": method, "reason": str(e)}


if __name__ == "__main__":
    import pprint
    pprint.pprint(test_connection())