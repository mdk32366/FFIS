"""
ffis_ingest_health.py — file-based health check for the FFIS ingest
(SPEC ffis-ingest-hardening@0.0, section 3). No HTTP listener.

Exit codes:
  0  heartbeat.json exists, now - poll_finished_at <= FFIS_HEALTH_MAX_AGE_S
     (default 2*interval + deadline = 540 s), and no poll in flight is older
     than FFIS_POLL_DEADLINE_S.
  1  heartbeat missing, unreadable, stale, no finished poll yet, or a poll in
     flight past the deadline.

Prints exactly one JSON line (ages and status only — never secrets).

    python ffis_ingest_health.py            # compose healthcheck
    python -m ffis_ingest_health --alert    # host watch (phase 2): also send an alert
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, Optional, Tuple

import ffis_ingest_state as st


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def evaluate(now: Optional[float] = None, sdir: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
    now = time.time() if now is None else now
    sdir = sdir or st.state_dir()
    deadline = _int_env("FFIS_POLL_DEADLINE_S", 300)
    interval = _int_env("FFIS_WATCH_INTERVAL_S", 120)
    max_age = _int_env("FFIS_HEALTH_MAX_AGE_S", 2 * interval + deadline)
    path = os.path.join(sdir, "heartbeat.json")
    out: Dict[str, Any] = {"check": "ffis-ingest-health", "max_age_s": max_age,
                           "deadline_s": deadline}
    try:
        with open(path) as f:
            hb = json.load(f)
        if not isinstance(hb, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        out.update(status="unhealthy", reason="heartbeat_missing")
        return 1, out
    except (OSError, ValueError):
        out.update(status="unhealthy", reason="heartbeat_unreadable")
        return 1, out
    started = st.parse_iso(hb.get("poll_started_at"))
    finished = st.parse_iso(hb.get("poll_finished_at"))
    last_ok = st.parse_iso(hb.get("last_ok_at"))

    def age(ts):
        return None if ts is None else round(now - ts, 1)
    out.update(poll_started_age_s=age(started), poll_finished_age_s=age(finished),
               last_ok_age_s=age(last_ok), last_result=hb.get("last_result"),
               stage=hb.get("stage"), found=hb.get("found"))
    in_flight = started is not None and (finished is None or started > finished)
    out["in_flight"] = in_flight
    if in_flight and now - started > deadline:
        out.update(status="unhealthy", reason="poll_in_flight_past_deadline")
        return 1, out
    if finished is None:
        out.update(status="unhealthy", reason="no_finished_poll")
        return 1, out
    if now - finished > max_age:
        out.update(status="unhealthy", reason="heartbeat_stale")
        return 1, out
    out.update(status="healthy", reason="ok")
    return 0, out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="FFIS ingest health check (file-based).")
    ap.add_argument("--alert", action="store_true",
                    help="Also send an alert (FFIS_ALERT_TO via the ingest SMTP creds).")
    args = ap.parse_args(argv)
    code, info = evaluate()
    if args.alert:
        info["alert"] = st.send_alert("host watch: ingest health " + info.get("status", "?"),
                                      {k: info.get(k) for k in ("status", "reason", "stage",
                                                                "poll_finished_age_s",
                                                                "last_ok_age_s")})
    print(json.dumps(info, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
