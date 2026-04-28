#!/usr/bin/env python3
"""
install_ffis_addons.py
======================
Single-file, idempotent installer for the FFIS Salesforce-CLI Org panel and
the Job Summary / Diagnostic Report panel.

What it does (idempotently):
    1. Verifies that we're running inside an FFIS clone (looks for
       `flat_file_scrubber.py` and `config.py` next to this script's target).
    2. Drops `ffis_sf_org.py` and `ffis_diagnostics.py` next to the main app.
    3. Patches `flat_file_scrubber.py` to:
         a. Import `render_org_panel` and `render_diagnostic_panel`.
         b. Add two new tab labels to the `tabs = st.tabs([...])` call.
         c. Append two new tab bodies that delegate to the new panels.
         d. Capture the source filename in session state at ingest time so the
            diagnostic report can reference it.
    4. Updates `requirements.txt` if needed (no NEW deps — pandas/streamlit/
       numpy are already pinned — but bumps min versions only if the file
       under-pins).
    5. Adds a documentation block to `README.md` describing the two features.

Idempotency:
    Every patch site is wrapped in a unique sentinel comment. Reruns short-
    circuit when the sentinel is already present. Running this script ten
    times in a row is identical to running it once.

Usage:
    cd /path/to/your/FFIS/clone
    python install_ffis_addons.py
        # or specify a target dir explicitly:
    python install_ffis_addons.py --target /path/to/FFIS

Author: Matthew Kelly  (FFIS / SFCOE Data Steward Toolkit extension installer)
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import textwrap
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────────────
# SENTINEL MARKERS — every patch site must include one of these so reruns are
# safe. Don't change these strings without also bumping the existing patches
# in the wild.
# ──────────────────────────────────────────────────────────────────────────────

S_IMPORTS  = "# >>> ffis-addons:imports >>>"
S_TAB_LBL  = "# >>> ffis-addons:tab-labels >>>"
S_TAB_BDY  = "# >>> ffis-addons:tab-bodies >>>"
S_INGEST   = "# >>> ffis-addons:capture-source-filename >>>"
S_README   = "<!-- ffis-addons:readme-block -->"

# ──────────────────────────────────────────────────────────────────────────────
# THE TWO MODULE FILES, EMBEDDED HERE SO THE INSTALLER IS A SINGLE FILE.
# ──────────────────────────────────────────────────────────────────────────────

# We don't embed the (large) module bodies here — the installer expects them
# to live next to itself. If you copy the installer into a different folder,
# also copy the two module files. This keeps the installer auditable.

MODULE_FILES = ("ffis_sf_org.py", "ffis_diagnostics.py")


# ──────────────────────────────────────────────────────────────────────────────
# PATCH SNIPPETS
# ──────────────────────────────────────────────────────────────────────────────

PATCH_IMPORTS = textwrap.dedent(f"""
{S_IMPORTS}
# Lazy-imported inside their tab bodies to avoid blowing up app start if
# either module has an import-time error in this environment.
def _ffis_addons_render_org_panel():
    from ffis_sf_org import render_org_panel
    return render_org_panel()

def _ffis_addons_render_diag_panel():
    from ffis_diagnostics import render_diagnostic_panel
    return render_diagnostic_panel()
# <<< ffis-addons:imports <<<
""").strip() + "\n"


# Inserted RIGHT BEFORE the closing `])` of the original `tabs = st.tabs([...]`
# call. Any number of trailing commas is fine.
PATCH_TAB_LABELS = textwrap.dedent(f"""
    {S_TAB_LBL}
    "🧪 SF Org",
    "📊 Diagnostic Report",
    # <<< ffis-addons:tab-labels <<<
""").rstrip() + "\n"


# Appended at the very end of `flat_file_scrubber.py`. The two new tabs are
# at indices [-2] and [-1] of the tabs list because we inserted them last.
PATCH_TAB_BODIES = textwrap.dedent(f"""

{S_TAB_BDY}
# ══════════════════════════════════════════════
# TAB N+0 — SALESFORCE ORG MANAGEMENT (added by ffis-addons)
# ══════════════════════════════════════════════
with tabs[-2]:
    _ffis_addons_render_org_panel()

# ══════════════════════════════════════════════
# TAB N+1 — JOB SUMMARY & DIAGNOSTIC REPORT (added by ffis-addons)
# ══════════════════════════════════════════════
with tabs[-1]:
    _ffis_addons_render_diag_panel()
