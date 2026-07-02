"""
ffis_worker_status.py
=====================
Streamlit panel showing the email-ingest worker's health, read from the
Fly.io Machines API. Renders machine state, restart policy, and recent
events for every machine in the 'ingest' process group.

Requires two Fly secrets on the APP machine:
    FLY_API_TOKEN   - a deploy/org token:  fly tokens create deploy -a ffis-scrubber
    FLY_APP_NAME    - e.g. ffis-scrubber   (optional; defaults to ffis-scrubber)

Reads only. Never mutates machines.
"""
from __future__ import annotations
import os
import datetime as _dt
from typing import Any, Dict, List, Optional

import requests
import streamlit as st

_FLY_API = "https://api.machines.dev/v1"


def _token() -> str:
    return os.environ.get("FLY_API_TOKEN", "").strip()


def _app() -> str:
    return os.environ.get("FLY_APP_NAME", "ffis-scrubber").strip()


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


def fetch_machines() -> List[Dict[str, Any]]:
    """Return all machines for the app. Raises on HTTP error."""
    url = f"{_FLY_API}/apps/{_app()}/machines"
    resp = requests.get(url, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def _process_group(m: Dict[str, Any]) -> str:
    return (m.get("config", {})
             .get("metadata", {})
             .get("fly_process_group", "?"))


def _fmt_ts(ts: Optional[str]) -> str:
    if not ts:
        return "—"
    try:
        d = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return d.strftime("%Y-%m-%d %H:%M:%SZ")
    except (ValueError, AttributeError):
        return ts

def render_worker_status_compact(process_group: str = "ingest") -> None:
    """
    Sidebar-sized worker health: one status line + a details expander.
    Reuses fetch_machines() and the module helpers. Safe to call inside
    `with st.sidebar:` — every widget key is namespaced to avoid collisions.
    """
    if not _token():
        st.caption("📡 Worker: ⚪ status unavailable")
        with st.expander("Enable monitoring"):
            st.caption(
                "Set `FLY_API_TOKEN` as a secret on this app to show live "
                "worker status. Create one with "
                "`fly tokens create deploy -a ffis-scrubber`."
            )
        return

    try:
        machines = fetch_machines()
    except requests.HTTPError as e:
        st.caption(f"📡 Worker: 🔴 API error ({e.response.status_code})")
        return
    except requests.RequestException:
        st.caption("📡 Worker: 🔴 unreachable")
        return

    workers = [m for m in machines if _process_group(m) == process_group]
    if not workers:
        st.caption(f"📡 Worker: ⚪ no '{process_group}' machine")
        return

    # Pick the most representative machine: prefer a started one, else the first.
    started = [m for m in workers if m.get("state") == "started"]
    primary = started[0] if started else workers[0]
    state = primary.get("state", "unknown")
    icon = {"started": "🟢", "stopped": "🔴", "starting": "🟡",
            "stopping": "🟡", "destroying": "⚫"}.get(state, "⚪")

    # One-line status; if there are multiple workers, note the count.
    suffix = f" ×{len(workers)}" if len(workers) > 1 else ""
    st.caption(f"📡 Email worker: {icon} {state}{suffix}")

    with st.expander("Worker details"):
        for m in workers:
            mstate = m.get("state", "unknown")
            micon = {"started": "🟢", "stopped": "🔴", "starting": "🟡",
                     "stopping": "🟡", "destroying": "⚫"}.get(mstate, "⚪")
            restart = m.get("config", {}).get("restart", {}).get("policy", "?")
            st.markdown(
                f"{micon} **{mstate}** · `{m.get('region','?')}` · "
                f"restart: {restart}"
            )
            st.caption(f"`{m.get('id','?')}` · updated {_fmt_ts(m.get('updated_at'))}")
            events = m.get("events", []) or []
            if events:
                latest = events[0]
                st.caption(f"last event: {latest.get('type','?')} "
                           f"@ {_fmt_ts(latest.get('timestamp'))}")
        if st.button("🔄 Refresh", key="ffis_worker_refresh_compact"):
            st.rerun()

def render_worker_status(process_group: str = "ingest") -> None:
    """Streamlit panel: state of every machine in the given process group."""
    st.subheader("📡 Email Ingest Worker")

    if not _token():
        st.warning(
            "Worker status unavailable: `FLY_API_TOKEN` is not set on this app. "
            "Create one with `fly tokens create deploy -a ffis-scrubber` and set it "
            "as a secret to enable live monitoring."
        )
        return

    try:
        machines = fetch_machines()
    except requests.HTTPError as e:
        st.error(f"Fly API error: {e.response.status_code} — check FLY_API_TOKEN scope.")
        return
    except requests.RequestException as e:
        st.error(f"Could not reach Fly API: {e}")
        return

    workers = [m for m in machines if _process_group(m) == process_group]
    if not workers:
        st.info(f"No machines found in the '{process_group}' process group.")
        return

    for m in workers:
        state = m.get("state", "unknown")
        icon = {"started": "🟢", "stopped": "🔴", "starting": "🟡",
                "stopping": "🟡", "destroying": "⚫"}.get(state, "⚪")
        restart = (m.get("config", {}).get("restart", {}).get("policy", "?"))
        region = m.get("region", "?")
        mid = m.get("id", "?")

        cols = st.columns([1, 2, 2, 2])
        cols[0].markdown(f"### {icon}")
        cols[1].metric("State", state)
        cols[2].metric("Region", region)
        cols[3].metric("Restart", restart)
        st.caption(f"Machine `{mid}` · updated {_fmt_ts(m.get('updated_at'))}")

        events = m.get("events", []) or []
        if events:
            with st.expander("Recent events", expanded=(state != "started")):
                for ev in events[:8]:
                    st.text(f"{_fmt_ts(ev.get('timestamp'))}  "
                            f"{ev.get('type','?'):10} {ev.get('source','')}")
        st.divider()

    if st.button("🔄 Refresh worker status"):
        st.rerun()