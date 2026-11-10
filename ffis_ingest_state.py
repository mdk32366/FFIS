"""
ffis_ingest_state.py
====================
Durable ingest state for the FFIS email pipeline (SPEC ffis-ingest-hardening@0.0).

* SQLite file ``$FFIS_STATE_DIR/ingest.db`` (WAL) holding:
    - ``watermark(folder PK, uidvalidity, last_uid, updated_at)``
    - ``ledger(key PK, uidvalidity, uid, message_id, internaldate, status,
               attempts, first_seen_at, done_at, reply_sent)``
* Atomic JSON writes (tmp file -> fsync -> rename) for ``heartbeat.json`` and
  ``stall.json``.
* In-memory fallback (with a WARNING) when the state dir is not writable,
  e.g. on Fly where the ingest has no volume.
* A best-effort stall/poison alert sender (SPEC section 5) with its own
  timeout and a 1-per-30-min rate limit tracked in ``stall.json``.

Stdlib only, so ``ffis_ingest_health.py`` can import it without pandas.
No secret value is ever logged or written by this module.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("ffis.ingest_state")

DEFAULT_STATE_DIR = "/state"
TERMINAL_STATUSES = ("done", "skipped_no_attachment", "blocked_sender", "poison")
ALERT_MIN_GAP_S = 30 * 60


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def state_dir() -> str:
    return os.environ.get("FFIS_STATE_DIR", DEFAULT_STATE_DIR) or DEFAULT_STATE_DIR


def utcnow_iso(ts: Optional[float] = None) -> str:
    ts = time.time() if ts is None else ts
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def dir_writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".probe-", dir=path)
        os.close(fd)
        os.unlink(tmp)
        return True
    except OSError:
        return False


# ──────────────────────────────────────────────────────────────────────────
# Atomic JSON files (heartbeat.json / stall.json) with in-memory fallback
# ──────────────────────────────────────────────────────────────────────────
_MEM_FILES: Dict[str, Dict[str, Any]] = {}
_MEM_LOCK = threading.Lock()


def write_json_atomic(path: str, obj: Dict[str, Any]) -> bool:
    """tmp -> fsync -> rename. Returns False (and keeps an in-memory copy) on failure."""
    with _MEM_LOCK:
        _MEM_FILES[path] = dict(obj)
    d = os.path.dirname(path) or "."
    try:
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=d)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(obj, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            dfd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
        return True
    except OSError:
        return False


def read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        with _MEM_LOCK:
            mem = _MEM_FILES.get(path)
        return dict(mem) if mem is not None else None


# ──────────────────────────────────────────────────────────────────────────
# Heartbeat (SPEC section 2)
# ──────────────────────────────────────────────────────────────────────────
class Heartbeat:
    """Thread-safe heartbeat + stage tracker, mirrored to heartbeat.json."""

    def __init__(self, sdir: Optional[str] = None, version: str = "ffis-ingest-hardening@0.0"):
        self.dir = sdir or state_dir()
        self.path = os.path.join(self.dir, "heartbeat.json")
        self.version = version
        self.lock = threading.Lock()
        self.process_started_at = time.time()
        prev = read_json(self.path) or {}
        self.data: Dict[str, Any] = {
            "poll_started_at": None,
            "poll_finished_at": None,
            "last_ok_at": prev.get("last_ok_at"),
            "last_result": prev.get("last_result"),
            "found": prev.get("found", 0),
            "pid": os.getpid(),
            "version": version,
        }
        self.stage = "idle"
        self.in_flight = False
        self._poll_started_ts: Optional[float] = None
        self.writable = dir_writable(self.dir)
        if not self.writable:
            log.warning("State dir %s is not writable; heartbeat kept in memory only.", self.dir)
        self._flush()

    def _flush(self) -> None:
        snap = dict(self.data)
        snap["stage"] = self.stage
        write_json_atomic(self.path, snap)

    def set_stage(self, stage: str) -> None:
        with self.lock:
            self.stage = stage

    def poll_start(self) -> None:
        with self.lock:
            now = time.time()
            self._poll_started_ts = now
            self.in_flight = True
            self.stage = "connect"
            self.data["poll_started_at"] = utcnow_iso(now)
            self.data["pid"] = os.getpid()
            self._flush()

    def imap_ok(self, found: int) -> None:
        with self.lock:
            self.data["last_ok_at"] = utcnow_iso()
            self.data["found"] = int(found)

    def poll_finish(self, ok: bool) -> None:
        with self.lock:
            self.in_flight = False
            self.data["poll_finished_at"] = utcnow_iso()
            self.data["last_result"] = "ok" if ok else "error"
            self.stage = "idle"
            self._flush()

    def snapshot(self) -> Tuple[bool, Optional[float], Optional[float], str]:
        """(in_flight, poll_started_ts, last_ok_ts, stage)."""
        with self.lock:
            return (self.in_flight, self._poll_started_ts,
                    parse_iso(self.data.get("last_ok_at")), self.stage)


# ──────────────────────────────────────────────────────────────────────────
# SQLite watermark + ledger (SPEC section 4)
# ──────────────────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS watermark (
    folder TEXT PRIMARY KEY,
    uidvalidity INTEGER,
    last_uid INTEGER,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
    key TEXT PRIMARY KEY,
    uidvalidity INTEGER,
    uid INTEGER,
    message_id TEXT,
    internaldate TEXT,
    status TEXT CHECK(status IN ('done','skipped_no_attachment','blocked_sender','failed','poison')),
    attempts INTEGER DEFAULT 0,
    first_seen_at TEXT,
    done_at TEXT,
    reply_sent INTEGER DEFAULT 0
);
"""


