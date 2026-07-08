"""
ffis_email_pipeline.py
======================
Automated email-in / email-out ingestion pipeline for FFIS (Flat File
Ingestion Scrubber).

Flow
----
1.  Poll an IMAP mailbox for unread messages that carry a CSV/TXT attachment.
2.  For each attachment:
      a. Load it into a DataFrame.
      b. Classify the Salesforce object type (Account, Contact, Lead,
         Opportunity, ...) using the Anthropic API, with a deterministic
         header-heuristic fallback when no API key is configured.
      c. Infer the "shape" of the file (columns, dtypes, null counts,
         required-field coverage, sample rows).
      d. Route every record into one of three layers:
           - good       : required fields present + acceptable values/types
           - duplicate  : matches an existing record (reference CSV, Snowflake,
                          or a live Salesforce query) or repeats within file
           - bad        : everything else (missing required / bad values)
      e. Optionally auto-export the good layer onward (Snowflake / Salesforce /
         generic REST API).
3.  Reply to the original sender with the three layers as CSV attachments
    plus a human-readable shape + summary report (including export status).

This module is intentionally free of Streamlit so it can run head-less as a
cron job, a Fly.io worker, or a one-shot CLI invocation. It reuses the
existing project config (`config.py`) and Snowflake helper
(`snowflake_connector.py`).

Salesforce auth
---------------
Two authentication modes are supported by `SalesforceConnector`, selected via
`SF_AUTH_MODE` (or auto-detected from whichever credentials are present):

  * "connected_app" — OAuth 2.0 using a Connected App's consumer key/secret.
      - grant "client_credentials" (server-to-server; requires a run-as user
        configured on the Connected App), or
      - grant "password" (Connected App consumer key/secret + a Salesforce
        username / password / security-token).
  * "password" — legacy OAuth 2.0 username-password flow. Still needs a
      Connected App's consumer key/secret plus username + password + token,
      but is kept as an explicit mode for clarity.

Either way FFIS never performs an interactive browser OAuth dance — it uses
the token endpoint directly so it can run head-less.

CLI
---
    python ffis_email_pipeline.py --once            # process inbox one time
    python ffis_email_pipeline.py --watch --interval 120
    python ffis_email_pipeline.py --file sample.csv  # offline test, no email
    python ffis_email_pipeline.py --once --export    # force good-layer export on
    python ffis_email_pipeline.py --sf-test          # test Salesforce auth only

See PIPELINE_SETUP.md for the environment variables / secrets.json keys it reads.
"""

from __future__ import annotations

import argparse
import email as email_lib
import imaplib
import io
import json
import logging
import os
import re
import smtplib
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from email import encoders
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# ──────────────────────────────────────────────────────────────────────────
# Project config (reused). Fall back gracefully if run outside the repo.
# ──────────────────────────────────────────────────────────────────────────
try:
    from config import (
        get_imap_config,
        get_smtp_config,
        get_required_fields,
        get_salesforce_objects,
        get_api_config,
        getenv_str,
        getenv_int,
        getenv_bool,
    )
except Exception:  # pragma: no cover - standalone fallback
    def getenv_str(k, d=""): return os.getenv(k, d)
    def getenv_int(k, d=0):
        try:
            return int(os.getenv(k, str(d)))
        except (TypeError, ValueError):
            return d
    def getenv_bool(k, d=False):
        return os.getenv(k, str(d)).lower() in ("true", "1", "yes", "on")

    def get_imap_config():
        return {"host": getenv_str("IMAP_HOST", "imap.gmail.com"),
                "port": getenv_int("IMAP_PORT", 993),
                "use_ssl": getenv_bool("IMAP_USE_SSL", True),
                "folder": getenv_str("IMAP_FOLDER", "INBOX")}

    def get_smtp_config():
        return {"host": getenv_str("SMTP_HOST", "smtp.gmail.com"),
                "port": getenv_int("SMTP_PORT", 587),
                "from_email": getenv_str("SMTP_FROM_EMAIL", ""),
                "app_password": getenv_str("SMTP_APP_PASSWORD", "")}

    def get_required_fields():
        return {
            "Account": ["Name"],
            "Contact": ["LastName", "AccountId"],
            "Lead": ["LastName", "Company"],
            "Opportunity": ["Name", "StageName", "CloseDate", "AccountId"],
            "User": ["LastName", "Username", "Email"],
        }

    def get_salesforce_objects():
        return ["Account", "Contact", "Lead", "Opportunity", "User"]

    def get_api_config():
        return {"endpoint_url": getenv_str("API_ENDPOINT_URL", ""),
                "method": getenv_str("API_METHOD", "POST"),
                "headers": {"Content-Type": "application/json"},
                "batch_size": getenv_int("API_BATCH_SIZE", 200)}

# Snowflake helper is optional — only used when the dedup/export backend is snowflake.
try:
    from snowflake_connector import query_snowflake, get_snowflake_config
except Exception:  # pragma: no cover
    query_snowflake = None
    get_snowflake_config = lambda: None  # noqa: E731

# Salesforce config helper is optional — if config.py exposes get_salesforce_config()
# we use it; otherwise SalesforceConnector reads env / secrets directly below.
try:
    from config import get_salesforce_config as _cfg_get_salesforce_config
except Exception:  # pragma: no cover
    _cfg_get_salesforce_config = None

log = logging.getLogger("ffis.pipeline")

def get_allowed_senders() -> list[str]:
    """
    Parse FFIS_ALLOWED_SENDERS into a normalized list of matchers.
    Empty/unset => [] => allow all senders (open intake, current behavior).
    Entries may be full addresses ('jane@sfcoe.org') or domains
    ('@sfcoe.org' or 'sfcoe.org'). Case-insensitive.
    """
    raw = os.environ.get("FFIS_ALLOWED_SENDERS", "").strip()
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        p = part.strip().lower()
        if not p:
            continue
        # normalize bare domains to '@domain' so matching is unambiguous
        if "@" not in p:
            p = "@" + p
        out.append(p)
    return out


def sender_allowed(sender: str, allow: list[str]) -> bool:
    """True if sender matches the allowlist, or the allowlist is empty."""
    if not allow:
        return True  # open intake
    s = (sender or "").strip().lower()
    if not s:
        return False
    for m in allow:
        if m.startswith("@"):
            if s.endswith(m):        # domain match
                return True
        elif s == m:                 # exact address match
            return True
    return False

# Anthropic model — kept in sync with ffis_chat.py
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


