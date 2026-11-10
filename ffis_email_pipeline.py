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
import ssl
import sys
import threading
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

# Ingest hardening (SPEC ffis-ingest-hardening@0.0): durable state, heartbeat,
# alerts. Stdlib-only helper module that ships next to this file.
try:
    import ffis_ingest_state as _ist
except Exception:  # pragma: no cover - module missing outside the repo
    _ist = None


def get_ingest_config() -> Dict[str, Any]:
    """Hardening knobs (all env, safe defaults). See docs/CUTOVER-mdkappsvr.md."""
    mode = getenv_str("FFIS_INGEST_MODE", "watermark").strip().lower() or "watermark"
    if mode not in ("watermark", "legacy_unseen"):
        log.warning("Unknown FFIS_INGEST_MODE=%r; using 'watermark'.", mode)
        mode = "watermark"
    return {
        "mode": mode,
        "imap_timeout_s": float(getenv_int("FFIS_IMAP_TIMEOUT_S", 30)),
        "poll_deadline_s": float(getenv_int("FFIS_POLL_DEADLINE_S", 300)),
        "smtp_timeout_s": float(getenv_int("FFIS_SMTP_TIMEOUT_S", 30)),
        "stall_cycles": getenv_int("FFIS_STALL_CYCLES", 4),
        "max_attempts": max(1, getenv_int("FFIS_MAX_ATTEMPTS", 3)),
        "imap_starttls": getenv_bool("FFIS_IMAP_STARTTLS", True),
    }


# Current poll stage, for logs / stall.json:
#   connect | login | select | search | fetch:<uid> | store | process:<uid> | reply:<uid> | logout
_STAGE = {"stage": "idle"}
_HEARTBEAT = None  # ffis_ingest_state.Heartbeat, created lazily


def _set_stage(stage: str) -> None:
    _STAGE["stage"] = stage
    if _HEARTBEAT is not None:
        _HEARTBEAT.set_stage(stage)


def current_stage() -> str:
    return _STAGE["stage"]


def _get_heartbeat():
    global _HEARTBEAT
    if _HEARTBEAT is None and _ist is not None:
        _HEARTBEAT = _ist.Heartbeat()
    return _HEARTBEAT

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


def _imap_connect(host: str, port: int, use_ssl: bool, timeout: float,
                  starttls: bool = True, ssl_context: Optional[ssl.SSLContext] = None):
    """Open an IMAP connection whose socket has a timeout (SPEC section 1).

    The timeout covers TCP connect, TLS handshake, greeting and every later
    blocking recv/send (LOGIN, SELECT, SEARCH, FETCH, STORE, LOGOUT).
    """
    _set_stage("connect")
    if use_ssl:
        return imaplib.IMAP4_SSL(host, port, ssl_context=ssl_context, timeout=timeout)
    M = imaplib.IMAP4(host, port, timeout=timeout)
    if starttls:
        M.starttls(ssl_context=ssl_context)
    return M


def _imap_close(M, clean: bool) -> None:
    """LOGOUT on a clean finish; on failure skip the round-trip and shut the socket."""
    if M is None:
        return
    if clean:
        _set_stage("logout")
        try:
            M.logout()
            return
        except Exception:  # noqa: BLE001
            pass
    try:
        M.shutdown()
    except Exception:  # noqa: BLE001
        pass