# <<< ffis-addons:tab-bodies <<<
""")


# Patch inside `load_dataframe` so the diagnostic panel knows the source name.
PATCH_INGEST = textwrap.dedent(f"""
    {S_INGEST}
    try:
        st.session_state["source_filename"] = filename
        st.session_state["ingest_filename"] = filename
    except Exception:
        pass
    # <<< ffis-addons:capture-source-filename <<<
""").rstrip() + "\n"


README_BLOCK = textwrap.dedent(f"""

{S_README}

## 🧪 Salesforce Org Management (Add-On)

The **🧪 SF Org** tab provides a Salesforce-CLI-driven UI for authorizing,
creating, and managing Salesforce orgs from inside FFIS — eliminating the need
to copy/paste OAuth bearer tokens into the API Export tab.

**Capabilities:**

* **Authorize Org (Web Login)** — Launches `sf org login web` for Production,
  Sandbox, or any custom My Domain URL. Stores credentials in the local CLI
  keychain under an alias of your choice.
* **Create Scratch Org** — Wraps `sf org create scratch` with a UI that
  validates your SFDX project directory, definition file, Dev Hub selection,
  and duration before invoking the CLI.
* **Authorized Orgs** — Lists every org the CLI knows about, classified by
  type (Production / Developer Edition / Sandbox / Scratch / Dev Hub). One-
  click *Show details*, *Open in browser*, and *Logout*.
* **Active Org for Export** — Pick any authorized org and FFIS will fetch a
  fresh access token from the CLI keychain, ready to be used by the API
  Export tab.