# ══════════════════════════════════════════════════════════════════════════
# 0. SALESFORCE CONNECTOR  (Connected App OAuth  +  username-password-token)
# ══════════════════════════════════════════════════════════════════════════
def get_salesforce_config() -> Dict[str, Any]:
    """
    Resolve Salesforce settings from config.py (if it exposes
    get_salesforce_config), otherwise from environment variables / secrets.

    Recognized keys (all optional until you use the connector):

      SF_AUTH_MODE          "connected_app" | "password" | "auto"  (default auto)
      SF_GRANT_TYPE         "client_credentials" | "password"      (connected_app)
      SF_LOGIN_URL          https://login.salesforce.com  (or test.salesforce.com
                            for sandboxes, or your My Domain URL)
      SF_CLIENT_ID          Connected App consumer key
      SF_CLIENT_SECRET      Connected App consumer secret
      SF_USERNAME           integration user's username
      SF_PASSWORD           integration user's password
      SF_SECURITY_TOKEN     integration user's security token (appended to pw)
      SF_API_VERSION        e.g. "v58.0"  (default v58.0)
      SF_DOMAIN             optional; convenience -> builds SF_LOGIN_URL
    """
    if _cfg_get_salesforce_config is not None:
        try:
            cfg = _cfg_get_salesforce_config() or {}
            if cfg:
                return cfg
        except Exception as e:  # noqa: BLE001
            log.warning("config.get_salesforce_config() failed, using env: %s", e)

    login_url = getenv_str("SF_LOGIN_URL", "")
    if not login_url:
        domain = getenv_str("SF_DOMAIN", "").strip()
        if domain:
            if not domain.startswith("http"):
                domain = "https://" + domain
            login_url = domain
        else:
            login_url = "https://login.salesforce.com"

    return {
        "auth_mode": getenv_str("SF_AUTH_MODE", "auto"),
        "grant_type": getenv_str("SF_GRANT_TYPE", "password"),
        "login_url": login_url.rstrip("/"),
        "client_id": getenv_str("SF_CLIENT_ID", ""),
        "client_secret": getenv_str("SF_CLIENT_SECRET", ""),
        "username": getenv_str("SF_USERNAME", ""),
        "password": getenv_str("SF_PASSWORD", ""),
        "security_token": getenv_str("SF_SECURITY_TOKEN", ""),
        "api_version": getenv_str("SF_API_VERSION", "v58.0"),
    }


class SalesforceAuthError(RuntimeError):
    """Raised when a Salesforce token cannot be obtained."""