def _extract_attachments(msg) -> List[Tuple[str, bytes]]:
    """CSV/TXT attachment rule (unchanged from the original fetch loop)."""
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
    return atts


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
    imap_timeout: Optional[float] = None,
    starttls: Optional[bool] = None,
    ssl_context: Optional[ssl.SSLContext] = None,
) -> List[InboundMessage]:
    """
    Connect to an IMAP mailbox and return one InboundMessage per email that
    contains at least one CSV/TXT attachment.

    Unlike the UI's `fetch_csv_from_imap`, this captures the sender address,
    subject, and Message-ID so the pipeline can reply to the originator.
    """
    icfg = get_ingest_config()
    timeout = icfg["imap_timeout_s"] if imap_timeout is None else imap_timeout
    use_starttls = icfg["imap_starttls"] if starttls is None else starttls
    results: List[InboundMessage] = []
    M = None
    clean = False
    try:
        M = _imap_connect(host, port, use_ssl, timeout, use_starttls, ssl_context)
        _set_stage("login")
        M.login(username, password)
        _set_stage("select")
        M.select(folder)

        base = "UNSEEN" if only_unseen else "ALL"
        criterion = f'({base} SUBJECT "{subject_filter}")' if subject_filter.strip() else base

        _set_stage("search")
        typ, data = M.search(None, criterion)
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")

        for msg_id in data[0].split():
            _set_stage(f"fetch:{msg_id.decode(errors='replace')}")
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

            atts = _extract_attachments(msg)

            if atts:
                results.append(InboundMessage(
                    msg_id=msg_id, sender=sender, subject=subject,
                    message_id_header=mid, attachments=atts,
                ))
                if mark_seen:
                    # legacy_unseen semantics (unchanged): \Seen before processing.
                    _set_stage("store")
                    M.store(msg_id, "+FLAGS", "\\Seen")
        clean = True
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"IMAP error at stage={current_stage()}: {e}")
    finally:
        _imap_close(M, clean)
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

    smtp_timeout = get_ingest_config()["smtp_timeout_s"]
    with smtplib.SMTP(host, port, timeout=smtp_timeout) as server:
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
                    send: bool = True, exporter: Optional[Exporter] = None,
                    reply_status: Optional[List[bool]] = None) -> List[RoutingResult]:
    """Process every attachment in one inbound email; reply once per attachment.

    If ``reply_status`` is given, one bool per attempted reply is appended
    (True only after ``sendmail`` returned).
    """
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
            _set_stage(f"reply:{m.msg_id.decode(errors='replace') if isinstance(m.msg_id, bytes) else m.msg_id}")
            try:
                send_results_email(m.sender, result, in_reply_to=m.message_id_header)
                if reply_status is not None:
                    reply_status.append(True)
            except Exception as e:  # noqa: BLE001
                log.error("Failed to send reply to %s: %s", m.sender, e)
                if reply_status is not None:
                    reply_status.append(False)
        out.append(result)
    return out


# ══════════════════════════════════════════════════════════════════════════
# INGEST HARDENING — UID watermark path (SPEC section 4)
# ══════════════════════════════════════════════════════════════════════════
_IMAP_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_ATTACH_CTYPES = ("text/csv", "text/plain", "application/octet-stream",
                  "application/vnd.ms-excel")


def _imap_tokens(items) -> list:
    """Parse an imaplib FETCH response (list of bytes / (meta, literal) tuples)
    into nested Python lists. Literals become bytes; atoms/quoted become str."""
    stream: list = []           # sequence of ("text", bytes) / ("lit", bytes)
    for it in items:
        if isinstance(it, tuple):
            meta, lit = it[0], it[1]
            meta = re.sub(rb"\{\d+\}$", b"", meta)
            stream.append(("text", meta))
            stream.append(("lit", lit))
        elif isinstance(it, bytes):
            stream.append(("text", it))
    root: list = []
    stack = [root]
    for kind, chunk in stream:
        if kind == "lit":
            stack[-1].append(chunk)
            continue
        s = chunk.decode("utf-8", "replace")
        i, n = 0, len(s)
        while i < n:
            c = s[i]
            if c in " \r\n":
                i += 1
            elif c == "(":
                new: list = []
                stack[-1].append(new)
                stack.append(new)
                i += 1
            elif c == ")":
                if len(stack) > 1:
                    stack.pop()
                i += 1
            elif c == '"':
                j, buf = i + 1, []
                while j < n and s[j] != '"':
                    if s[j] == "\\" and j + 1 < n:
                        j += 1
                    buf.append(s[j])
                    j += 1
                stack[-1].append(buf and "".join(buf) or "")
                i = j + 1
            else:
                j, depth = i, 0
                while j < n and (depth > 0 or s[j] not in " ()\r\n"):
                    if s[j] == "[":
                        depth += 1
                    elif s[j] == "]":
                        depth -= 1
                    j += 1
                tok = s[i:j]
                stack[-1].append(None if tok.upper() == "NIL" else tok)
                i = j
    return root


