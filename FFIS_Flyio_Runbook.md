# FFIS — Fly.io Deployment Runbook
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
