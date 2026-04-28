"""
ffis_sf_org.py
==============
Salesforce CLI org management for the Flat File Scrubber.

Provides a Streamlit panel + helper functions that wrap the Salesforce CLI
(`sf` / `sfdx`) so a Data Steward can:

    * Detect whether the CLI is installed and what version it is
    * Authorize an existing Sandbox, Dev, or Production org via web login
    * Create a brand-new Scratch Org from a project directory + definition file
    * List all currently authorized orgs
    * Pull org details (instance URL, access token, expiration, status)
    * Open any authorized org in the browser
    * Promote an authorized org's instance URL straight into the FFIS API
      Export tab so the next load goes there with no copy/paste

Design philosophy:
    * No external Python deps beyond what FFIS already pins (subprocess + json
      from stdlib, streamlit, pandas).
    * All shell commands are emitted with `--json` and parsed defensively;
      legacy `sfdx` JSON envelopes are normalized to the modern `sf` shape.
    * Long-running commands are streamed with a spinner and a captured-output
      panel so the user can see what the CLI is doing.
    * Every successful auth/create writes its alias into st.session_state under
      `sf_authorized_orgs` so other tabs (API Export) can offer it as a target.

Author : Matthew Kelly  (extends the FFIS / SFCOE Data Steward Toolkit)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st


# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

# Salesforce CLI executable candidates, in preference order.
# `sf` is the modern unified CLI (v2+); `sfdx` is the legacy one.
SF_CLI_CANDIDATES = ("sf", "sfdx")

# Canonical login URLs for the three auth flows the panel exposes.
LOGIN_URLS = {
    "Production / Developer Edition": "https://login.salesforce.com",
    "Sandbox": "https://test.salesforce.com",
    "Custom My Domain": "",  # user supplies
}

# Default scratch-org definition file shipped inside SFDX projects.
DEFAULT_SCRATCH_DEF = "config/project-scratch-def.json"

# Reasonable upper bounds for the scratch-org duration field (CLI accepts 1–30).
SCRATCH_DURATION_MIN = 1
SCRATCH_DURATION_MAX = 30
SCRATCH_DURATION_DEFAULT = 7

# Subprocess timeouts (seconds).  Org creation can be slow; auth is usually fast.
TIMEOUT_FAST = 30        # version checks, list, display
TIMEOUT_AUTH = 300       # web login (waits for the browser flow)
TIMEOUT_SCRATCH = 900    # scratch-org creation (15 min upper bound)


# ──────────────────────────────────────────────────────────────────────────────
# DATA TYPES
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CliResult:
    """Normalized result of a CLI invocation."""
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    parsed: dict | None = None    # JSON body if --json was used and parse worked
    cmd: list[str] = field(default_factory=list)
    duration_sec: float = 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        # truncate huge stdout in the dict view
        if len(d["stdout"]) > 4000:
            d["stdout"] = d["stdout"][:4000] + "\n…(truncated)"
        return d


@dataclass
class OrgRecord:
    """One authorized Salesforce org as known to the local CLI keychain."""
    alias: str
    username: str
    instance_url: str
    org_id: str
    is_default: bool = False
    is_dev_hub: bool = False
    is_scratch: bool = False
    is_sandbox: bool = False
    expiration_date: str | None = None
    status: str = "Active"
    access_token: str | None = None   # only set after explicit display

    def label(self) -> str:
        bits = [self.alias or self.username]
        tags = []
        if self.is_default: tags.append("default")
        if self.is_dev_hub: tags.append("DevHub")
        if self.is_scratch: tags.append("scratch")
        if self.is_sandbox: tags.append("sandbox")
        if tags:
            bits.append(f"({', '.join(tags)})")
        return " ".join(bits)


# ──────────────────────────────────────────────────────────────────────────────
# CLI DETECTION & EXECUTION
# ──────────────────────────────────────────────────────────────────────────────

def find_sf_cli() -> str | None:
    """Return the first available Salesforce CLI executable on PATH, or None."""
    for candidate in SF_CLI_CANDIDATES:
        if shutil.which(candidate):
            return candidate
    return None


def get_cli_version(cli: str | None = None) -> str | None:
    """Return the CLI version string, or None if the CLI isn't reachable."""
    cli = cli or find_sf_cli()
    if not cli:
        return None
    try:
        proc = subprocess.run(
            [cli, "--version"],
            capture_output=True, text=True, timeout=TIMEOUT_FAST,
        )
        if proc.returncode == 0:
            return proc.stdout.strip().splitlines()[0] if proc.stdout else None
    except Exception:
        return None
    return None