def _fetch_pairs(items) -> Dict[str, Any]:
    """{ 'UID': '12', 'INTERNALDATE': '...', 'BODYSTRUCTURE': [...], 'BODY[...]': b'...' }"""
    toks = _imap_tokens(items)
    # toks: [seq, 'FETCH'?, [k, v, k, v ...]] — imaplib strips the '* ' prefix
    lst = next((t for t in toks if isinstance(t, list)), [])
    out: Dict[str, Any] = {}
    for k, v in zip(lst[0::2], lst[1::2]):
        if isinstance(k, str):
            key = k.upper()
            if key.startswith("BODY["):
                key = "BODY[" + key.split("[", 1)[1].split()[0].rstrip("]") + "]"
            out[key] = v
    return out


def _bs_param(params, names) -> Optional[str]:
    if not isinstance(params, list):
        return None
    for k, v in zip(params[0::2], params[1::2]):
        if isinstance(k, str) and k.upper().rstrip("*") in names and isinstance(v, (str, bytes)):
            return v.decode("utf-8", "replace") if isinstance(v, bytes) else v
    return None


def _bs_parts(bs) -> List[Tuple[str, Optional[str]]]:
    """Flatten BODYSTRUCTURE into [(content_type, filename_or_None)]."""
    out: List[Tuple[str, Optional[str]]] = []
    if not isinstance(bs, list) or not bs:
        return out
    if isinstance(bs[0], list):                    # multipart
        for p in bs:
            if isinstance(p, list):
                out.extend(_bs_parts(p))
        return out
    ctype = f"{str(bs[0] or '').lower()}/{str(bs[1] if len(bs) > 1 and bs[1] else '').lower()}"
    fname = _bs_param(bs[2] if len(bs) > 2 else None, ("NAME",))
    for ext in bs[7:]:                              # disposition: ("ATTACHMENT" ("FILENAME" "x"))
        if (isinstance(ext, list) and len(ext) >= 2 and isinstance(ext[0], str)
                and isinstance(ext[1], list)):
            fn = _bs_param(ext[1], ("FILENAME",))
            if fn:
                fname = fn
        if ctype == "message/rfc822" and isinstance(ext, list) and ext and isinstance(ext[0], (list, str)):
            pass
    out.append((ctype, _decode_hdr(fname) if fname else None))
    return out


def bodystructure_has_attachment(bs) -> bool:
    """BODYSTRUCTURE pre-check mirroring _extract_attachments' rule."""
    for ctype, fname in _bs_parts(bs):
        if fname and (fname.lower().endswith((".csv", ".txt")) or ctype in _ATTACH_CTYPES):
            return True
    return False