class IngestState:
    def __init__(self, sdir: Optional[str] = None):
        self.dir = sdir or state_dir()
        self.path = os.path.join(self.dir, "ingest.db")
        self.in_memory = False
        conn = None
        if dir_writable(self.dir):
            try:
                conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
            except sqlite3.Error as e:
                log.warning("Could not open %s (%s); using in-memory ingest state.", self.path, e)
                conn = None
        if conn is None:
            log.warning("State dir %s not writable; ingest state is IN MEMORY ONLY "
                        "(watermark/ledger will not survive a restart).", self.dir)
            conn = sqlite3.connect(":memory:", isolation_level=None)
            self.in_memory = True
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        self.conn = conn

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # watermark
    def get_watermark(self, folder: str) -> Optional[Tuple[int, int]]:
        r = self.conn.execute("SELECT uidvalidity, last_uid FROM watermark WHERE folder=?",
                              (folder,)).fetchone()
        return (int(r["uidvalidity"]), int(r["last_uid"])) if r else None

    def set_watermark(self, folder: str, uidvalidity: int, last_uid: int) -> None:
        self.conn.execute(
            "INSERT INTO watermark(folder, uidvalidity, last_uid, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(folder) DO UPDATE SET uidvalidity=excluded.uidvalidity, "
            "last_uid=excluded.last_uid, updated_at=excluded.updated_at",
            (folder, int(uidvalidity), int(last_uid), utcnow_iso()))

    # ledger
    def get_entry(self, key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM ledger WHERE key=?", (key,)).fetchone()

    def last_done_internaldate(self) -> Optional[str]:
        r = self.conn.execute(
            "SELECT MAX(internaldate) AS d FROM ledger WHERE status='done' "
            "AND internaldate IS NOT NULL").fetchone()
        return r["d"] if r and r["d"] else None

    def commit_terminal(self, *, key: str, folder: str, uidvalidity: int, uid: int,
                        message_id: str, internaldate: Optional[str], status: str,
                        reply_sent: int = 0, advance: bool = True) -> None:
        """ONE transaction: ledger row -> terminal status, then (optionally) last_uid=uid."""
        assert status in TERMINAL_STATUSES, status
        now = utcnow_iso()
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute(
                "INSERT INTO ledger(key, uidvalidity, uid, message_id, internaldate, status, "
                "attempts, first_seen_at, done_at, reply_sent) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET uidvalidity=excluded.uidvalidity, "
                "uid=excluded.uid, internaldate=COALESCE(excluded.internaldate, internaldate), "
                "status=excluded.status, done_at=excluded.done_at, "
                "reply_sent=MAX(reply_sent, excluded.reply_sent)",
                (key, int(uidvalidity), int(uid), message_id or None, internaldate, status,
                 1 if status == "done" else 0, now, now, int(reply_sent)))
            if advance:
                c.execute(
                    "INSERT INTO watermark(folder, uidvalidity, last_uid, updated_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(folder) DO UPDATE SET "
                    "uidvalidity=excluded.uidvalidity, "
                    "last_uid=MAX(watermark.last_uid, excluded.last_uid), "
                    "updated_at=excluded.updated_at",
                    (folder, int(uidvalidity), int(uid), now))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def record_failure(self, *, key: str, uidvalidity: int, uid: int, message_id: str,
                       internaldate: Optional[str]) -> int:
        """attempts += 1, status 'failed'. Returns the new attempt count."""
        now = utcnow_iso()
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute(
                "INSERT INTO ledger(key, uidvalidity, uid, message_id, internaldate, status, "
                "attempts, first_seen_at, reply_sent) VALUES(?,?,?,?,?,'failed',1,?,0) "
                "ON CONFLICT(key) DO UPDATE SET attempts=attempts+1, status='failed', "
                "uidvalidity=excluded.uidvalidity, uid=excluded.uid",
                (key, int(uidvalidity), int(uid), message_id or None, internaldate, now))
            n = c.execute("SELECT attempts FROM ledger WHERE key=?", (key,)).fetchone()[0]
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        return int(n)

    def counts(self) -> Dict[str, int]:
        return {r["status"]: r["n"] for r in self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM ledger GROUP BY status")}


# ──────────────────────────────────────────────────────────────────────────
# Alerts (SPEC section 5) — best effort, own timeout, rate limited
# ──────────────────────────────────────────────────────────────────────────
def stall_path(sdir: Optional[str] = None) -> str:
    return os.path.join(sdir or state_dir(), "stall.json")


def _smtp_settings() -> Dict[str, Any]:
    try:
        from config import get_smtp_config  # type: ignore
        cfg = get_smtp_config()
    except Exception:  # noqa: BLE001
        cfg = {"host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
               "port": _env_int("SMTP_PORT", 587),
               "from_email": os.environ.get("SMTP_FROM_EMAIL", ""),
               "app_password": os.environ.get("SMTP_APP_PASSWORD", "")}
    return cfg


def send_alert(subject: str, lines: Dict[str, Any], *, sdir: Optional[str] = None,
               rate_limit: bool = True, timeout: float = 15.0) -> str:
    """
    Send a plain-text alert to FFIS_ALERT_TO. Never includes message contents
    or credentials. Returns one of: 'sent', 'no_recipient', 'rate_limited', 'failed'.
    Always logs a CRITICAL line first, so an unset recipient still leaves a trace.
    """
    body_lines = [f"{k}: {v}" for k, v in lines.items()]
    body_lines.append(f"host: {socket.gethostname()}")
    log.critical("ALERT: %s | %s", subject, "; ".join(body_lines))
    to_addr = os.environ.get("FFIS_ALERT_TO", "").strip()
    if not to_addr:
        return "no_recipient"
    sp = stall_path(sdir)
    st = read_json(sp) or {}
    now = time.time()
    last = parse_iso(st.get("last_alert_at"))
    if rate_limit and last is not None and now - last < ALERT_MIN_GAP_S:
        log.warning("Alert rate-limited (last alert %.0fs ago).", now - last)
        return "rate_limited"
    import smtplib
    from email.mime.text import MIMEText
    cfg = _smtp_settings()
    user, pwd = cfg.get("from_email", ""), cfg.get("app_password", "")
    if not (user and pwd):
        log.error("Alert not sent: SMTP credentials missing.")
        return "failed"
    msg = MIMEText("\n".join(body_lines), "plain")
    msg["From"] = user
    msg["To"] = to_addr
    msg["Subject"] = f"[FFIS ingest] {subject}"
    try:
        with smtplib.SMTP(cfg.get("host", "smtp.gmail.com"), int(cfg.get("port", 587)),
                          timeout=timeout) as s:
            s.starttls()
            s.login(user, pwd)
            s.sendmail(user, [a.strip() for a in to_addr.split(",")], msg.as_string())
    except Exception as e:  # noqa: BLE001
        log.error("Alert send failed: %s", type(e).__name__)
        return "failed"
    st = read_json(sp) or {}
    st["last_alert_at"] = utcnow_iso(now)
    write_json_atomic(sp, st)
    return "sent"