def run_sf(args: list[str], timeout: int = TIMEOUT_FAST,
           cli: str | None = None, env_extra: dict | None = None) -> CliResult:
    """
    Invoke the Salesforce CLI and return a normalized CliResult.

    `args` is the argv tail AFTER the executable, e.g.
        ["org", "list", "--json"]

    The function always uses `--json` mode if it appears in `args`.
    """
    cli = cli or find_sf_cli()
    if not cli:
        return CliResult(
            ok=False, returncode=-1,
            stdout="", stderr="Salesforce CLI (sf or sfdx) not found on PATH.",
            cmd=args,
        )

    cmd = [cli] + list(args)
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as e:
        return CliResult(
            ok=False, returncode=-1,
            stdout=e.stdout or "",
            stderr=f"Command timed out after {timeout}s.",
            cmd=cmd, duration_sec=time.time() - t0,
        )
    except Exception as e:
        return CliResult(
            ok=False, returncode=-1, stdout="", stderr=str(e),
            cmd=cmd, duration_sec=time.time() - t0,
        )

    elapsed = time.time() - t0
    parsed = None
    if "--json" in args and proc.stdout:
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError:
            parsed = None

    # `sf --json` returns status 0 on success; non-zero on error.
    # `sfdx --json` is similar but legacy envelopes differ.
    ok = proc.returncode == 0
    if parsed and "status" in parsed:
        # sfdx legacy: status is 0 for success
        ok = ok and (parsed.get("status") == 0)

    return CliResult(
        ok=ok, returncode=proc.returncode,
        stdout=proc.stdout, stderr=proc.stderr,
        parsed=parsed, cmd=cmd, duration_sec=elapsed,
    )


# ──────────────────────────────────────────────────────────────────────────────
# JSON ENVELOPE NORMALIZATION
# ──────────────────────────────────────────────────────────────────────────────

def _envelope_payload(parsed: dict | None) -> Any:
    """
    Return the payload from a CLI JSON envelope.

    `sf --json`  → {"status": 0, "result": {...}}
    `sfdx --json`→ {"status": 0, "result": {...}}    (same)
    Errors:       → {"status": 1, "name": "...", "message": "..."}
    """
    if not parsed:
        return None
    if "result" in parsed:
        return parsed["result"]
    return parsed