def _internaldate_iso(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.strptime(value.strip(), "%d-%b-%Y %H:%M:%S %z")
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def _since_criterion(iso: str, days_back: int = 2) -> str:
    from datetime import datetime, timedelta
    d = datetime.strptime(iso[:10], "%Y-%m-%d") - timedelta(days=days_back)
    return f"SINCE {d.day:02d}-{_IMAP_MONTHS[d.month - 1]}-{d.year}"


def _uid_search(M, criterion: str) -> List[int]:
    _set_stage("search")
    typ, data = M.uid("SEARCH", None, criterion)
    if typ != "OK":
        raise RuntimeError(f"IMAP UID SEARCH failed: {typ}")
    return sorted({int(x) for x in (data[0] or b"").split() if x.isdigit()})


def _select_info(M, folder: str) -> Tuple[int, Optional[int]]:
    _set_stage("select")
    typ, _ = M.select(folder)
    if typ != "OK":
        raise RuntimeError(f"IMAP SELECT {folder} failed: {typ}")
    _, v = M.response("UIDVALIDITY")
    _, nx = M.response("UIDNEXT")
    if not v or v[0] is None:
        raise RuntimeError("IMAP server did not return UIDVALIDITY")
    uidnext = int(nx[0]) if nx and nx[0] is not None else None
    return int(v[0]), uidnext


def poll_watermark(M, folder: str, state, cfg: Dict[str, Any], dedup: DedupSource,
                   exporter: Optional[Exporter], send: bool, max_attempts: int) -> Dict[str, int]:
    """One watermark poll over an already-logged-in IMAP connection.

    Advances the watermark only after a durable ledger commit; sets \\Seen only
    after that commit (if MARK_SEEN). Processing failures stop the advance for
    this poll; after ``max_attempts`` the message becomes ``poison`` (alert) and
    the watermark moves on. IMAP/socket errors propagate to the caller.
    """
    uidvalidity, uidnext = _select_info(M, folder)
    wm = state.get_watermark(folder)
    resync = wm is None or wm[0] != uidvalidity
    subj = cfg.get("subject_filter", "").strip()
    subj_crit = f' SUBJECT "{subj}"' if subj else ""
    if resync:
        last_iso = state.last_done_internaldate()
        base = _since_criterion(last_iso) if last_iso else "UNSEEN"
        log.warning("Watermark resync for %s (stored uidvalidity=%s, server=%s): UID SEARCH %s.",
                    folder, wm[0] if wm else None, uidvalidity, base)
        uids = _uid_search(M, base + subj_crit)
        last_uid = 0
    else:
        last_uid = wm[1]
        uids = [u for u in _uid_search(M, f"UID {last_uid + 1}:*{subj_crit}") if u > last_uid]
    stats = {"new": len(uids), "with_attachments": 0, "done": 0, "skipped": 0,
             "failed": 0, "poison": 0}
    stopped = False
    for uid in uids:
        outcome = _handle_uid(M, folder, uid, uidvalidity, state, cfg, dedup, exporter, send,
                              max_attempts, advance=not resync, stats=stats)
        if outcome == "stop":
            stopped = True
            break
    if resync and not stopped:
        top = (uidnext - 1) if uidnext else max(uids or [0])
        if not uidnext:
            allu = _uid_search(M, "ALL")
            top = max(allu or [0])
        state.set_watermark(folder, uidvalidity, max(top, max(uids or [0])))
        log.info("Watermark rebased: %s uidvalidity=%s last_uid=%s.", folder, uidvalidity,
                 max(top, max(uids or [0])))
    return stats


def _handle_uid(M, folder, uid, uidvalidity, state, cfg, dedup, exporter, send,
                max_attempts, advance, stats) -> str:
    _set_stage(f"fetch:{uid}")
    typ, data = M.uid("FETCH", str(uid),
                      "(UID INTERNALDATE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT)])")
    if typ != "OK":
        raise RuntimeError(f"IMAP UID FETCH {uid} failed: {typ}")
    if not data or data == [None]:
        log.info("UID %s vanished before fetch; skipping.", uid)
        return "advance"
    f = _fetch_pairs(data)
    hdr_raw = f.get("BODY[HEADER.FIELDS]") or b""
    if isinstance(hdr_raw, str):
        hdr_raw = hdr_raw.encode()
    hdr = email_lib.message_from_bytes(hdr_raw) if hdr_raw else email_lib.message.Message()
    mid = (hdr.get("Message-ID") or "").strip()
    sender = parseaddr(hdr.get("From", ""))[1].strip().lower()
    subject = _decode_hdr(hdr.get("Subject", ""))
    idate = _internaldate_iso(f.get("INTERNALDATE"))
    key = mid or f"{uidvalidity}:{uid}"
    common = dict(key=key, folder=folder, uidvalidity=uidvalidity, uid=uid,
                  message_id=mid, internaldate=idate)

    entry = state.get_entry(key)
    if entry is not None and entry["status"] in _ist.TERMINAL_STATUSES:
        log.info("UID %s already %s in ledger (key dedup); not reprocessing.", uid, entry["status"])
        stats["skipped"] += 1
        if advance:
            wm = state.get_watermark(folder)
            if wm is None or wm[1] < uid:
                state.set_watermark(folder, uidvalidity, uid)
        return "advance"

    if not bodystructure_has_attachment(f.get("BODYSTRUCTURE")):
        state.commit_terminal(status="skipped_no_attachment", advance=advance, **common)
        stats["skipped"] += 1
        return "advance"

    stats["with_attachments"] += 1
    if not sender_allowed(sender, get_allowed_senders()):
        log.warning("BLOCKED sender %s (not in FFIS_ALLOWED_SENDERS); UID %s.", sender or "(empty)", uid)
        state.commit_terminal(status="blocked_sender", advance=advance, **common)
        if cfg.get("mark_seen", True):
            _set_stage("store")
            M.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
        stats["skipped"] += 1
        return "advance"

    _set_stage(f"fetch:{uid}")
    typ2, raw = M.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if typ2 != "OK":
        raise RuntimeError(f"IMAP UID FETCH {uid} body failed: {typ2}")
    body = _fetch_pairs(raw).get("BODY[]") if raw else None
    if isinstance(body, str):
        body = body.encode()
    msg = email_lib.message_from_bytes(body or b"")
    atts = _extract_attachments(msg)
    if not atts:
        state.commit_terminal(status="skipped_no_attachment", advance=advance, **common)
        stats["skipped"] += 1
        return "advance"
    m = InboundMessage(msg_id=str(uid).encode(), sender=sender, subject=subject,
                       message_id_header=msg.get("Message-ID", "") or mid, attachments=atts)
    _set_stage(f"process:{uid}")
    replies: List[bool] = []
    try:
        process_message(m, dedup, cfg, send=send, exporter=exporter, reply_status=replies)
    except Exception as e:  # noqa: BLE001
        attempts = state.record_failure(key=key, uidvalidity=uidvalidity, uid=uid,
                                        message_id=mid, internaldate=idate)
        if attempts >= max_attempts:
            state.commit_terminal(status="poison", advance=advance, **common)
            stats["poison"] += 1
            log.critical("POISON: UID %s failed %d time(s) (%s); marked poison, advancing.",
                         uid, attempts, type(e).__name__)
            _ist.send_alert("poison message", {"uid": uid, "uidvalidity": uidvalidity,
                                               "attempts": attempts, "error": type(e).__name__,
                                               "stage": f"process:{uid}"})
            return "advance"
        stats["failed"] += 1
        log.error("Processing UID %s failed (attempt %d/%d): %s. Not advancing this poll.",
                  uid, attempts, max_attempts, e)
        return "stop"
    reply_sent = 1 if replies and all(replies) else 0
    state.commit_terminal(status="done", reply_sent=reply_sent, advance=advance, **common)
    stats["done"] += 1
    if cfg.get("mark_seen", True):
        # Only AFTER the durable commit. \Seen is no longer the source of truth.
        _set_stage("store")
        M.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
    return "advance"


def fetch_and_process_watermark(imap: Dict[str, Any], user: str, pwd: str, cfg: Dict[str, Any],
                                dedup: DedupSource, exporter: Optional[Exporter], send: bool,
                                icfg: Dict[str, Any], state=None) -> Dict[str, int]:
    own_state = state is None
    if own_state:
        state = _ist.IngestState()
    M = None
    clean = False
    try:
        try:
            M = _imap_connect(imap["host"], imap["port"], imap["use_ssl"],
                              icfg["imap_timeout_s"], icfg["imap_starttls"])
            _set_stage("login")
            M.login(user, pwd)
            stats = poll_watermark(M, imap["folder"], state, cfg, dedup, exporter, send,
                                   icfg["max_attempts"])
            clean = True
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"IMAP error at stage={current_stage()}: {e}")
        finally:
            _imap_close(M, clean)
    finally:
        if own_state:
            state.close()
    return stats