@dataclass
class SalesforceConnector:
    """
    Minimal, dependency-light Salesforce client for head-less use.

    Handles two OAuth 2.0 flows against the token endpoint:

      * Connected App, grant_type=client_credentials
          POST /services/oauth2/token
          client_id, client_secret            (run-as user set on the app)
      * Connected App / legacy, grant_type=password
          POST /services/oauth2/token
          client_id, client_secret, username,
          password + security_token

    On success it caches `access_token` and `instance_url` and exposes:
      - query(soql)          -> list[dict]           (REST /query)
      - insert(object, rows) -> list[dict] results   (Composite sObjects)
      - test_connection()    -> dict (identity summary)

    Requires the `requests` package (already used elsewhere for API export).
    """
    login_url: str = "https://login.salesforce.com"
    client_id: str = ""
    client_secret: str = ""
    username: str = ""
    password: str = ""
    security_token: str = ""
    auth_mode: str = "auto"          # "connected_app" | "password" | "auto"
    grant_type: str = "password"     # used when auth_mode resolves to connected_app
    api_version: str = "v58.0"

    access_token: Optional[str] = field(default=None, init=False)
    instance_url: Optional[str] = field(default=None, init=False)
    _authed: bool = field(default=False, init=False)

    # ---- construction ----------------------------------------------------
    @classmethod
    def from_config(cls, cfg: Optional[Dict[str, Any]] = None) -> "SalesforceConnector":
        cfg = cfg or get_salesforce_config()
        return cls(
            login_url=cfg.get("login_url", "https://login.salesforce.com").rstrip("/"),
            client_id=cfg.get("client_id", ""),
            client_secret=cfg.get("client_secret", ""),
            username=cfg.get("username", ""),
            password=cfg.get("password", ""),
            security_token=cfg.get("security_token", ""),
            auth_mode=cfg.get("auth_mode", "auto"),
            grant_type=cfg.get("grant_type", "password"),
            api_version=cfg.get("api_version", "v58.0"),
        )

    # ---- helpers ---------------------------------------------------------
    def _resolve_mode(self) -> str:
        """Decide which flow to run when auth_mode == 'auto'."""
        mode = (self.auth_mode or "auto").lower()
        if mode in ("connected_app", "password"):
            return mode
        # auto: if we have a username+password, use the password grant (most
        # common for FFIS integration users); otherwise try client_credentials.
        if self.username and self.password:
            return "password"
        if self.client_id and self.client_secret:
            return "connected_app"
        raise SalesforceAuthError(
            "No Salesforce credentials found. Set SF_CLIENT_ID/SF_CLIENT_SECRET "
            "and either SF_USERNAME/SF_PASSWORD/SF_SECURITY_TOKEN (password grant) "
            "or configure a run-as user for the client_credentials grant."
        )

    def _token_endpoint(self) -> str:
        return f"{self.login_url}/services/oauth2/token"

    def _build_payload(self, mode: str) -> Dict[str, str]:
        """
        Build the form-encoded token request body.

        'password'         -> OAuth username-password flow. Salesforce wants the
                              security token appended directly to the password.
        'connected_app'    -> either client_credentials (no user) or password,
                              controlled by self.grant_type.
        """
        if mode == "password":
            grant = "password"
        else:  # connected_app
            grant = (self.grant_type or "password").lower()

        if grant == "client_credentials":
            if not (self.client_id and self.client_secret):
                raise SalesforceAuthError(
                    "client_credentials grant needs SF_CLIENT_ID and SF_CLIENT_SECRET."
                )
            return {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }

        # password grant (used by both 'password' mode and connected_app+password)
        missing = [n for n, v in (
            ("SF_CLIENT_ID", self.client_id),
            ("SF_CLIENT_SECRET", self.client_secret),
            ("SF_USERNAME", self.username),
            ("SF_PASSWORD", self.password),
        ) if not v]
        if missing:
            raise SalesforceAuthError(
                "password grant is missing: " + ", ".join(missing) +
                " (SF_SECURITY_TOKEN may also be required unless the app's IP is "
                "allow-listed)."
            )
        return {
            "grant_type": "password",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "username": self.username,
            "password": f"{self.password}{self.security_token or ''}",
        }

    # ---- auth ------------------------------------------------------------
    def authenticate(self, force: bool = False) -> None:
        """Obtain and cache an access token + instance URL."""
        if self._authed and not force:
            return
        try:
            import requests
        except Exception as e:  # noqa: BLE001
            raise SalesforceAuthError(f"requests unavailable: {e}")

        mode = self._resolve_mode()
        payload = self._build_payload(mode)
        try:
            resp = requests.post(
                self._token_endpoint(), data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
        except Exception as e:  # noqa: BLE001
            raise SalesforceAuthError(f"token request failed: {e}")

        if resp.status_code != 200:
            # Salesforce returns a JSON {error, error_description} on failure.
            detail = resp.text[:300]
            try:
                j = resp.json()
                detail = f"{j.get('error')}: {j.get('error_description')}"
            except Exception:
                pass
            raise SalesforceAuthError(
                f"auth failed ({resp.status_code}) via {mode}/"
                f"{payload.get('grant_type')}: {detail}"
            )

        data = resp.json()
        self.access_token = data.get("access_token")
        self.instance_url = (data.get("instance_url") or "").rstrip("/")
        if not (self.access_token and self.instance_url):
            raise SalesforceAuthError(
                "auth response missing access_token/instance_url: "
                f"{list(data.keys())}"
            )
        self._authed = True
        log.info("Salesforce auth OK via %s/%s -> %s",
                 mode, payload.get("grant_type"), self.instance_url)

    def _headers(self) -> Dict[str, str]:
        self.authenticate()
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _base(self) -> str:
        self.authenticate()
        return f"{self.instance_url}/services/data/{self.api_version}"

    # ---- operations ------------------------------------------------------
    def test_connection(self) -> Dict[str, Any]:
        """Authenticate and hit /limits as a lightweight sanity check."""
        import requests
        r = requests.get(f"{self._base()}/limits", headers=self._headers(), timeout=30)
        ok = r.status_code == 200
        return {
            "success": ok,
            "instance_url": self.instance_url,
            "api_version": self.api_version,
            "status_code": r.status_code,
            "detail": "ok" if ok else r.text[:200],
        }

    def query(self, soql: str) -> List[Dict[str, Any]]:
        """Run a SOQL query, following nextRecordsUrl pagination."""
        import requests
        records: List[Dict[str, Any]] = []
        url = f"{self._base()}/query"
        params: Optional[Dict[str, str]] = {"q": soql}
        while True:
            r = requests.get(url, headers=self._headers(), params=params, timeout=60)
            if r.status_code != 200:
                raise RuntimeError(f"SOQL query failed ({r.status_code}): {r.text[:200]}")
            body = r.json()
            records.extend(body.get("records", []))
            nxt = body.get("nextRecordsUrl")
            if body.get("done", True) or not nxt:
                break
            url = f"{self.instance_url}{nxt}"
            params = None  # nextRecordsUrl is fully-qualified
        return records

    def insert(self, object_type: str, rows: List[Dict[str, Any]],
               all_or_none: bool = False, batch_size: int = 200) -> Dict[str, Any]:
        """
        Insert records via the Composite sObjects collections endpoint
        (up to 200 per call). Returns a summary with per-record results.
        """
        import requests
        if not rows:
            return {"success": True, "created": 0, "errors": [], "results": []}
        endpoint = f"{self._base()}/composite/sobjects"
        created, errors, results = 0, [], []
        for i in range(0, len(rows), min(batch_size, 200)):
            chunk = rows[i:i + min(batch_size, 200)]
            payload = {
                "allOrNone": all_or_none,
                "records": [{"attributes": {"type": object_type}, **row} for row in chunk],
            }
            r = requests.post(endpoint, headers=self._headers(),
                              data=json.dumps(payload), timeout=60)
            if r.status_code not in (200, 201):
                errors.append(f"batch @ {i}: {r.status_code} {r.text[:120]}")
                continue
            for res in r.json():
                results.append(res)
                if res.get("success"):
                    created += 1
                else:
                    errors.append(res.get("errors"))
        return {"success": len(errors) == 0, "created": created,
                "errors": errors, "results": results}


def salesforce_available() -> bool:
    """True if enough Salesforce config is present to attempt auth."""
    cfg = get_salesforce_config()
    has_app = bool(cfg.get("client_id") and cfg.get("client_secret"))
    has_pw = bool(cfg.get("username") and cfg.get("password"))
    mode = (cfg.get("auth_mode") or "auto").lower()
    if mode == "connected_app" and (cfg.get("grant_type") == "client_credentials"):
        return has_app
    return has_app and (has_pw or mode == "connected_app")


# ══════════════════════════════════════════════════════════════════════════
# 1. INBOUND EMAIL — fetch messages WITH sender info (so we can reply)
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class InboundMessage:
    """A single inbound email plus its CSV/TXT attachments."""
    msg_id: bytes
    sender: str                                  # bare email address of From
    subject: str
    message_id_header: str                       # for In-Reply-To threading
    attachments: List[Tuple[str, bytes]] = field(default_factory=list)


def _decode_hdr(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def fetch_inbound_messages(
    host: str,
    port: int,
    use_ssl: bool,
    username: str,
    password: str,
    folder: str = "INBOX",
    subject_filter: str = "",
    only_unseen: bool = True,
    mark_seen: bool = True,
) -> List[InboundMessage]:
    """
    Connect to an IMAP mailbox and return one InboundMessage per email that
    contains at least one CSV/TXT attachment.

    Unlike the UI's `fetch_csv_from_imap`, this captures the sender address,
    subject, and Message-ID so the pipeline can reply to the originator.
    """
    results: List[InboundMessage] = []
    M = None
    try:
        M = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
        if not use_ssl:
            M.starttls()
        M.login(username, password)
        M.select(folder)

        base = "UNSEEN" if only_unseen else "ALL"
        criterion = f'({base} SUBJECT "{subject_filter}")' if subject_filter.strip() else base

        typ, data = M.search(None, criterion)
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")

        for msg_id in data[0].split():
            # BODY.PEEK[] fetches the full message WITHOUT implicitly setting the
            # \Seen flag (plain RFC822/BODY[] would mark it read on fetch). This
            # keeps "unread" meaningful and lets mark_seen control seen status.
            typ2, raw = M.fetch(msg_id, "(BODY.PEEK[])")
            if typ2 != "OK" or not raw or not raw[0]:
                continue
            msg = email_lib.message_from_bytes(raw[0][1])

            sender = parseaddr(msg.get("From", ""))[1].strip().lower()
            subject = _decode_hdr(msg.get("Subject", ""))
            mid = msg.get("Message-ID", "")

            atts: List[Tuple[str, bytes]] = []
            for part in msg.walk():
                ct = part.get_content_type()
                fname = _decode_hdr(part.get_filename())
                if fname and (
                    fname.lower().endswith((".csv", ".txt"))
                    or ct in ("text/csv", "text/plain", "application/octet-stream",
                              "application/vnd.ms-excel")
                ):
                    payload = part.get_payload(decode=True)
                    if payload:
                        atts.append((fname, payload))

            if atts:
                results.append(InboundMessage(
                    msg_id=msg_id, sender=sender, subject=subject,
                    message_id_header=mid, attachments=atts,
                ))
                if mark_seen:
                    M.store(msg_id, "+FLAGS", "\\Seen")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"IMAP error: {e}")
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass
    return results


def read_csv_bytes(data: bytes, filename: str = "") -> pd.DataFrame:
    """Robustly parse CSV/TSV bytes into a DataFrame with encoding fallbacks."""
    sep = "\t" if filename.lower().endswith(".tsv") else None  # None => sniff
    last_err: Optional[Exception] = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return pd.read_csv(
                io.BytesIO(data), sep=sep, engine="python", dtype=str,
                encoding=enc, keep_default_na=True, skip_blank_lines=True,
            )
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise ValueError(f"Could not parse '{filename or 'attachment'}': {last_err}")


# ══════════════════════════════════════════════════════════════════════════
# 2. CLASSIFICATION + SHAPE
# ══════════════════════════════════════════════════════════════════════════
# Column "signatures" used for the offline heuristic classifier and to break
# ties. Lower-cased for matching.
_OBJECT_SIGNATURES: Dict[str, set] = {
    "Account": {"name", "accountnumber", "industry", "billingstreet",
                "billingcity", "billingstate", "billingcountry", "website",
                "phone", "annualrevenue", "numberofemployees", "type",
                "parentid", "ownership", "sic", "rating"},
    "Contact": {"firstname", "lastname", "email", "accountid", "title",
                "mailingstreet", "mailingcity", "phone", "mobilephone",
                "department", "salutation", "leadsource", "birthdate"},
    "Lead": {"firstname", "lastname", "company", "email", "status",
             "leadsource", "industry", "rating", "title", "phone",
             "numberofemployees", "annualrevenue", "isconverted"},
    "Opportunity": {"name", "stagename", "closedate", "amount", "accountid",
                    "probability", "type", "forecastcategory", "leadsource",
                    "nextstep", "iswon", "isclosed", "campaignid"},
    "User": {"username", "email", "profileid", "timezonesidkey",
             "localesidkey", "emailencodingkey", "languagelocalekey",
             "alias", "lastname", "firstname", "isactive"},
    "Account to Account Relationship": {"parentid", "childid", "relationshiptype"},
    "Account to Contact Relationship": {"accountid", "contactid", "roles", "isdirect"},
}


@dataclass
class Classification:
    object_type: str
    confidence: float
    method: str            # "llm" or "heuristic"
    reasoning: str = ""


def _canon(name: str) -> str:
    """Canonical form for fuzzy field-name matching: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", str(name).strip().lower())


# Common human-readable header variants -> the canonical Salesforce-style
# token they should be treated as equivalent to (both sides run through
# _canon() before lookup, so spacing/case/punctuation don't matter).
_FIELD_ALIASES: Dict[str, str] = {
    "emailaddress": "email",
    "emailaddr": "email",
    "phonenumber": "phone",
    "directphonenumber": "phone",
    "mobilephonenumber": "mobilephone",
    "cellphone": "mobilephone",
    "cellphonenumber": "mobilephone",
    "companyname": "company",
    "accountname": "name",
    "fullname": "name",
    "jobtitle": "title",
    "positiontitle": "title",
}


def _canon_with_alias(name: str) -> str:
    c = _canon(name)
    return _FIELD_ALIASES.get(c, c)


def resolve_column(df_columns, target_field: str) -> Optional[str]:
    """
    Find the actual column in df_columns that corresponds to a Salesforce
    API field name (e.g. 'LastName'), tolerating human-readable variants
    like 'Last Name' or 'Email Address'. Returns the original column
    label as it appears in the file, or None if no match is found.
    """
    target_canon = _canon_with_alias(target_field)
    for col in df_columns:
        if _canon_with_alias(col) == target_canon:
            return col
    return None


def _normalize_cols(df: pd.DataFrame) -> List[str]:
    return [_canon_with_alias(c) for c in df.columns]


def heuristic_classify(df: pd.DataFrame, allowed: List[str]) -> Classification:
    """Score the file's columns against known object signatures."""
    cols = set(_normalize_cols(df))
    required = {k.lower(): [_canon_with_alias(c) for c in v] for k, v in get_required_fields().items()}
    best_obj, best_score = None, -1.0
    for obj in allowed:
        sig = _OBJECT_SIGNATURES.get(obj, set())
        if not sig:
            continue
        overlap = len(cols & sig)
        coverage = overlap / max(len(sig), 1)
        # Bonus: all required fields for this object are present.
        req = set(required.get(obj.lower(), []))
        req_bonus = 0.25 if req and req.issubset(cols) else 0.0
        score = coverage + req_bonus + 0.02 * overlap
        if score > best_score:
            best_obj, best_score = obj, score
    if not best_obj:
        best_obj = allowed[0] if allowed else "Account"
    confidence = max(0.0, min(1.0, best_score))
    return Classification(
        object_type=best_obj, confidence=round(confidence, 2),
        method="heuristic",
        reasoning=f"Header overlap with {best_obj} signature columns.",
    )


def llm_classify(df: pd.DataFrame, filename: str, allowed: List[str]) -> Optional[Classification]:
    """Classify via Anthropic. Returns None if unavailable so caller can fall back."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        import anthropic
    except Exception:
        return None

    sample = df.head(8).to_csv(index=False)
    headers = ", ".join(str(c) for c in df.columns)
    prompt = (
        "You classify CSV files into Salesforce object types. "
        f"Allowed types (choose exactly one): {allowed}.\n\n"
        f"File name: {filename}\n"
        f"Column headers: {headers}\n\n"
        f"First rows:\n{sample}\n\n"
        "Respond with ONLY a compact JSON object: "
        '{"object_type": "<one allowed type>", "confidence": <0..1>, '
        '"reasoning": "<one sentence>"}.'
    )
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            blk.text for blk in resp.content if getattr(blk, "type", "") == "text"
        ).strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(0))
        obj = str(data.get("object_type", "")).strip()
        if obj not in allowed:
            # try case-insensitive match
            lut = {a.lower(): a for a in allowed}
            obj = lut.get(obj.lower(), "")
        if not obj:
            return None
        conf = float(data.get("confidence", 0.5))
        return Classification(
            object_type=obj, confidence=round(max(0.0, min(1.0, conf)), 2),
            method="llm", reasoning=str(data.get("reasoning", "")),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("LLM classification failed, falling back to heuristic: %s", e)
        return None


def classify_object_type(df: pd.DataFrame, filename: str = "",
                         allowed: Optional[List[str]] = None) -> Classification:
    """LLM classification with deterministic heuristic fallback."""
    if allowed is None:
        allowed = [o for o in get_salesforce_objects()
                   if o not in ("Snowflake Table - DEFAULT",)]
    return llm_classify(df, filename, allowed) or heuristic_classify(df, allowed)


def infer_shape(df: pd.DataFrame, object_type: str) -> Dict[str, Any]:
    """Describe the file's shape: dimensions, columns, dtypes, nulls, coverage."""
    required = get_required_fields().get(object_type, [])
    present_cols = list(df.columns)
    null_counts = {c: int(df[c].isna().sum()) for c in present_cols}
    field_map = {rf: resolve_column(present_cols, rf) for rf in required}
    missing_required = [rf for rf, actual in field_map.items() if actual is None]
    return {
        "rows": int(df.shape[0]),
        "columns": int(df.shape[1]),
        "column_names": present_cols,
        "dtypes": {c: str(df[c].dtype) for c in present_cols},
        "null_counts": null_counts,
        "required_fields": required,
        "required_present": [rf for rf, actual in field_map.items() if actual is not None],
        "required_missing_columns": missing_required,
        "required_field_map": {rf: actual for rf, actual in field_map.items() if actual},
    }


# ══════════════════════════════════════════════════════════════════════════
# 3. VALIDATION  (required fields + acceptable types/values)
# ══════════════════════════════════════════════════════════════════════════
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Light-weight expected-type rules keyed by exact field name (case-insensitive).
# Kept deliberately small per the chosen scope: required + types.
_DATE_FIELDS = {"closedate", "birthdate", "convertdate", "lastactivitydate"}
_EMAIL_FIELDS = {"email", "username"}
_NUMERIC_FIELDS = {"amount", "annualrevenue", "numberofemployees", "probability"}


def _is_blank(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == ""
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _validate_value(field_name: str, value: Any) -> Optional[str]:
    """Return an error string if the value is unacceptable, else None."""
    fn = field_name.strip().lower()
    if _is_blank(value):
        return None  # blank handled by required-field check, not here
    sval = str(value).strip()
    if fn in _EMAIL_FIELDS and not _EMAIL_RE.match(sval):
        return f"{field_name}: invalid email format"
    if fn in _DATE_FIELDS:
        parsed = pd.to_datetime(sval, errors="coerce")
        if pd.isna(parsed):
            return f"{field_name}: unparseable date"
    if fn in _NUMERIC_FIELDS:
        try:
            float(sval.replace(",", "").replace("$", ""))
        except ValueError:
            return f"{field_name}: not numeric"
    return None


def validate_records(df: pd.DataFrame, object_type: str) -> Tuple[pd.Series, List[List[str]]]:
    """
    Return (is_good mask, per-row list of failure reasons).

    A record is good when:
      * every required field for the object is present as a column AND non-blank
      * every value present passes its type/format rule
    """
    required = get_required_fields().get(object_type, [])
    field_map = {rf: resolve_column(df.columns, rf) for rf in required}
    missing_cols = [rf for rf, actual in field_map.items() if actual is None]
    present_required = [(rf, actual) for rf, actual in field_map.items() if actual is not None]

    reasons: List[List[str]] = []
    good = []
    for _, row in df.iterrows():
        row_reasons: List[str] = []
        for rf in missing_cols:
            row_reasons.append(f"{rf}: required column missing from file")
        for rf, actual_col in present_required:
            if _is_blank(row.get(actual_col)):
                row_reasons.append(f"{rf}: required value is blank")
        for c in df.columns:
            err = _validate_value(str(c), row.get(c))
            if err:
                row_reasons.append(err)
        reasons.append(row_reasons)
        good.append(len(row_reasons) == 0)
    return pd.Series(good, index=df.index), reasons


# ══════════════════════════════════════════════════════════════════════════
# 4. DEDUP  (pluggable: CSV reference, Snowflake, or live Salesforce)
# ══════════════════════════════════════════════════════════════════════════
# Preferred match keys per object. Resolved against columns actually present;
# falls back to required fields, then to the full row.
_DEDUP_KEYS: Dict[str, List[str]] = {
    "Account": ["Name"],
    "Contact": ["Email"],
    "Lead": ["Email"],
    "Opportunity": ["Name", "AccountId"],
    "User": ["Username"],
    "Account to Account Relationship": ["ParentId", "ChildId"],
    "Account to Contact Relationship": ["AccountId", "ContactId"],
}


def resolve_key_columns(df: pd.DataFrame, object_type: str) -> List[str]:
    """Pick the dedup key columns that actually exist in this file (fuzzy-matched)."""
    preferred = _DEDUP_KEYS.get(object_type, [])
    keys = []
    for pk in preferred:
        actual = resolve_column(df.columns, pk)
        if actual:
            keys.append(actual)
    if keys:
        return keys
    req = []
    for rf in get_required_fields().get(object_type, []):
        actual = resolve_column(df.columns, rf)
        if actual:
            req.append(actual)
    if req:
        return req
    return list(df.columns)  # last resort: whole-row dedup


def _key_series(df: pd.DataFrame, keys: List[str]) -> pd.Series:
    """Build a normalized composite key per row (case-insensitive, trimmed)."""
    parts = []
    for c in keys:
        parts.append(df[c].astype(str).str.strip().str.lower().fillna(""))
    if not parts:
        return pd.Series([""] * len(df), index=df.index)
    out = parts[0]
    for p in parts[1:]:
        out = out.str.cat(p, sep="||")
    return out


class DedupSource(ABC):
    """Strategy interface for flagging records that already exist."""

    name = "base"

    @abstractmethod
    def existing_keys(self, object_type: str, keys: List[str]) -> set:
        """Return a set of normalized composite keys that already exist."""
        raise NotImplementedError


class NullDedup(DedupSource):
    """No external reference — only within-file duplicates are caught."""
    name = "none"

    def existing_keys(self, object_type: str, keys: List[str]) -> set:
        return set()


class CsvReferenceDedup(DedupSource):
    """
    Match against existing-record exports stored as CSVs.

    `reference_dir` should contain one CSV per object, named with the object in
    the filename, e.g. `Account_existing.csv`, `Contact.csv`. The whole
    directory is scanned and any file whose name contains the object name
    (case-insensitive) is used.
    """
    name = "csv"

    def __init__(self, reference_dir: str):
        self.reference_dir = reference_dir

    def existing_keys(self, object_type: str, keys: List[str]) -> set:
        out: set = set()
        if not self.reference_dir or not os.path.isdir(self.reference_dir):
            log.warning("CSV dedup: reference dir '%s' not found.", self.reference_dir)
            return out
        token = object_type.lower().replace(" ", "")
        for fname in os.listdir(self.reference_dir):
            if not fname.lower().endswith((".csv", ".tsv", ".txt")):
                continue
            stem = fname.lower().replace(" ", "")
            if token not in stem:
                continue
            path = os.path.join(self.reference_dir, fname)
            try:
                ref = pd.read_csv(path, dtype=str)
            except Exception as e:  # noqa: BLE001
                log.warning("CSV dedup: could not read %s: %s", path, e)
                continue
            ref_keys = [k for k in keys if k in ref.columns]
            if not ref_keys:
                continue
            out |= set(_key_series(ref, ref_keys).tolist())
        return out


class SnowflakeDedup(DedupSource):
    """
    Match against existing records in Snowflake.

    Maps each object type to a fully-qualified table via `table_map` (or env
    `SNOWFLAKE_TABLE_<OBJECT>`). Queries only the key columns. Ready to use
    once Snowflake credentials are configured; until then the pipeline can keep
    running on the CSV backend.
    """
    name = "snowflake"

    def __init__(self, table_map: Optional[Dict[str, str]] = None):
        self.table_map = table_map or {}

    def _table_for(self, object_type: str) -> Optional[str]:
        if object_type in self.table_map:
            return self.table_map[object_type]
        env_key = "SNOWFLAKE_TABLE_" + re.sub(r"[^A-Z]", "", object_type.upper())
        return getenv_str(env_key, "") or None

    def existing_keys(self, object_type: str, keys: List[str]) -> set:
        if query_snowflake is None:
            log.warning("Snowflake dedup requested but connector unavailable.")
            return set()
        table = self._table_for(object_type)
        if not table:
            log.warning("Snowflake dedup: no table mapped for %s.", object_type)
            return set()
        col_list = ", ".join(f'"{k}"' for k in keys)
        sql = f"SELECT {col_list} FROM {table}"
        rows = query_snowflake(sql)
        if not rows:
            return set()
        out = set()
        for r in rows:
            parts = ["" if v is None else str(v).strip().lower() for v in r]
            out.add("||".join(parts))
        return out


class SalesforceDedup(DedupSource):
    """
    Match against live records in Salesforce via SOQL.

    Uses the SalesforceConnector to query only the key columns for the target
    object, so dedup reflects the current state of the org rather than a stale
    CSV export. Object names with spaces (e.g. relationships) won't map to a
    standard SObject, so those fall back to an empty set (handled upstream by
    CSV/Snowflake backends instead).
    """
    name = "salesforce"

    # Map FFIS object labels to Salesforce SObject API names where they differ.
    _SOBJECT_MAP: Dict[str, str] = {
        "Account to Account Relationship": "AccountRelationship",
        "Account to Contact Relationship": "AccountContactRelation",
    }

    def __init__(self, connector: Optional[SalesforceConnector] = None):
        self.connector = connector or SalesforceConnector.from_config()

    def _sobject_for(self, object_type: str) -> str:
        return self._SOBJECT_MAP.get(object_type, object_type)

    def existing_keys(self, object_type: str, keys: List[str]) -> set:
        if not keys:
            return set()
        sobject = self._sobject_for(object_type)
        # Only query fields that are plausibly real API names (skip whitespace).
        fields = [k for k in keys if k and " " not in k]
        if not fields:
            return set()
        soql = f"SELECT {', '.join(fields)} FROM {sobject}"
        try:
            records = self.connector.query(soql)
        except Exception as e:  # noqa: BLE001
            log.warning("Salesforce dedup query failed for %s: %s", sobject, e)
            return set()
        out = set()
        for rec in records:
            parts = []
            for k in keys:
                v = rec.get(k)
                parts.append("" if v is None else str(v).strip().lower())
            out.add("||".join(parts))
        return out


def make_dedup_source(backend: str = "auto", reference_dir: str = "") -> DedupSource:
    """
    Factory. 'auto' prefers Snowflake, then live Salesforce, then CSV, else none.
    Explicit backends: 'snowflake' | 'salesforce' | 'csv' | 'none'.
    """
    backend = (backend or "auto").lower()
    if backend == "snowflake":
        return SnowflakeDedup()
    if backend == "salesforce":
        return SalesforceDedup()
    if backend == "csv":
        return CsvReferenceDedup(reference_dir)
    if backend == "none":
        return NullDedup()
    # auto
    if get_snowflake_config and get_snowflake_config():
        return SnowflakeDedup()
    if salesforce_available():
        return SalesforceDedup()
    if reference_dir and os.path.isdir(reference_dir):
        return CsvReferenceDedup(reference_dir)
    return NullDedup()


def duplicate_mask(df: pd.DataFrame, object_type: str, source: DedupSource) -> pd.Series:
    """
    True for rows that are duplicates: either a repeat within the file
    (keeping the first occurrence) or a match against the dedup source.
    """
    keys = resolve_key_columns(df, object_type)
    keyser = _key_series(df, keys)
    within = keyser.duplicated(keep="first")
    existing = source.existing_keys(object_type, keys)
    against_ref = keyser.isin(existing) if existing else pd.Series(False, index=df.index)
    return within | against_ref


# ══════════════════════════════════════════════════════════════════════════
# 4b. EXPORT  (pluggable: load the GOOD layer onward — Snowflake / SF / REST)
# ══════════════════════════════════════════════════════════════════════════
class Exporter(ABC):
    """Strategy interface for loading the good layer into a destination."""

    name = "base"

    @abstractmethod
    def export(self, df: pd.DataFrame, object_type: str) -> Dict[str, Any]:
        """Load df. Return a status dict with at least {'success': bool}."""
        raise NotImplementedError


class NoneExporter(Exporter):
    """Disabled — the good layer is only emailed back, not loaded anywhere."""
    name = "none"

    def export(self, df: pd.DataFrame, object_type: str) -> Dict[str, Any]:
        return {"success": True, "skipped": True, "destination": "none", "rows": int(len(df))}


def _export_table_for(object_type: str) -> str:
    """Resolve the destination table for an object (env override -> prefix+name)."""
    env_key = "FFIS_EXPORT_TABLE_" + re.sub(r"[^A-Z]", "", object_type.upper())
    explicit = getenv_str(env_key, "")
    if explicit:
        return explicit
    prefix = getenv_str("FFIS_EXPORT_TABLE_PREFIX", "")
    return prefix + re.sub(r"[^a-z0-9]+", "_", object_type.lower()).strip("_")


class SnowflakeExporter(Exporter):
    """Load the good layer into a Snowflake table (append by default)."""
    name = "snowflake"

    def __init__(self, if_exists: str = "append"):
        self.if_exists = if_exists

    def export(self, df: pd.DataFrame, object_type: str) -> Dict[str, Any]:
        if df is None or df.empty:
            return {"success": True, "skipped": True, "reason": "empty good layer",
                    "destination": "snowflake", "rows": 0}
        try:
            from snowflake_connector import export_to_snowflake
        except Exception as e:  # noqa: BLE001
            return {"success": False, "destination": "snowflake",
                    "error": f"connector unavailable: {e}"}
        table = _export_table_for(object_type)
        try:
            ok = export_to_snowflake(df, table, if_exists=self.if_exists)
            return {"success": bool(ok), "destination": "snowflake",
                    "table": table, "rows": int(len(df)),
                    "if_exists": self.if_exists}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "destination": "snowflake",
                    "table": table, "error": str(e)}


class SalesforceExporter(Exporter):
    """
    Insert the good layer directly into Salesforce via the Composite sObjects
    endpoint. Authenticates through SalesforceConnector (Connected App OAuth or
    username-password-token). Batches at 200 (Composite max).
    """
    name = "salesforce"

    def __init__(self, connector: Optional[SalesforceConnector] = None,
                 all_or_none: bool = False, batch_size: int = 200):
        self.connector = connector or SalesforceConnector.from_config()
        self.all_or_none = all_or_none
        self.batch_size = batch_size

    def export(self, df: pd.DataFrame, object_type: str) -> Dict[str, Any]:
        if df is None or df.empty:
            return {"success": True, "skipped": True, "reason": "empty good layer",
                    "destination": "salesforce", "rows": 0}
        sobject = SalesforceDedup._SOBJECT_MAP.get(object_type, object_type)
        # Drop all-null columns and NaN cells so Salesforce doesn't reject blanks.
        rows: List[Dict[str, Any]] = []
        for rec in df.to_dict(orient="records"):
            rows.append({k: v for k, v in rec.items()
                         if v is not None and not (isinstance(v, float) and pd.isna(v))
                         and str(v).strip() != ""})
        try:
            result = self.connector.insert(
                sobject, rows, all_or_none=self.all_or_none,
                batch_size=self.batch_size,
            )
        except Exception as e:  # noqa: BLE001
            return {"success": False, "destination": "salesforce",
                    "sobject": sobject, "error": str(e)}
        return {"success": result.get("success", False), "destination": "salesforce",
                "sobject": sobject, "rows": int(len(rows)),
                "created": result.get("created", 0),
                "errors": result.get("errors", [])[:10]}


class ApiExporter(Exporter):
    """
    POST the good layer to a REST endpoint in batches (e.g. a Salesforce
    ingestion API). Reuses get_api_config() for endpoint/headers/batch size.
    """
    name = "api"

    def __init__(self, endpoint: str = "", headers: Optional[Dict[str, str]] = None,
                 batch_size: int = 0):
        cfg = get_api_config()
        self.endpoint = endpoint or cfg.get("endpoint_url", "")
        self.headers = headers or cfg.get("headers", {"Content-Type": "application/json"})
        self.batch_size = batch_size or int(cfg.get("batch_size", 200) or 200)

    def export(self, df: pd.DataFrame, object_type: str) -> Dict[str, Any]:
        if df is None or df.empty:
            return {"success": True, "skipped": True, "reason": "empty good layer",
                    "destination": "api", "rows": 0}
        if not self.endpoint:
            return {"success": False, "destination": "api",
                    "error": "no API endpoint configured (API_ENDPOINT_URL)"}
        try:
            import requests
        except Exception as e:  # noqa: BLE001
            return {"success": False, "destination": "api",
                    "error": f"requests unavailable: {e}"}
        total = len(df)
        sent, batches, errors = 0, 0, []
        for i in range(0, total, self.batch_size):
            payload = df.iloc[i:i + self.batch_size].to_dict(orient="records")
            try:
                resp = requests.post(self.endpoint, json=payload,
                                     headers=self.headers, timeout=30)
                if resp.status_code in (200, 201, 202):
                    batches += 1
                    sent += len(payload)
                else:
                    errors.append(f"batch {batches + 1}: {resp.status_code} {resp.text[:80]}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"batch {batches + 1}: {e}")
        return {"success": len(errors) == 0, "destination": "api",
                "endpoint": self.endpoint, "rows": total, "rows_sent": sent,
                "batches_sent": batches, "errors": errors}


def make_exporter(backend: str = "auto", if_exists: str = "append") -> Exporter:
    """
    Factory. 'auto' prefers Snowflake, then Salesforce, then generic API.
    Explicit: 'snowflake' | 'salesforce' | 'api' | 'none'.
    """
    backend = (backend or "auto").lower()
    if backend == "none":
        return NoneExporter()
    if backend == "snowflake":
        return SnowflakeExporter(if_exists=if_exists)
    if backend == "salesforce":
        return SalesforceExporter()
    if backend == "api":
        return ApiExporter()
    # auto
    if get_snowflake_config and get_snowflake_config():
        return SnowflakeExporter(if_exists=if_exists)
    if salesforce_available():
        return SalesforceExporter()
    if get_api_config().get("endpoint_url"):
        return ApiExporter()
    return NoneExporter()


# ══════════════════════════════════════════════════════════════════════════
# ROUTING — split into good / duplicate / bad
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class RoutingResult:
    object_type: str
    classification: Classification
    shape: Dict[str, Any]
    good: pd.DataFrame
    duplicate: pd.DataFrame
    bad: pd.DataFrame
    bad_reasons: List[List[str]]
    key_columns: List[str]
    dedup_backend: str
    filename: str = ""
    export_status: Optional[Dict[str, Any]] = None

    @property
    def counts(self) -> Dict[str, int]:
        return {
            "total": int(len(self.good) + len(self.duplicate) + len(self.bad)),
            "good": int(len(self.good)),
            "duplicate": int(len(self.duplicate)),
            "bad": int(len(self.bad)),
        }


def route_records(df: pd.DataFrame, filename: str = "",
                  dedup: Optional[DedupSource] = None,
                  classification: Optional[Classification] = None) -> RoutingResult:
    """
    Classify the file and split every record into good / duplicate / bad.

    Precedence: duplicates are removed first (so they're never also counted as
    good/bad), then the remaining records are validated into good vs bad.
    """
    if dedup is None:
        dedup = NullDedup()
    cls = classification or classify_object_type(df, filename)
    obj = cls.object_type
    shape = infer_shape(df, obj)
    keys = resolve_key_columns(df, obj)

    df = df.reset_index(drop=True)
    dup_mask = duplicate_mask(df, obj, dedup)
    duplicate_df = df[dup_mask].copy()

    remaining = df[~dup_mask].copy()
    good_mask, reasons_remaining = validate_records(remaining, obj)
    good_df = remaining[good_mask].copy()
    bad_df = remaining[~good_mask].copy()
    bad_reasons = [r for r, keep in zip(reasons_remaining, (~good_mask).tolist()) if keep]

    return RoutingResult(
        object_type=obj, classification=cls, shape=shape,
        good=good_df, duplicate=duplicate_df, bad=bad_df,
        bad_reasons=bad_reasons, key_columns=keys,
        dedup_backend=dedup.name, filename=filename,
    )


# ══════════════════════════════════════════════════════════════════════════
# 5. OUTBOUND EMAIL — reply to sender with the three layers + shape summary
# ══════════════════════════════════════════════════════════════════════════
def _csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def build_summary(result: RoutingResult) -> str:
    """Human-readable shape + routing summary for the reply body."""
    c = result.counts
    cls = result.classification
    s = result.shape
    lines = [
        "FFIS automated ingestion result",
        "=" * 40,
        f"File:            {result.filename or '(attachment)'}",
        f"Detected object: {cls.object_type}  "
        f"(confidence {cls.confidence:.0%}, via {cls.method})",
        f"  {cls.reasoning}".rstrip(),
        "",
        "File shape",
        "-" * 40,
        f"Rows x Columns:  {s['rows']} x {s['columns']}",
        f"Columns:         {', '.join(s['column_names'])}",
        f"Required fields: {', '.join(s['required_fields']) or '(none)'}",
    ]
    if s["required_missing_columns"]:
        lines.append(f"  ! Missing required columns: {', '.join(s['required_missing_columns'])}")
    lines += [
        "",
        "Routing",
        "-" * 40,
        f"Dedup match keys: {', '.join(result.key_columns)}  (backend: {result.dedup_backend})",
        f"Total records:    {c['total']}",
        f"  GOOD:           {c['good']}",
        f"  DUPLICATE:      {c['duplicate']}",
        f"  BAD:            {c['bad']}",
    ]
    if result.bad_reasons:
        lines += ["", "Sample of why records were rejected (bad layer):", "-" * 40]
        for r in result.bad_reasons[:10]:
            lines.append("  - " + "; ".join(r[:4]) if r else "  - (unspecified)")
    es = result.export_status
    if es:
        lines += ["", "Export (good layer)", "-" * 40]
        dest = es.get("destination", "?")
        if es.get("skipped"):
            lines.append(f"  Destination {dest}: skipped ({es.get('reason', 'disabled')}).")
        elif es.get("success"):
            tgt = es.get("table") or es.get("sobject") or es.get("endpoint") or dest
            sent = es.get("created", es.get("rows_sent", es.get("rows")))
            lines.append(f"  Loaded {sent} record(s) to {dest}: {tgt}.")
        else:
            lines.append(f"  Destination {dest}: FAILED — {es.get('error') or es.get('errors')}")
    lines += ["", "Attached: <stem>_good.csv, _duplicate.csv, _bad.csv (non-empty layers only)."]
    return "\n".join(lines)


def send_results_email(to_addr: str, result: RoutingResult,
                       smtp_cfg: Optional[Dict[str, Any]] = None,
                       in_reply_to: str = "", subject_prefix: str = "FFIS Result") -> None:
    """Reply to the sender with good/duplicate/bad CSVs and a summary."""
    smtp_cfg = smtp_cfg or get_smtp_config()
    user = smtp_cfg.get("from_email", "")
    pwd = smtp_cfg.get("app_password", "")
    host = smtp_cfg.get("host", "smtp.gmail.com")
    port = int(smtp_cfg.get("port", 587))
    if not (user and pwd and to_addr):
        raise RuntimeError("SMTP credentials or recipient missing; cannot send reply.")

    stem = re.sub(r"\.[^.]+$", "", result.filename) or result.object_type.lower()
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_") or "ingest"

    msg = MIMEMultipart()
    msg["From"] = user
    msg["To"] = to_addr
    c = result.counts
    msg["Subject"] = (f"{subject_prefix}: {result.object_type} — "
                      f"{c['good']} good / {c['duplicate']} dup / {c['bad']} bad")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.attach(MIMEText(build_summary(result), "plain"))

    for label, frame in (("good", result.good),
                         ("duplicate", result.duplicate),
                         ("bad", result.bad)):
        if frame is None or frame.empty:
            continue
        part = MIMEBase("application", "octet-stream")
        part.set_payload(_csv_bytes(frame))
        encoders.encode_base64(part)
        part.add_header("Content-Disposition",
                        f"attachment; filename={stem}_{label}.csv")
        msg.attach(part)

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, pwd)
        server.sendmail(user, [a.strip() for a in to_addr.split(",")], msg.as_string())
    log.info("Sent results to %s (%s).", to_addr, c)


# ══════════════════════════════════════════════════════════════════════════
# ORCHESTRATION
# ══════════════════════════════════════════════════════════════════════════
def get_pipeline_config() -> Dict[str, Any]:
    return {
        "subject_filter": getenv_str("FFIS_PIPELINE_SUBJECT_FILTER", ""),
        "only_unseen": getenv_bool("FFIS_PIPELINE_ONLY_UNSEEN", True),
        "mark_seen": getenv_bool("FFIS_PIPELINE_MARK_SEEN", True),
        "dedup_backend": getenv_str("FFIS_DEDUP_BACKEND", "auto"),
        "reference_dir": getenv_str("FFIS_REFERENCE_DIR", ""),
        "reply": getenv_bool("FFIS_PIPELINE_REPLY", True),
        "export_good": getenv_bool("FFIS_EXPORT_GOOD", False),
        "export_backend": getenv_str("FFIS_EXPORT_BACKEND", "auto"),
        "export_if_exists": getenv_str("FFIS_EXPORT_IF_EXISTS", "append"),
    }


def process_message(m: InboundMessage, dedup: DedupSource, cfg: Dict[str, Any],
                    send: bool = True, exporter: Optional[Exporter] = None) -> List[RoutingResult]:
    """Process every attachment in one inbound email; reply once per attachment."""
    out: List[RoutingResult] = []
    allow = get_allowed_senders()
    if not sender_allowed(m.sender, allow):
        log.warning("BLOCKED sender %s (not in FFIS_ALLOWED_SENDERS); skipping %d attachment(s).",
                    m.sender or "(empty)", len(m.attachments))
        return out
    for fname, data in m.attachments:
        try:
            df = read_csv_bytes(data, fname)
        except Exception as e:  # noqa: BLE001
            log.error("Skipping %s from %s: %s", fname, m.sender, e)
            continue
        result = route_records(df, filename=fname, dedup=dedup)
        log.info("Processed %s from %s -> %s %s",
                 fname, m.sender, result.object_type, result.counts)
        # Auto-export the GOOD layer onward (gated by FFIS_EXPORT_GOOD).
        if exporter is not None and cfg.get("export_good", False):
            result.export_status = exporter.export(result.good, result.object_type)
            log.info("Export (%s) for %s: %s", exporter.name, fname, result.export_status)
        if send and cfg.get("reply", True) and m.sender:
            try:
                send_results_email(m.sender, result, in_reply_to=m.message_id_header)
            except Exception as e:  # noqa: BLE001
                log.error("Failed to send reply to %s: %s", m.sender, e)
        out.append(result)
    return out


def run_once(send: bool = True) -> List[RoutingResult]:
    cfg = get_pipeline_config()
    imap = get_imap_config()
    user = getenv_str("IMAP_USER", getenv_str("SMTP_FROM_EMAIL", ""))
    pwd = getenv_str("IMAP_PASSWORD", getenv_str("SMTP_APP_PASSWORD", ""))
    if not (user and pwd):
        raise RuntimeError("IMAP_USER / IMAP_PASSWORD (or SMTP_* equivalents) not set.")
    dedup = make_dedup_source(cfg["dedup_backend"], cfg["reference_dir"])
    exporter = make_exporter(cfg["export_backend"], cfg["export_if_exists"]) \
        if cfg["export_good"] else NoneExporter()
    log.info("Polling %s as %s (dedup: %s, export: %s%s).",
             imap["host"], user, dedup.name,
             exporter.name if cfg["export_good"] else "off",
             "" if cfg["export_good"] else " (FFIS_EXPORT_GOOD=false)")
    messages = fetch_inbound_messages(
        host=imap["host"], port=imap["port"], use_ssl=imap["use_ssl"],
        username=user, password=pwd, folder=imap["folder"],
        subject_filter=cfg["subject_filter"], only_unseen=cfg["only_unseen"],
        mark_seen=cfg["mark_seen"],
    )
    log.info("Found %d message(s) with attachments.", len(messages))
    results: List[RoutingResult] = []
    for m in messages:
        results.extend(process_message(m, dedup, cfg, send=send, exporter=exporter))
    return results


def run_watch(interval: int = 120, send: bool = True) -> None:
    log.info("Watching inbox every %ds. Ctrl-C to stop.", interval)
    while True:
        try:
            run_once(send=send)
        except Exception as e:  # noqa: BLE001
            log.error("Poll cycle failed: %s", e)
        time.sleep(max(15, interval))


def process_local_file(path: str) -> RoutingResult:
    """Offline test: classify + route a local CSV, no email involved."""
    cfg = get_pipeline_config()
    dedup = make_dedup_source(cfg["dedup_backend"], cfg["reference_dir"])
    with open(path, "rb") as f:
        df = read_csv_bytes(f.read(), os.path.basename(path))
    result = route_records(df, filename=os.path.basename(path), dedup=dedup)
    if cfg["export_good"]:
        exporter = make_exporter(cfg["export_backend"], cfg["export_if_exists"])
        result.export_status = exporter.export(result.good, result.object_type)
    print(build_summary(result))
    return result


def test_salesforce_auth() -> int:
    """Authenticate against Salesforce and print a short status. CLI: --sf-test."""
    try:
        conn = SalesforceConnector.from_config()
        status = conn.test_connection()
    except Exception as e:  # noqa: BLE001
        print(f"Salesforce auth FAILED: {e}")
        return 1
    if status.get("success"):
        print(f"Salesforce auth OK -> {status['instance_url']} "
              f"(API {status['api_version']})")
        return 0
    print(f"Salesforce reachable but /limits returned {status.get('status_code')}: "
          f"{status.get('detail')}")
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="FFIS automated email ingestion pipeline.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true", help="Poll the inbox a single time.")
    g.add_argument("--watch", action="store_true", help="Poll continuously.")
    g.add_argument("--file", metavar="CSV", help="Classify+route a local CSV (no email).")
    g.add_argument("--sf-test", action="store_true", help="Test Salesforce auth and exit.")
    p.add_argument("--interval", type=int, default=120, help="Seconds between polls (--watch).")
    p.add_argument("--no-send", action="store_true", help="Process but do not send replies.")
    p.add_argument("--export", dest="export", action="store_true", default=None,
                   help="Force auto-export of the good layer on (overrides FFIS_EXPORT_GOOD).")
    p.add_argument("--no-export", dest="export", action="store_false",
                   help="Force auto-export off.")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    # CLI export flag overrides the FFIS_EXPORT_GOOD env var for this run.
    if args.export is not None:
        os.environ["FFIS_EXPORT_GOOD"] = "true" if args.export else "false"

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Keep third-party HTTP/SDK debug noise (and row data) out of --verbose.
    for _noisy in ("anthropic", "httpx", "httpcore", "urllib3"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
    if args.sf_test:
        return test_salesforce_auth()
    if args.file:
        process_local_file(args.file)
        return 0
    if args.once:
        run_once(send=not args.no_send)
        return 0
    if args.watch:
        run_watch(interval=args.interval, send=not args.no_send)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())