**Requirements:** the Salesforce CLI (`sf` v2 or `sfdx` legacy) must be
installed and on PATH. Install it from
[developer.salesforce.com/tools/salesforcecli](https://developer.salesforce.com/tools/salesforcecli)
(`brew install salesforcedx` on macOS).

---

## 📊 Job Summary & Diagnostic Report (Add-On)

The **📊 Diagnostic Report** tab runs the full battery of pandas summary and
diagnostic checks against the **raw** ingested DataFrame (before any cleaning
operations) and produces a comprehensive report plus a per-stage time
estimate for completing the cleaning pipeline.

**What it computes:**

* Shape, memory footprint, total cells, total nulls, duplicate-row count
* Per-column profile — dtype, non-null count, null %, unique count,
  cardinality class, leading/trailing whitespace count, special-character
  hit count, sample value, and top values for low/medium-cardinality columns
* Required-field gap analysis vs. your selected target object (combined with
  any user-marked required columns from the Inspect tab)
* Salesforce ID candidate detection — flags any column whose values match
  the 15- or 18-character alphanumeric ID format
* Special-character density per column (regex-driven, with sampling for
  files >100k rows)
* Numeric correlations — top 10 pairs with |ρ| ≥ 0.5
* Head / tail samples
* Raw `df.info()` capture
* **Quality Grade (A–F)** — composite score with itemized penalties

**Time-to-clean estimate** — for each of the 11 FFIS workflow stages, the
estimator combines:

* A base overhead cost (human attention)
* A compute cost that scales linearly with row count
* An issue-density multiplier that bumps the cost when the underlying
  problem (nulls, dupes, special chars, required gaps) is dense

The result is a stage-by-stage breakdown plus a total. Stages with zero
issue density are explicitly marked **skipped**.

**Exports:** Markdown report (`<job_name>_diagnostic_report.md`) and JSON
report (`<job_name>_diagnostic_report.json`), both downloadable from the
**💾 Export** sub-tab inside the panel.

**Standalone usage:**

```bash
# Generate a Markdown report from any CSV without launching the UI:
python ffis_diagnostics.py path/to/data.csv Account
```

<!-- /ffis-addons:readme-block -->
""")


# ──────────────────────────────────────────────────────────────────────────────
# UTILITIES
# ──────────────────────────────────────────────────────────────────────────────

class InstallError(RuntimeError):
    pass


def log(msg: str, *, level: str = "INFO"):
    icon = {"INFO": "ℹ️ ", "OK": "✅", "WARN": "⚠️ ", "ERROR": "❌"}[level]
    print(f"  {icon} {msg}")


def find_target_dir(arg: str | None) -> Path:
    """Resolve and validate the FFIS target directory."""
    if arg:
        target = Path(arg).expanduser().resolve()
    else:
        target = Path.cwd().resolve()

    if not (target / "flat_file_scrubber.py").is_file():
        raise InstallError(
            f"`flat_file_scrubber.py` not found in {target}. "
            f"Run this from inside an FFIS clone, or pass --target."
        )
    if not (target / "config.py").is_file():
        raise InstallError(
            f"`config.py` not found in {target}. "
            f"This doesn't look like a complete FFIS checkout."
        )
    return target


def installer_dir() -> Path:
    """Directory where this script + the two module files live."""
    return Path(__file__).resolve().parent


def copy_modules(target: Path):
    """Copy ffis_sf_org.py and ffis_diagnostics.py to the target dir."""
    src_dir = installer_dir()
    for name in MODULE_FILES:
        src = src_dir / name
        dst = target / name
        if not src.is_file():
            raise InstallError(
                f"Missing source module `{name}` next to the installer "
                f"at {src_dir}. Make sure both module files are alongside "
                f"this script."
            )
        if dst.is_file() and dst.read_bytes() == src.read_bytes():
            log(f"`{name}` already up-to-date.", level="OK")
        else:
            shutil.copy2(src, dst)
            log(f"`{name}` written to {dst}.", level="OK")


def patch_main_app(target: Path):
    """Apply all four patches to flat_file_scrubber.py, idempotently."""
    fp = target / "flat_file_scrubber.py"
    src = fp.read_text(encoding="utf-8")
    original = src

    # ── 1. Imports / lazy renderers ────────────────────────────────────────
    if S_IMPORTS in src:
        log("imports patch already present.", level="OK")
    else:
        # Insert after the last existing `from config import (...)` line, OR
        # after the last `import` statement near the top of the file.
        # Match the closing `)` of the multi-line config import.
        m = re.search(
            r"^from config import \(.*?\)\s*$",
            src, flags=re.MULTILINE | re.DOTALL,
        )
        if m:
            insert_at = m.end()
            src = src[:insert_at] + "\n\n" + PATCH_IMPORTS + src[insert_at:]
            log("imports patch applied (after config-import block).",
                level="OK")
        else:
            # Fallback: insert after the last top-of-file `import` line.
            lines = src.splitlines(keepends=True)
            last_import = 0
            for i, line in enumerate(lines):
                if line.startswith(("import ", "from ")):
                    last_import = i
                if i > 60:
                    break
            lines.insert(last_import + 1,
                         "\n" + PATCH_IMPORTS + "\n")
            src = "".join(lines)
            log("imports patch applied (fallback insertion).", level="WARN")

    # ── 2. Tab labels ──────────────────────────────────────────────────────
    if S_TAB_LBL in src:
        log("tab-labels patch already present.", level="OK")
    else:
        # Find the `tabs = st.tabs([ ... ])` block and insert the new labels
        # just before its closing `])`.
        m = re.search(
            r"(tabs\s*=\s*st\.tabs\s*\(\s*\[)(.+?)(\s*\]\s*\))",
            src, flags=re.DOTALL,
        )
        if not m:
            raise InstallError(
                "Could not locate `tabs = st.tabs([...])` in "
                "flat_file_scrubber.py. Manual patching required."
            )
        before, body, after = m.group(1), m.group(2), m.group(3)
        # Ensure the existing list ends with a comma + newline before our
        # additions so the syntax is always clean regardless of what was
        # there before.
        body_stripped = body.rstrip()
        if not body_stripped.endswith(","):
            body_stripped = body_stripped + ","
        new_body = body_stripped + "\n" + PATCH_TAB_LABELS
        src = src[:m.start()] + before + new_body + after + src[m.end():]
        log("tab-labels patch applied.", level="OK")

    # ── 3. Tab bodies (appended at end) ────────────────────────────────────
    if S_TAB_BDY in src:
        log("tab-bodies patch already present.", level="OK")
    else:
        if not src.endswith("\n"):
            src = src + "\n"
        src = src + PATCH_TAB_BODIES
        log("tab-bodies patch applied (appended).", level="OK")

    # ── 4. Capture source filename in load_dataframe ──────────────────────
    if S_INGEST in src:
        log("ingest-filename patch already present.", level="OK")
    else:
        # Inject right after the line that mutates st.session_state.raw_df
        # in load_dataframe. We anchor on the docstring or the function def.
        anchor = re.search(
            r"(def\s+load_dataframe\s*\([^)]*\)\s*:\s*\n"  # def line
            r"(?:\s+\"\"\".*?\"\"\"\s*\n)?"                # optional docstring
            r")",
            src, flags=re.DOTALL,
        )
        if anchor:
            insert_at = anchor.end()
            # Indent the patch to match the function body (4 spaces).
            indented = "\n".join(
                ("    " + line if line.strip() else line)
                for line in PATCH_INGEST.splitlines()
            ) + "\n"
            src = src[:insert_at] + indented + src[insert_at:]
            log("ingest-filename patch applied.", level="OK")
        else:
            log("Could not locate `load_dataframe` — diagnostic panel will "
                "still work, but the source filename field will be blank "
                "until you set st.session_state['source_filename'] yourself.",
                level="WARN")

    if src != original:
        # Save a single rolling backup so the user can recover.
        backup = fp.with_suffix(".py.ffis-addons.bak")
        if not backup.exists():
            backup.write_text(original, encoding="utf-8")
            log(f"Backup saved to {backup.name}.", level="OK")
        fp.write_text(src, encoding="utf-8")
        log("flat_file_scrubber.py updated.", level="OK")
    else:
        log("flat_file_scrubber.py — no changes needed.", level="OK")


def update_requirements(target: Path):
    """Ensure requirements.txt mentions everything we actually need."""
    fp = target / "requirements.txt"
    if not fp.is_file():
        log("requirements.txt missing — leaving alone (FFIS uses one).",
            level="WARN")
        return
    content = fp.read_text(encoding="utf-8")
    needed = {
        "streamlit": ">=1.32.0",
        "pandas":    ">=2.0.0",
        "numpy":     ">=1.26.0",
    }
    new_lines: list[str] = []
    seen = {pkg: False for pkg in needed}
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            new_lines.append(line)
            continue
        m = re.match(r"^([A-Za-z0-9_\-]+)", s)
        if m and m.group(1).lower() in needed:
            seen[m.group(1).lower()] = True
        new_lines.append(line)
    additions = [
        f"{pkg}{ver}" for pkg, ver in needed.items() if not seen[pkg]
    ]
    if additions:
        if new_lines and new_lines[-1] != "":
            new_lines.append("")
        new_lines.append("# ffis-addons additions (no new transitive deps)")
        new_lines.extend(additions)
        fp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        log(f"requirements.txt: appended {', '.join(additions)}.", level="OK")
    else:
        log("requirements.txt already covers all needed packages.",
            level="OK")


def patch_readme(target: Path):
    """Append the documentation block to README.md if not already present."""
    fp = target / "README.md"
    if not fp.is_file():
        log("README.md missing — skipping documentation patch.", level="WARN")
        return
    content = fp.read_text(encoding="utf-8")
    if S_README in content:
        log("README.md already documents the add-ons.", level="OK")
        return
    if not content.endswith("\n"):
        content = content + "\n"
    content = content + README_BLOCK
    fp.write_text(content, encoding="utf-8")
    log("README.md: add-on documentation appended.", level="OK")


def verify_install(target: Path):
    """Sanity check: import the new modules from the target dir."""
    sys.path.insert(0, str(target))
    try:
        import importlib
        for name in ("ffis_sf_org", "ffis_diagnostics"):
            importlib.import_module(name)
            log(f"`{name}` imports cleanly from {target}.", level="OK")
    except Exception as e:
        log(f"Post-install import check failed: {e}", level="ERROR")
        raise
    finally:
        sys.path.pop(0)


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Install the FFIS add-on tabs (SF Org + Diagnostic).",
    )
    parser.add_argument(
        "--target", "-t", default=None,
        help="Path to the FFIS clone. Defaults to the current directory.",
    )
    parser.add_argument(
        "--skip-readme", action="store_true",
        help="Don't append documentation to README.md.",
    )
    parser.add_argument(
        "--skip-requirements", action="store_true",
        help="Don't touch requirements.txt.",
    )
    args = parser.parse_args()

    print("\n  🧹 FFIS Add-On Installer (SF Org + Diagnostic Report)")
    print("  " + "─" * 56)

    try:
        target = find_target_dir(args.target)
        log(f"Target FFIS directory: {target}", level="INFO")

        copy_modules(target)
        patch_main_app(target)
        if not args.skip_requirements:
            update_requirements(target)
        if not args.skip_readme:
            patch_readme(target)
        verify_install(target)

        print()
        log("Installation complete.", level="OK")
        log("Restart the Streamlit server to pick up the new tabs:",
            level="INFO")
        print("\n      streamlit run flat_file_scrubber.py\n")
        return 0
    except InstallError as e:
        print()
        log(str(e), level="ERROR")
        return 1
    except Exception as e:
        print()
        log(f"Unexpected error: {e}", level="ERROR")
        import traceback
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