# ══════════════════════════════════════════════════════════════════════════
# INGEST HARDENING — stall watchdog (SPEC section 2)
# ══════════════════════════════════════════════════════════════════════════
class StallWatchdog(threading.Thread):
    """Daemon thread. Every ``tick`` s:
    * poll in flight longer than ``deadline`` -> CRITICAL, stall.json, alert, os._exit(70)
    * no successful IMAP leg for ``cycles * (interval + deadline)`` -> same, exit 71.
      The reference time is max(last_ok_at, process start) so a freshly
      restarted process always gets one full window (no crash loop on a
      stale heartbeat from the previous container).
    """

    def __init__(self, hb, deadline: float, interval: Optional[float], cycles: int,
                 tick: float = 10.0, exit_fn=None):
        super().__init__(daemon=True, name="ffis-stall-watchdog")
        self.hb, self.deadline, self.interval = hb, deadline, interval
        self.cycles, self.tick = cycles, tick
        self.exit_fn = exit_fn or os._exit

    def run(self) -> None:
        while True:
            time.sleep(self.tick)
            try:
                self.check()
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001
                log.error("Watchdog check error: %s", e)

    def check(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        in_flight, started, last_ok, stage = self.hb.snapshot()
        if in_flight and started is not None and now - started > self.deadline:
            self.trip(70, "STALL: poll exceeded deadline", stage, now, started, last_ok)
            return
        if self.interval is not None and self.cycles > 0:
            ref = max(last_ok or 0.0, self.hb.process_started_at)
            limit = self.cycles * (self.interval + self.deadline)
            if now - ref > limit:
                self.trip(71, f"STALL: no successful IMAP poll for >{limit:.0f}s",
                          stage, now, started, last_ok)

    def trip(self, code: int, reason: str, stage: str, now: float,
             started: Optional[float], last_ok: Optional[float]) -> None:
        poll_age = round(now - started, 1) if started else None
        ok_age = round(now - last_ok, 1) if last_ok else None
        log.critical("%s (stage=%s, poll_age_s=%s, last_ok_age_s=%s, exit=%d)",
                     reason, stage, poll_age, ok_age, code)
        sp = _ist.stall_path(self.hb.dir)
        st = _ist.read_json(sp) or {}
        st.update({"stalled_at": _ist.utcnow_iso(now), "exit_code": code, "reason": reason,
                   "stage": stage, "poll_age_s": poll_age, "last_ok_age_s": ok_age,
                   "pid": os.getpid(), "recovered_at": None})
        _ist.write_json_atomic(sp, st)
        t = threading.Thread(target=_ist.send_alert, daemon=True,
                             args=(f"ingest stall (exit {code})",
                                   {"reason": reason, "stage": stage, "poll_age_s": poll_age,
                                    "last_ok_age_s": ok_age, "exit_code": code}),
                             kwargs={"sdir": self.hb.dir})
        t.start()
        t.join(15)
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:  # noqa: BLE001
                pass
        self.exit_fn(code)


def _maybe_send_recovered(hb) -> None:
    sp = _ist.stall_path(hb.dir)
    st = _ist.read_json(sp)
    if not st or not st.get("stalled_at") or st.get("recovered_at"):
        return
    stalled = _ist.parse_iso(st.get("stalled_at"))
    dur = round(time.time() - stalled, 1) if stalled else None
    st["recovered_at"] = _ist.utcnow_iso()
    _ist.write_json_atomic(sp, st)
    log.warning("Recovered after stall (exit %s, %ss ago).", st.get("exit_code"), dur)
    _ist.send_alert("ingest recovered", {"stall_duration_s": dur,
                                         "previous_exit_code": st.get("exit_code"),
                                         "previous_stage": st.get("stage")},
                    sdir=hb.dir, rate_limit=False)


def run_once(send: bool = True) -> List[RoutingResult]:
    cfg = get_pipeline_config()
    icfg = get_ingest_config()
    imap = get_imap_config()
    user = getenv_str("IMAP_USER", getenv_str("SMTP_FROM_EMAIL", ""))
    pwd = getenv_str("IMAP_PASSWORD", getenv_str("SMTP_APP_PASSWORD", ""))
    if not (user and pwd):
        raise RuntimeError("IMAP_USER / IMAP_PASSWORD (or SMTP_* equivalents) not set.")
    dedup = make_dedup_source(cfg["dedup_backend"], cfg["reference_dir"])
    exporter = make_exporter(cfg["export_backend"], cfg["export_if_exists"]) \
        if cfg["export_good"] else NoneExporter()
    log.info("Polling %s as %s (dedup: %s, export: %s%s, mode: %s).",
             imap["host"], user, dedup.name,
             exporter.name if cfg["export_good"] else "off",
             "" if cfg["export_good"] else " (FFIS_EXPORT_GOOD=false)", icfg["mode"])
    hb = _get_heartbeat()
    if icfg["mode"] == "watermark" and _ist is not None:
        stats = fetch_and_process_watermark(imap, user, pwd, cfg, dedup, exporter, send, icfg)
        if hb is not None:
            hb.imap_ok(stats["with_attachments"])
        log.info("Found %d message(s) with attachments. (watermark: %d new uid(s), %d done, "
                 "%d skipped, %d failed, %d poison)", stats["with_attachments"], stats["new"],
                 stats["done"], stats["skipped"], stats["failed"], stats["poison"])
        return []
    messages = fetch_inbound_messages(
        host=imap["host"], port=imap["port"], use_ssl=imap["use_ssl"],
        username=user, password=pwd, folder=imap["folder"],
        subject_filter=cfg["subject_filter"], only_unseen=cfg["only_unseen"],
        mark_seen=cfg["mark_seen"],
    )
    if hb is not None:
        hb.imap_ok(len(messages))
    log.info("Found %d message(s) with attachments.", len(messages))
    results: List[RoutingResult] = []
    for m in messages:
        _set_stage(f"process:{m.msg_id.decode(errors='replace')}")
        results.extend(process_message(m, dedup, cfg, send=send, exporter=exporter))
    return results


def run_poll(send: bool = True) -> bool:
    """One heartbeat-wrapped poll. Returns True on success. Never raises."""
    hb = _get_heartbeat()
    if hb is not None:
        hb.poll_start()
    ok = False
    try:
        run_once(send=send)
        ok = True
    except Exception as e:  # noqa: BLE001
        log.error("Poll cycle failed: %s", e)
    finally:
        if hb is not None:
            hb.poll_finish(ok)
    if ok and hb is not None:
        try:
            _maybe_send_recovered(hb)
        except Exception as e:  # noqa: BLE001
            log.error("Recovered-alert check failed: %s", e)
    return ok


def start_watchdog(interval: Optional[float]) -> Optional["StallWatchdog"]:
    hb = _get_heartbeat()
    if hb is None:
        return None
    icfg = get_ingest_config()
    wd = StallWatchdog(hb, deadline=icfg["poll_deadline_s"], interval=interval,
                       cycles=icfg["stall_cycles"])
    wd.start()
    return wd


def run_watch(interval: int = 120, send: bool = True) -> None:
    icfg = get_ingest_config()
    sleep_s = max(15, interval)
    log.info("Watching inbox every %ds. Ctrl-C to stop. (mode=%s, imap_timeout=%ss, "
             "poll_deadline=%ss, smtp_timeout=%ss, stall_cycles=%s)", interval, icfg["mode"],
             icfg["imap_timeout_s"], icfg["poll_deadline_s"], icfg["smtp_timeout_s"],
             icfg["stall_cycles"])
    start_watchdog(float(sleep_s))
    while True:
        run_poll(send=send)
        time.sleep(sleep_s)


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
        start_watchdog(None)          # poll deadline only
        return 0 if run_poll(send=not args.no_send) else 1
    if args.watch:
        run_watch(interval=args.interval, send=not args.no_send)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())