def _normalize_org_row(row: dict) -> OrgRecord:
    """Coerce one entry from `org list --json` into an OrgRecord."""
    return OrgRecord(
        alias=row.get("alias") or row.get("aliases") or "",
        username=row.get("username", ""),
        instance_url=row.get("instanceUrl") or row.get("loginUrl") or "",
        org_id=row.get("orgId") or row.get("orgID") or "",
        is_default=bool(row.get("isDefaultUsername") or row.get("isDefault")),
        is_dev_hub=bool(row.get("isDevHub")),
        is_scratch=bool(row.get("isScratch") or row.get("isScratchOrg")),
        is_sandbox=bool(row.get("isSandbox")),
        expiration_date=row.get("expirationDate"),
        status=row.get("status", "Active"),
        access_token=row.get("accessToken"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# ORG OPERATIONS
# ──────────────────────────────────────────────────────────────────────────────

def list_orgs() -> tuple[list[OrgRecord], CliResult]:
    """Return all authorized orgs known to the CLI."""
    res = run_sf(["org", "list", "--json"], timeout=TIMEOUT_FAST)
    orgs: list[OrgRecord] = []
    if not res.ok:
        return orgs, res

    payload = _envelope_payload(res.parsed) or {}
    # The result has buckets: nonScratchOrgs, scratchOrgs, devHubs, sandboxes
    seen_ids: set[str] = set()
    for bucket in ("nonScratchOrgs", "scratchOrgs", "devHubs",
                   "sandboxes", "other"):
        for row in (payload.get(bucket) or []):
            org = _normalize_org_row(row)
            if org.org_id and org.org_id in seen_ids:
                continue
            if bucket == "scratchOrgs":
                org.is_scratch = True
            if bucket == "sandboxes":
                org.is_sandbox = True
            if bucket == "devHubs":
                org.is_dev_hub = True
            seen_ids.add(org.org_id)
            orgs.append(org)
    return orgs, res


def display_org(alias_or_username: str) -> tuple[OrgRecord | None, CliResult]:
    """Return full details (incl. access token) for one authorized org."""
    res = run_sf(
        ["org", "display", "--target-org", alias_or_username, "--json"],
        timeout=TIMEOUT_FAST,
    )
    if not res.ok:
        return None, res
    payload = _envelope_payload(res.parsed) or {}
    return _normalize_org_row(payload), res


def login_web(login_url: str, alias: str,
              set_default: bool = False) -> CliResult:
    """
    Authorize a Production or Sandbox org via the OAuth web flow.

    Opens the user's default browser. Blocks until the flow completes or times
    out.  The CLI handles the OAuth callback on a local loopback port.
    """
    args = ["org", "login", "web",
            "--instance-url", login_url,
            "--alias", alias,
            "--json"]
    if set_default:
        args.append("--set-default")
    return run_sf(args, timeout=TIMEOUT_AUTH)


def logout_org(alias_or_username: str) -> CliResult:
    """Revoke local credentials for an authorized org."""
    return run_sf(
        ["org", "logout", "--target-org", alias_or_username,
         "--no-prompt", "--json"],
        timeout=TIMEOUT_FAST,
    )


def create_scratch_org(project_dir: str, definition_file: str,
                       alias: str, dev_hub_alias: str,
                       duration_days: int = SCRATCH_DURATION_DEFAULT,
                       set_default: bool = False) -> CliResult:
    """
    Create a scratch org from a definition file.

    Equivalent CLI:
        sf org create scratch \\
            --definition-file <definition_file> \\
            --alias <alias> \\
            --target-dev-hub <dev_hub_alias> \\
            --duration-days <N> \\
            [--set-default] \\
            --json

    Must be run with `cwd = project_dir` because the CLI looks for
    `sfdx-project.json` in the current working directory.
    """
    duration = max(SCRATCH_DURATION_MIN,
                   min(SCRATCH_DURATION_MAX, int(duration_days)))
    args = [
        "org", "create", "scratch",
        "--definition-file", definition_file,
        "--alias", alias,
        "--target-dev-hub", dev_hub_alias,
        "--duration-days", str(duration),
        "--json",
    ]
    if set_default:
        args.append("--set-default")

    cli = find_sf_cli()
    if not cli:
        return CliResult(False, -1, "", "Salesforce CLI not found.", cmd=args)

    cmd = [cli] + args
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=project_dir, timeout=TIMEOUT_SCRATCH,
        )
    except subprocess.TimeoutExpired as e:
        return CliResult(
            ok=False, returncode=-1, stdout=e.stdout or "",
            stderr=f"Scratch-org creation timed out after {TIMEOUT_SCRATCH}s.",
            cmd=cmd, duration_sec=time.time() - t0,
        )
    except Exception as e:
        return CliResult(False, -1, "", str(e), cmd=cmd,
                         duration_sec=time.time() - t0)

    parsed = None
    try:
        parsed = json.loads(proc.stdout) if proc.stdout else None
    except json.JSONDecodeError:
        parsed = None
    ok = proc.returncode == 0
    if parsed and "status" in parsed:
        ok = ok and (parsed.get("status") == 0)
    return CliResult(
        ok=ok, returncode=proc.returncode,
        stdout=proc.stdout, stderr=proc.stderr,
        parsed=parsed, cmd=cmd, duration_sec=time.time() - t0,
    )


def open_org(alias_or_username: str) -> CliResult:
    """Open the authorized org in the user's default browser."""
    return run_sf(
        ["org", "open", "--target-org", alias_or_username, "--json"],
        timeout=TIMEOUT_FAST,
    )


# ──────────────────────────────────────────────────────────────────────────────
# SESSION-STATE INTEGRATION
# ──────────────────────────────────────────────────────────────────────────────

def _ss_init():
    """Ensure the keys this panel reads/writes exist on st.session_state."""
    st.session_state.setdefault("sf_authorized_orgs", [])   # list[OrgRecord]
    st.session_state.setdefault("sf_active_org", None)      # OrgRecord | None
    st.session_state.setdefault("sf_cli_log", [])           # list[CliResult]


def _ss_record_call(res: CliResult):
    """Append a CLI call to the session-state log (capped at 25 entries)."""
    st.session_state.sf_cli_log.append(res)
    if len(st.session_state.sf_cli_log) > 25:
        st.session_state.sf_cli_log.pop(0)


def _ss_replace_orgs(orgs: list[OrgRecord]):
    """Refresh the authorized-org list and keep the active selection if valid."""
    st.session_state.sf_authorized_orgs = orgs
    active = st.session_state.sf_active_org
    if active and not any(o.org_id == active.org_id for o in orgs):
        st.session_state.sf_active_org = None


def get_active_org() -> OrgRecord | None:
    """Public accessor for other FFIS tabs (e.g. API Export)."""
    _ss_init()
    return st.session_state.sf_active_org


# ──────────────────────────────────────────────────────────────────────────────
# UI HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _orgs_dataframe(orgs: list[OrgRecord]) -> pd.DataFrame:
    rows = []
    for o in orgs:
        rows.append({
            "Alias":      o.alias or "—",
            "Username":   o.username,
            "Instance":   o.instance_url,
            "Org ID":     o.org_id,
            "Type":       ("Scratch" if o.is_scratch
                           else "Sandbox" if o.is_sandbox
                           else "DevHub" if o.is_dev_hub
                           else "Production / DE"),
            "Default?":   "✓" if o.is_default else "",
            "Expires":    o.expiration_date or "—",
            "Status":     o.status,
        })
    return pd.DataFrame(rows)


def _show_cli_log_panel():
    """Expandable panel that shows the last few CLI invocations."""
    log = st.session_state.get("sf_cli_log", [])
    if not log:
        return
    with st.expander(f"🪵 Salesforce CLI call log ({len(log)} entries)",
                     expanded=False):
        for i, res in enumerate(reversed(log), 1):
            tag = "✅" if res.ok else "❌"
            st.markdown(
                f"**{tag} #{len(log) - i + 1} — `{' '.join(res.cmd[:6])}…` "
                f"({res.duration_sec:.1f}s, rc={res.returncode})**"
            )
            if res.stdout:
                with st.expander("stdout", expanded=False):
                    st.code(res.stdout[:4000] or "(empty)", language="json")
            if res.stderr:
                with st.expander("stderr", expanded=False):
                    st.code(res.stderr[:4000], language="text")


# ──────────────────────────────────────────────────────────────────────────────
# MAIN PANEL — ENTRY POINT FOR THE FFIS TAB
# ──────────────────────────────────────────────────────────────────────────────

def render_org_panel():
    """
    Render the full Salesforce Org panel.

    Drop this into a Streamlit tab body in flat_file_scrubber.py:

        with tabs[NEW_INDEX]:
            from ffis_sf_org import render_org_panel
            render_org_panel()
    """
    _ss_init()

    st.markdown(
        '<div class="section-header">Salesforce Org Management</div>',
        unsafe_allow_html=True,
    )

    # ── CLI detection ────────────────────────────────────────────────────────
    cli_path = find_sf_cli()
    cli_ver = get_cli_version(cli_path) if cli_path else None

    if not cli_path:
        st.markdown(
            '<div class="warn-box">⚠️ <strong>Salesforce CLI not found.</strong> '
            'Install it before using this tab. On macOS: <code>brew install '
            'salesforcedx</code>. On Windows: download the installer from '
            '<a href="https://developer.salesforce.com/tools/salesforcecli" '
            'target="_blank" style="color:#63b3ed">'
            'developer.salesforce.com/tools/salesforcecli</a>. '
            'After installing, restart this Streamlit server so the new PATH '
            'is picked up.</div>',
            unsafe_allow_html=True,
        )
        return

    st.markdown(
        f'<div class="info-box">✅ Salesforce CLI detected: '
        f'<code>{cli_path}</code> &nbsp;·&nbsp; <code>{cli_ver or "version unknown"}</code></div>',
        unsafe_allow_html=True,
    )

    # ── Sub-tabs for the four flows ─────────────────────────────────────────
    sub = st.tabs([
        "🔌 Authorize Org (Web Login)",
        "🧪 Create Scratch Org",
        "📋 Authorized Orgs",
        "⚙️ Active Org for Export",
    ])

    # ─── Sub-tab 0: Web Login ───────────────────────────────────────────────
    with sub[0]:
        st.subheader("Authorize an Existing Org")
        st.markdown(
            '<div class="info-box">Opens your default browser to '
            'login.salesforce.com (or your sandbox / My Domain). After '
            'completing the OAuth flow the CLI stores the credentials locally '
            'and the org becomes available in <strong>Authorized Orgs</strong> '
            'and <strong>Active Org for Export</strong>.</div>',
            unsafe_allow_html=True,
        )
        col1, col2 = st.columns(2)
        env_choice = col1.selectbox(
            "Environment",
            list(LOGIN_URLS.keys()),
            key="sforg_env_choice",
        )
        custom_url = ""
        if env_choice == "Custom My Domain":
            custom_url = col2.text_input(
                "My Domain URL",
                placeholder="https://mycompany.my.salesforce.com",
                key="sforg_custom_url",
            )
        else:
            col2.text_input(
                "Login URL (read-only)",
                value=LOGIN_URLS[env_choice],
                disabled=True,
                key="sforg_login_url_ro",
            )

        col3, col4 = st.columns(2)
        alias = col3.text_input(
            "Alias for this org",
            value="",
            placeholder="e.g. acme-prod or fsc-sandbox",
            key="sforg_alias",
        )
        set_default = col4.checkbox(
            "Set as default org",
            value=False,
            key="sforg_set_default",
        )

        if st.button("🌐 Launch Web Login", key="sforg_login_btn",
                     type="primary"):
            if not alias:
                st.error("Enter an alias before logging in.")
            else:
                login_url = (custom_url.strip()
                             if env_choice == "Custom My Domain"
                             else LOGIN_URLS[env_choice])
                if not login_url:
                    st.error("Enter a valid My Domain URL.")
                else:
                    with st.spinner(
                        "Opening browser for OAuth login… "
                        "complete the flow there, then return here."
                    ):
                        res = login_web(login_url, alias, set_default)
                    _ss_record_call(res)
                    if res.ok:
                        st.success(
                            f"Authorized org with alias **{alias}**. "
                            f"Refresh the **Authorized Orgs** sub-tab."
                        )
                    else:
                        st.error(
                            f"Login failed (rc={res.returncode}). "
                            f"See the call log below for details."
                        )

    # ─── Sub-tab 1: Scratch Org ─────────────────────────────────────────────
    with sub[1]:
        st.subheader("Create a Scratch Org")
        st.markdown(
            '<div class="info-box">Scratch orgs are short-lived, fully '
            'configurable Salesforce environments created from a Dev Hub. '
            'You need: (1) a local SFDX project directory containing '
            '<code>sfdx-project.json</code>, (2) a definition file (typically '
            f'<code>{DEFAULT_SCRATCH_DEF}</code>), and (3) an authorized Dev '
            'Hub org. If you don\'t have a Dev Hub yet, authorize a Production '
            'or Developer Edition org via the previous sub-tab and enable Dev '
            'Hub in <em>Setup → Dev Hub</em>.</div>',
            unsafe_allow_html=True,
        )

        # ─ QUICK CREATE SECTION ──────────────────────────────────────────────
        st.markdown(
            '<div style="background-color: #1a3a2e; border-left: 4px solid #4ade80; padding: 12px; margin: 12px 0; border-radius: 4px;">'
            '<strong style="color: #86efac;">⚡ Quick Create</strong> — Two ways to spawn a new org'
            '</div>',
            unsafe_allow_html=True,
        )

        qc_type = st.radio(
            "Choose org type:",
            ["🏗️ Development Org (No Dependencies)", "🧪 Scratch Org (Requires Dev Hub)"],
            horizontal=True,
            key="sforg_qc_type",
        )

        # ─ OPTION 1: Development Org ─────────────────────────────────────────
        if "Development" in qc_type:
            st.markdown(
                '<div class="info-box">Development Edition orgs are <strong>permanent, free</strong> Salesforce environments. '
                'No Dev Hub required. Sign up at developer.salesforce.com, then authorize the credentials below.</div>',
                unsafe_allow_html=True,
            )

            dev_cols = st.columns([2, 1])
            dev_alias = dev_cols[0].text_input(
                "Org Alias",
                value="",
                placeholder="e.g. dev-org-1 or my-dev",
                key="sforg_dev_alias",
                help="Name for your new Development org"
            )

            if dev_cols[1].button("📝 Sign Up", key="sforg_dev_signup_btn", help="Opens developer.salesforce.com"):
                import webbrowser
                webbrowser.open("https://developer.salesforce.com/signup")
                st.info("Opening signup page in browser… Complete the form to create your org.")

            if st.button(
                "✅ Authorize Development Org",
                key="sforg_dev_authorize_btn",
                type="primary",
                use_container_width=True,
                help="Opens login flow for your new Development org"
            ):
                if not dev_alias:
                    st.error("Enter an alias first.")
                else:
                    # Re-use the web login flow for Development Edition
                    with st.spinner(f"Opening browser for login to **{dev_alias}**…"):
                        res = login_web(
                            "https://login.salesforce.com",  # Development orgs use standard login
                            dev_alias,
                            set_default=True
                        )
                    _ss_record_call(res)
                    if res.ok:
                        st.success(
                            f"✅ Authorized **{dev_alias}**. "
                            f"Your Development org is now ready to use in the Export tab."
                        )
                        # Refresh authorized list
                        refreshed, lr = list_orgs()
                        _ss_record_call(lr)
                        _ss_replace_orgs(refreshed)
                        st.balloons()
                    else:
                        st.error(f"Authorization failed (rc={res.returncode}). See the log below.")

        # ─ OPTION 2: Scratch Org ──────────────────────────────────────────────
        else:
            st.markdown(
                '<div class="info-box">Scratch orgs are <strong>temporary, auto-delete</strong> (up to 30 days). '
                'Requires an authorized Dev Hub org. After authorization, they\'ll appear in the full form below.</div>',
                unsafe_allow_html=True,
            )

            qc_col1, qc_col2 = st.columns([1, 1])
            quick_alias = qc_col1.text_input(
                "Org Alias",
                value="",
                placeholder="e.g. scratch-1",
                key="sforg_quick_alias",
                help="Name for your new scratch org"
            )

            # Refresh and get current org list for quick create
            if st.button("🔄 Refresh Dev Hubs", key="sforg_quick_refresh"):
                orgs, res = list_orgs()
                _ss_record_call(res)
                _ss_replace_orgs(orgs)

            orgs = st.session_state.sf_authorized_orgs or []
            dev_hub_options = [o for o in orgs if o.is_dev_hub]

            quick_dev_hub = None
            if dev_hub_options:
                qc_col2.success(f"✅ Found {len(dev_hub_options)} Dev Hub(s)")
                quick_dev_hub = dev_hub_options[0]
            else:
                qc_col2.warning("⚠️ No Dev Hub found")
                qc_col2.caption("Authorize one in the tab above first")

            # Get the project directory (use current working dir or sensible default)
            quick_project_dir = os.getcwd()  # Use current working directory

            # Quick create button
            if st.button(
                "🚀 Create Scratch Org",
                key="sforg_quick_create_btn",
                type="primary",
                use_container_width=True,
            ):
                errs = []
                if not quick_alias:
                    errs.append("Enter an org alias.")
                elif quick_dev_hub is None:
                    errs.append("No Dev Hub available. Authorize one first in the tab above.")
                elif not Path(quick_project_dir).is_dir():
                    errs.append(f"Project directory not found: {quick_project_dir}")
                elif not (Path(quick_project_dir) / "sfdx-project.json").is_file():
                    errs.append(f"Directory {quick_project_dir} does not contain sfdx-project.json")
                elif not (Path(quick_project_dir) / DEFAULT_SCRATCH_DEF).is_file():
                    errs.append(f"Definition file not found: {DEFAULT_SCRATCH_DEF}")

                if errs:
                    for e in errs:
                        st.error(e)
                else:
                    with st.spinner(
                        f"🚀 Creating **{quick_alias}** (this takes 1–5 minutes)…"
                    ):
                        res = create_scratch_org(
                            project_dir=quick_project_dir,
                            definition_file=DEFAULT_SCRATCH_DEF,
                            alias=quick_alias,
                            dev_hub_alias=quick_dev_hub.alias or quick_dev_hub.username,
                            duration_days=SCRATCH_DURATION_DEFAULT,
                            set_default=True,
                        )
                    _ss_record_call(res)
                    if res.ok:
                        payload = _envelope_payload(res.parsed) or {}
                        org_id = payload.get("orgId") or payload.get("id") or "?"
                        st.success(
                            f"✅ Scratch org **{quick_alias}** created! "
                            f"(ID: `{org_id}`, expires in {SCRATCH_DURATION_DEFAULT} days)"
                        )
                        # Auto-refresh authorized list
                        refreshed, lr = list_orgs()
                        _ss_record_call(lr)
                        _ss_replace_orgs(refreshed)
                        st.balloons()
                    else:
                        msg = ""
                        if res.parsed:
                            msg = (res.parsed.get("message") or res.parsed.get("name") or "")
                        st.error(f"Creation failed (rc={res.returncode}). {msg}")

        st.divider()

        # Refresh the authorized list so the dev hub picker is current.
        if st.button("🔄 Refresh authorized orgs", key="sforg_refresh_for_scratch"):
            orgs, res = list_orgs()
            _ss_record_call(res)
            _ss_replace_orgs(orgs)

        orgs = st.session_state.sf_authorized_orgs or []
        dev_hub_options = [o for o in orgs if o.is_dev_hub]
        all_options = orgs   # let the user pick anything; CLI will validate

        col1, col2 = st.columns(2)
        project_dir = col1.text_input(
            "SFDX Project Directory (absolute path)",
            placeholder="/Users/me/projects/fsc-insurance-sfdx",
            key="sforg_project_dir",
        )
        def_file = col2.text_input(
            "Scratch Definition File (relative to project dir)",
            value=DEFAULT_SCRATCH_DEF,
            key="sforg_def_file",
        )

        col3, col4, col5 = st.columns(3)
        scratch_alias = col3.text_input(
            "Scratch Org Alias",
            value="",
            placeholder="e.g. fsc-scratch-1",
            key="sforg_scratch_alias",
        )
        if all_options:
            labels = [o.label() for o in all_options]
            preferred = [o.label() for o in dev_hub_options] or labels
            default_idx = labels.index(preferred[0]) if preferred else 0
            dh_label = col4.selectbox(
                "Dev Hub Org",
                options=labels,
                index=default_idx,
                key="sforg_dh_choice",
            )
            dev_hub = all_options[labels.index(dh_label)]
        else:
            col4.info("Authorize a Dev Hub first (previous sub-tab).")
            dev_hub = None

        duration = col5.number_input(
            "Duration (days)",
            min_value=SCRATCH_DURATION_MIN,
            max_value=SCRATCH_DURATION_MAX,
            value=SCRATCH_DURATION_DEFAULT,
            step=1,
            key="sforg_duration",
        )
        set_default_scratch = st.checkbox(
            "Set new scratch org as default",
            value=False,
            key="sforg_scratch_default",
        )

        if st.button("🧪 Create Scratch Org", key="sforg_create_btn",
                     type="primary"):
            errs = []
            if not project_dir:
                errs.append("Project directory is required.")
            elif not Path(project_dir).is_dir():
                errs.append(f"Directory not found: `{project_dir}`")
            elif not (Path(project_dir) / "sfdx-project.json").is_file():
                errs.append(
                    f"`{project_dir}` does not contain `sfdx-project.json`."
                )
            elif not (Path(project_dir) / def_file).is_file():
                errs.append(f"Definition file not found: `{def_file}`")
            if not scratch_alias:
                errs.append("Scratch org alias is required.")
            if dev_hub is None:
                errs.append("A Dev Hub org must be authorized first.")
            if errs:
                for e in errs:
                    st.error(e)
            else:
                with st.spinner(
                    f"Creating scratch org **{scratch_alias}** "
                    f"(this can take 1–5 minutes)…"
                ):
                    res = create_scratch_org(
                        project_dir=project_dir,
                        definition_file=def_file,
                        alias=scratch_alias,
                        dev_hub_alias=dev_hub.alias or dev_hub.username,
                        duration_days=int(duration),
                        set_default=set_default_scratch,
                    )
                _ss_record_call(res)
                if res.ok:
                    payload = _envelope_payload(res.parsed) or {}
                    org_id = payload.get("orgId") or payload.get("id") or "?"
                    st.success(
                        f"Scratch org **{scratch_alias}** created "
                        f"(Org ID `{org_id}`, expires in {duration} days)."
                    )
                    # Auto-refresh authorized list
                    refreshed, lr = list_orgs()
                    _ss_record_call(lr)
                    _ss_replace_orgs(refreshed)
                else:
                    msg = ""
                    if res.parsed:
                        msg = (res.parsed.get("message")
                               or res.parsed.get("name") or "")
                    st.error(
                        f"Scratch-org creation failed "
                        f"(rc={res.returncode}). {msg}"
                    )

    # ─── Sub-tab 2: Authorized Orgs ─────────────────────────────────────────
    with sub[2]:
        st.subheader("Authorized Orgs on This Machine")
        col_a, col_b = st.columns([1, 4])
        if col_a.button("🔄 Refresh", key="sforg_refresh_listing"):
            orgs, res = list_orgs()
            _ss_record_call(res)
            _ss_replace_orgs(orgs)
            st.rerun()

        orgs = st.session_state.sf_authorized_orgs or []
        if not orgs:
            st.info("No authorized orgs yet — click **Refresh** above, or "
                    "authorize an org in the previous sub-tabs.")
        else:
            df = _orgs_dataframe(orgs)
            st.dataframe(df, use_container_width=True, hide_index=True)

            st.divider()
            labels = [o.label() for o in orgs]
            target_label = st.selectbox(
                "Inspect / open / logout an org",
                options=labels,
                key="sforg_target_choice",
            )
            target = orgs[labels.index(target_label)]
            ref = target.alias or target.username

            cols = st.columns(3)
            if cols[0].button("🔍 Show details", key="sforg_show_btn"):
                with st.spinner(f"Fetching details for {ref}…"):
                    full, res = display_org(ref)
                _ss_record_call(res)
                if res.ok and full:
                    safe = asdict(full)
                    if safe.get("access_token"):
                        # mask all but last 6
                        tok = safe["access_token"]
                        safe["access_token"] = (
                            "•" * max(0, len(tok) - 6) + tok[-6:]
                        )
                    st.json(safe)
                else:
                    st.error("Could not fetch org details.")

            if cols[1].button("🌐 Open in browser", key="sforg_open_btn"):
                with st.spinner(f"Opening {ref} in browser…"):
                    res = open_org(ref)
                _ss_record_call(res)
                if res.ok:
                    st.success(f"Opened {ref}.")
                else:
                    st.error("Open failed — see CLI log.")

            if cols[2].button("🚪 Logout", key="sforg_logout_btn"):
                with st.spinner(f"Revoking credentials for {ref}…"):
                    res = logout_org(ref)
                _ss_record_call(res)
                if res.ok:
                    refreshed, lr = list_orgs()
                    _ss_record_call(lr)
                    _ss_replace_orgs(refreshed)
                    st.success(f"Logged out of {ref}.")
                    st.rerun()
                else:
                    st.error("Logout failed — see CLI log.")

    # ─── Sub-tab 3: Active Org for Export ───────────────────────────────────
    with sub[3]:
        st.subheader("Pick an Active Org for FFIS Exports")
        st.markdown(
            '<div class="info-box">Choosing an active org here pre-fills the '
            '<strong>API Endpoint URL</strong> and <strong>Authorization '
            'header</strong> in the Export tab. The access token is fetched '
            'fresh from the CLI keychain each time you click '
            '<em>Use This Org</em>, so you do not need to paste tokens '
            'manually anymore.</div>',
            unsafe_allow_html=True,
        )

        orgs = st.session_state.sf_authorized_orgs or []
        if not orgs:
            st.info("No authorized orgs in session. Refresh the list in the "
                    "previous sub-tab.")
            return

        labels = [o.label() for o in orgs]
        chosen = st.selectbox(
            "Org to use for the next API export",
            options=labels,
            key="sforg_active_choice",
        )
        chosen_org = orgs[labels.index(chosen)]

        if st.button("✅ Use This Org", key="sforg_set_active_btn",
                     type="primary"):
            with st.spinner(f"Fetching access token for {chosen_org.label()}…"):
                full, res = display_org(
                    chosen_org.alias or chosen_org.username
                )
            _ss_record_call(res)
            if res.ok and full:
                st.session_state.sf_active_org = full
                st.success(
                    f"Active org set to **{full.label()}**. The Export tab "
                    f"will now post to `{full.instance_url}` with a fresh "
                    f"OAuth bearer token."
                )
            else:
                st.error("Failed to fetch access token. See CLI log.")

        active = st.session_state.sf_active_org
        if active:
            st.markdown("**Currently active org:**")
            preview = {
                "alias": active.alias,
                "username": active.username,
                "instance_url": active.instance_url,
                "org_id": active.org_id,
                "is_sandbox": active.is_sandbox,
                "is_scratch": active.is_scratch,
                "expiration_date": active.expiration_date,
                "access_token": ("•" * 26
                                 + (active.access_token[-6:]
                                    if active.access_token else "")),
            }
            st.json(preview)

    # ── CLI call log (shared across sub-tabs) ───────────────────────────────
    st.divider()
    _show_cli_log_panel()


# ──────────────────────────────────────────────────────────────────────────────
# CLI HOOK (optional) — lets you smoke-test without Streamlit
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli = find_sf_cli()
    print(f"sf CLI on PATH: {cli or '(not found)'}")
    print(f"Version       : {get_cli_version(cli) or '(unknown)'}")
    if cli:
        orgs, res = list_orgs()
        print(f"Authorized orgs ({len(orgs)}):")
        for o in orgs:
            print(f"  · {o.label()}  →  {o.instance_url}")
