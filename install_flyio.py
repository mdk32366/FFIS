#!/usr/bin/env python3
"""
install_flyio.py — FFIS Fly.io Deployment Installer
=====================================================
Run this from the root of your FFIS repo:

    python install_flyio.py

What it does:
  1. Replaces Dockerfile with Fly.io-tuned version
  2. Adds fly.toml (Fly.io app configuration)
  3. Updates .dockerignore (keeps secrets out of the image)
  4. Removes Oracle runbook (FFIS_Oracle_Deployment_Runbook.md) if present
  5. Writes FFIS_Flyio_Runbook.md
  6. Stages all changes with git add
  7. Prints the 4 commands you need to finish the deploy

Safe to re-run — checks before overwriting, backs up Dockerfile first.

Author: Matthew Kelly / FFIS v2.1
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

# ── Colour helpers ────────────────────────────────────────────────────────────
def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text

OK   = lambda t: print(_c("92",   f"  ✅  {t}"))
SKIP = lambda t: print(_c("93",   f"  ⏭   {t}"))
ERR  = lambda t: print(_c("91",   f"  ❌  {t}"))
HDR  = lambda t: print(_c("96;1", f"\n{'─'*60}\n  {t}\n{'─'*60}"))
INFO = lambda t: print(f"       {t}")


# ════════════════════════════════════════════════════════════════════════════
#  FILE CONTENTS
# ════════════════════════════════════════════════════════════════════════════

DOCKERFILE = r'''# Dockerfile — FFIS Flat File Scrubber
# Tuned for Fly.io: non-root user, .streamlit/config.toml baked in,
# secrets loaded from Fly secrets (not .env file).

FROM python:3.11-slim

# ── System dependencies ───────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Non-root user ─────────────────────────────────────────────────────────────
RUN useradd -m -u 1000 appuser
WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# ── Streamlit configuration ───────────────────────────────────────────────────
RUN mkdir -p /app/.streamlit
RUN echo '\
[server]\n\
headless = true\n\
port = 8501\n\
address = "0.0.0.0"\n\
enableCORS = false\n\
enableXsrfProtection = false\n\
maxUploadSize = 500\n\
\n\
[browser]\n\
gatherUsageStats = false\n\
\n\
[theme]\n\
base = "dark"\n\
' > /app/.streamlit/config.toml

# ── Application source ────────────────────────────────────────────────────────
COPY --chown=appuser:appuser . .

# ── Switch to non-root ────────────────────────────────────────────────────────
USER appuser

# ── Health check ─────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

# ── Entrypoint ────────────────────────────────────────────────────────────────
EXPOSE 8501
CMD ["python", "-m", "streamlit", "run", "flat_file_scrubber.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
'''

FLY_TOML = '''# fly.toml — FFIS Flat File Scrubber
# Deploy with: fly deploy
# Docs: https://fly.io/docs/reference/configuration/

app            = "ffis-scrubber"   # Change if name is taken — must be globally unique
primary_region = "sea"             # Seattle — closest to the PNW

[build]
  # Uses the Dockerfile in the repo root

[env]
  STREAMLIT_PAGE_TITLE    = "Flat File Scrubber"
  STREAMLIT_PAGE_ICON     = "🧹"
  STREAMLIT_LAYOUT        = "wide"
  STREAMLIT_SIDEBAR_STATE = "expanded"
  UNDO_HISTORY_LIMIT      = "20"
  API_REQUEST_TIMEOUT     = "30"

[http_service]
  internal_port        = 8501
  force_https          = true
  auto_stop_machines   = false   # always-on — no cold starts for a data tool
  auto_start_machines  = true
  min_machines_running = 1

  [http_service.concurrency]
    type       = "connections"
    hard_limit = 25
    soft_limit = 20

[[http_service.checks]]
  grace_period = "30s"
  interval     = "15s"
  method       = "GET"
  timeout      = "5s"
  path         = "/_stcore/health"

[[vm]]
  memory = "2gb"
  cpus   = 1
'''

DOCKERIGNORE_ADDITIONS = """
# ── Fly.io / secrets ──────────────────────────────────────────────────────────
.env
secrets.json
.venv/
__pycache__/
*.pyc
*.pyo
.git/
*.bak
ffis_test_data/
install_chat.py
install_flyio.py
generate_ffis_test_data.py
FFIS_Oracle_Deployment_Runbook.md
"""

FLYIO_RUNBOOK = '''# FFIS — Fly.io Deployment Runbook
**Flat File Scrubber + AI Chat Assistant**
*Matthew Kelly — April 2026*

---

## Architecture

```
Internet (HTTPS)
       │
       ▼
Cloudflare Access      ← email-based login for invited users
       │
       ▼
  fly.dev URL          ← free *.fly.dev subdomain, SSL automatic
       │
       ▼
Fly.io Machine         ← 1 vCPU · 2 GB RAM · Seattle region
  └── FFIS :8501       ← Streamlit + AI chat tab, always-on
```

**Monthly cost: ~$10–11**

---

## Prerequisites

- [ ] `flyctl` installed — https://fly.io/docs/hands-on/install-flyctl/
- [ ] Fly.io account with a credit card on file
- [ ] `ANTHROPIC_API_KEY` ready
- [ ] Email addresses of people you want to grant access

---

# PHASE 1 — Install flyctl & Log In

```bash
# macOS / Linux
curl -L https://fly.io/install.sh | sh

# Windows (PowerShell)
iwr https://fly.io/install.ps1 -useb | iex

# Log in
fly auth login
```

---

# PHASE 2 — Deploy

All deployment files are already in the repo (`Dockerfile`, `fly.toml`).

```bash
cd your-ffis-repo

# 1. Register the app with Fly.io (one-time only)
fly launch --no-deploy
#   App name:   ffis-scrubber  (or change fly.toml if taken)
#   Region:     sea
#   Postgres:   No
#   Redis:      No
#   Deploy now: No

# 2. Set your Anthropic key (and any other secrets)
fly secrets set ANTHROPIC_API_KEY="sk-ant-your-key-here"

# 3. Deploy
fly deploy

# 4. Open the app
fly open
```

> First deploy takes 3–5 minutes. Subsequent deploys are ~60 seconds.

---

# PHASE 3 — Cloudflare Access (User Access Control)

Gates the app behind email-based one-time-code login.
No code changes to FFIS required.

## 3.1 — Create a free Cloudflare account

https://cloudflare.com → Sign up → choose the **Free plan**

## 3.2 — Enable Zero Trust

Cloudflare dashboard → **Zero Trust** → choose a team name (e.g. `ffis-team`)
→ select **Free plan** (supports up to 50 users)

## 3.3 — Create a Tunnel

Zero Trust → **Networks → Tunnels → Create a tunnel → Cloudflare**

Name: `ffis-tunnel`

Under **Public Hostnames**:
- Subdomain: `ffis`
- Domain: your `*.cloudflareaccess.com` domain
- Service Type: `HTTPS`
- URL: `ffis-scrubber.fly.dev`
- ✅ Check **No TLS Verify**

Save tunnel. Your app is now reachable at:
`https://ffis.your-team.cloudflareaccess.com`

## 3.4 — Create an Access Policy

Zero Trust → **Access → Applications → Add an application → Self-hosted**

- Application name: `FFIS`
- Application domain: the subdomain from 3.3
- Policy name: `Allowed Users`
- Action: Allow
- Include rule: **Emails** → add each allowed email address

Save. Test in an incognito window — you should see the Cloudflare login screen.

## 3.5 — Managing access

**Grant access:** Edit the policy → add email
**Revoke access:** Edit the policy → remove email

---

# PHASE 4 — Ongoing Operations

## Deploy an update

```bash
git pull
fly deploy
```

## Useful commands

```bash
fly status          # machine health
fly logs            # live log stream
fly open            # open app in browser
fly ssh console     # SSH into the running machine
fly secrets list    # view secret names (values hidden)
fly secrets set KEY="value"   # add/update a secret
fly scale memory 4096         # bump to 4 GB RAM if needed
fly scale count 0   # pause app (stops billing)
fly scale count 1   # resume app
```

## Billing

~$10–11/month for 1x shared-cpu-1x, 2 GB RAM, always-on.
First 100 GB outbound bandwidth is free each month.

---

# Troubleshooting

| Problem | Fix |
|---|---|
| `fly deploy` fails health check | `fly logs` — look for Python errors |
| 502 on fly.dev URL | Confirm `internal_port = 8501` in fly.toml |
| AI chat tab: "API key not set" | `fly secrets set ANTHROPIC_API_KEY=...` |
| App name taken on `fly launch` | Edit `app =` in fly.toml, retry |
| Cloudflare login loop | Clear browser cookies for the domain |

---

*FFIS Fly.io Runbook v1.0 — April 2026*
'''


# ════════════════════════════════════════════════════════════════════════════
#  INSTALLER
# ════════════════════════════════════════════════════════════════════════════

def main():
    repo_root = Path(__file__).parent

    print(_c("96;1", "\n🚀  FFIS Fly.io Installer"))
    print(_c("90",   "    Flat File Scrubber — v2.1\n"))

    # ── Pre-flight ────────────────────────────────────────────────────────────
    HDR("Pre-flight checks")

    scrubber = repo_root / "flat_file_scrubber.py"
    if not scrubber.exists():
        ERR("flat_file_scrubber.py not found.")
        ERR("Run this script from the root of your FFIS repository.")
        sys.exit(1)
    OK(f"Repo root confirmed: {repo_root}")

    git_dir = repo_root / ".git"
    if not git_dir.exists():
        ERR("No .git directory found — is this a git repo?")
        sys.exit(1)
    OK("Git repo detected")

    # ── Step 1: Dockerfile ────────────────────────────────────────────────────
    HDR("Step 1 — Replace Dockerfile")

    dockerfile = repo_root / "Dockerfile"
    if dockerfile.exists():
        backup = dockerfile.with_suffix(".bak")
        shutil.copy2(dockerfile, backup)
        INFO(f"Backup saved → Dockerfile.bak")
    dockerfile.write_text(DOCKERFILE, encoding="utf-8")
    OK(f"Dockerfile written ({dockerfile.stat().st_size // 1024} KB)")

    # ── Step 2: fly.toml ──────────────────────────────────────────────────────
    HDR("Step 2 — Write fly.toml")

    fly_toml = repo_root / "fly.toml"
    if fly_toml.exists():
        SKIP("fly.toml already exists — overwriting with latest version")
    fly_toml.write_text(FLY_TOML, encoding="utf-8")
    OK("fly.toml written")

    # ── Step 3: .dockerignore ─────────────────────────────────────────────────
    HDR("Step 3 — Update .dockerignore")

    dockerignore = repo_root / ".dockerignore"
    if dockerignore.exists():
        existing = dockerignore.read_text(encoding="utf-8")
        if ".env" in existing and "secrets.json" in existing:
            SKIP(".dockerignore already contains key entries — leaving untouched")
        else:
            dockerignore.write_text(
                existing.rstrip() + "\n" + DOCKERIGNORE_ADDITIONS,
                encoding="utf-8"
            )
            OK(".dockerignore updated with Fly.io entries")
    else:
        dockerignore.write_text(DOCKERIGNORE_ADDITIONS.strip() + "\n", encoding="utf-8")
        OK(".dockerignore created")

    # ── Step 4: Remove Oracle runbook ─────────────────────────────────────────
    HDR("Step 4 — Remove Oracle runbook")

    oracle_runbook = repo_root / "FFIS_Oracle_Deployment_Runbook.md"
    if oracle_runbook.exists():
        oracle_runbook.unlink()
        OK("Removed FFIS_Oracle_Deployment_Runbook.md")
    else:
        SKIP("FFIS_Oracle_Deployment_Runbook.md not found — nothing to remove")

    # Also remove installer scripts that don't belong in the repo
    for stale in ["install_flyio.py"]:
        # We intentionally do NOT remove ourselves mid-run.
        # git will track the removal after the user commits.
        pass

    # ── Step 5: Write Fly.io runbook ──────────────────────────────────────────
    HDR("Step 5 — Write FFIS_Flyio_Runbook.md")

    flyio_runbook = repo_root / "FFIS_Flyio_Runbook.md"
    flyio_runbook.write_text(FLYIO_RUNBOOK, encoding="utf-8")
    OK("FFIS_Flyio_Runbook.md written")

    # ── Step 6: git add ───────────────────────────────────────────────────────
    HDR("Step 6 — Stage changes with git")

    files_to_stage = [
        "Dockerfile",
        "fly.toml",
        ".dockerignore",
        "FFIS_Flyio_Runbook.md",
    ]

    # Stage removals too
    files_to_remove = [
        "FFIS_Oracle_Deployment_Runbook.md",
        "Dockerfile.bak",
    ]

    try:
        subprocess.run(
            ["git", "add"] + files_to_stage,
            cwd=repo_root, check=True, capture_output=True
        )
        OK(f"Staged: {', '.join(files_to_stage)}")

        # git rm --cached for deleted files (silently ignore if not tracked)
        for f in files_to_remove:
            subprocess.run(
                ["git", "rm", "--cached", "--force", "-q", f],
                cwd=repo_root, capture_output=True
            )
        # Also remove Dockerfile.bak from working tree — it's just noise
        bak = repo_root / "Dockerfile.bak"
        if bak.exists():
            bak.unlink()
            INFO("Removed Dockerfile.bak (backup no longer needed)")

        # Show git status summary
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=repo_root, capture_output=True, text=True
        )
        if result.stdout.strip():
            print()
            for line in result.stdout.strip().splitlines():
                INFO(line)

    except subprocess.CalledProcessError as e:
        ERR(f"git add failed: {e}")
        INFO("Stage the files manually with: git add Dockerfile fly.toml .dockerignore FFIS_Flyio_Runbook.md")

    # ── Done ──────────────────────────────────────────────────────────────────
    HDR("✅  Installation complete")

    print(_c("92", "  Finish the deployment with these 4 commands:\n"))

    steps = [
        ('git commit -m "feat: migrate deployment to Fly.io"', "Commit the changes"),
        ("git push",                                             "Push to GitHub"),
        ("fly launch --no-deploy",                              "Register app with Fly.io (one-time)"),
        ('fly secrets set ANTHROPIC_API_KEY="sk-ant-..."',      "Set your API key"),
        ("fly deploy",                                          "Build image and deploy"),
        ("fly open",                                            "Open the live app"),
    ]

    for i, (cmd, desc) in enumerate(steps, 1):
        print(_c("97",  f"  {i}. {desc}:"))
        print(_c("90",  f"     {cmd}\n"))

    print(_c("93", "  💡  If 'ffis-scrubber' is taken on fly launch,"))
    print(_c("93", "      edit app = \"...\" in fly.toml and retry.\n"))
    print(_c("96", "  Full instructions: FFIS_Flyio_Runbook.md\n"))


if __name__ == "__main__":
    main()
