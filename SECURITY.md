# Security Configuration Guide

## Overview

This document explains how the Flat File Scrubber protects sensitive data (API keys, database credentials, email passwords, etc.) from being accidentally committed to version control.

---

## Why This Matters

**Never commit secrets to git!** If you accidentally push API keys or passwords to a GitHub repository:
- 🔴 Anyone can access your systems
- 🔴 Attackers can abuse your services
- 🔴 GitHub will scan and warn about exposed secrets
- 🔴 Credentials must be rotated immediately (expensive and disruptive)

---

## How We Protect Secrets

### 1. `.gitignore` Configuration

The `.gitignore` file automatically excludes sensitive files from version control:

```
# Ignored (never committed):
.env
.env.local
secrets.json
secrets.*.json
credentials/

# Allowed (safe to commit):
.env.example
.dockerenv.example
secrets.json.example
```

**The rule:** Files ending in `.example` are safe to commit (templates). Real secrets are not.

### 2. Example Files

Three example files guide you through configuration:

| File | Purpose | Commit to Git? |
|------|---------|---|
| `.env.example` | Template for application settings | ✅ Yes |
| `.dockerenv.example` | Template for Docker environment | ✅ Yes |
| `secrets.json.example` | Template for sensitive credentials | ✅ Yes |

**Workflow:**
```bash
# Copy example file
cp .env.example .env

# Edit with your real values
nano .env

# Run application (only .env.example gets committed)
streamlit run flat_file_scrubber.py
```

### 3. Supported Secret Sources (Priority Order)

The application loads configuration from these sources (in order):

1. **Environment Variables** (highest priority)
   ```bash
   export SNOWFLAKE_ACCOUNT=xy12345.us-east-1
   export SNOWFLAKE_USER=my_user
   ```

2. **secrets.json** (for complex nested configs)
   ```json
   {
     "snowflake": {
       "account": "xy12345.us-east-1",
       "user": "my_user",
       "password": "***"
     }
   }
   ```

3. **System Environment Variables** (defaults)
   ```bash
   echo $SNOWFLAKE_ACCOUNT
   ```

4. **Defaults in config.py** (lowest priority)

---

## Quick Setup

### Automated Setup (Recommended)

Run the interactive setup wizard:

```bash
python setup_secrets.py
```

This script will:
- ✅ Guide you through creating `.env`
- ✅ Securely prompt for passwords (won't echo to screen)
- ✅ Create `secrets.json` from your input
- ✅ Verify `.gitignore` is protecting files
- ✅ Set file permissions to `600` (user-only read/write)

### Manual Setup

```bash
# 1. Copy example files
cp .env.example .env
cp secrets.json.example secrets.json

# 2. Edit with your credentials
nano .env
nano secrets.json

# 3. (Optional) Restrict file permissions
chmod 600 .env secrets.json

# 4. Verify .gitignore includes these files
cat .gitignore | grep -E "\.env|secrets\.json"
```

---

## Environment Variables & Secrets Reference

### Application Configuration (in `.env`)

```bash
# Streamlit UI
STREAMLIT_PAGE_TITLE=Flat File Scrubber
STREAMLIT_LAYOUT=wide

# Salesforce
SALESFORCE_OBJECTS=Account,Contact,Lead

# Application behavior
UNDO_HISTORY_LIMIT=20
API_REQUEST_TIMEOUT=30
```

### Sensitive Credentials (in `secrets.json` or env vars)

```bash
# Snowflake
SNOWFLAKE_ACCOUNT=xy12345.us-east-1
SNOWFLAKE_USER=your_user
SNOWFLAKE_PASSWORD=***
SNOWFLAKE_DATABASE=your_db
SNOWFLAKE_SCHEMA=your_schema
SNOWFLAKE_WAREHOUSE=your_warehouse

# Salesforce API
SF_API_KEY=***
SF_API_URL=https://your-instance.salesforce.com

# Email (IMAP)
IMAP_HOST=imap.gmail.com
IMAP_EMAIL=your_email@gmail.com
IMAP_PASSWORD=***

# Email (SMTP)
SMTP_HOST=smtp.gmail.com
SMTP_FROM_EMAIL=your_email@gmail.com
SMTP_PASSWORD=***

# API Endpoints
API_ENDPOINT_URL=https://api.example.com
API_AUTH_TOKEN=***
```

---

## Docker Deployment with Secrets

### Local Development

Secrets are automatically mounted as read-only:

```bash
docker-compose up -d
# docker-compose.yml mounts .env and secrets.json as read-only
```

### Cloud Deployment (Google Cloud Run)

Never pass secrets on command line. Use Cloud Secret Manager:

```bash
# Create secret in Google Cloud
echo -n "my_password" | gcloud secrets create snowflake-password --data-file=-

# Reference in deployment
gcloud run deploy flat-file-scrubber \
  --set-env-vars SNOWFLAKE_PASSWORD=projects/PROJECT/secrets/snowflake-password/latest
```

### Cloud Deployment (AWS ECS)

Use AWS Secrets Manager:

```bash
# Create secret
aws secretsmanager create-secret \
  --name ffis/snowflake-password \
  --secret-string "my_password"

# Reference in task definition
{
  "containerDefinitions": [{
    "secrets": [{
      "name": "SNOWFLAKE_PASSWORD",
      "valueFrom": "arn:aws:secretsmanager:region:account-id:secret:ffis/snowflake-password"
    }]
  }]
}
```

### Kubernetes with Secrets

```bash
# Create secret from .env file
kubectl create secret generic ffis-secrets --from-file=.env

# Reference in deployment
env:
- name: SNOWFLAKE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: ffis-secrets
      key: SNOWFLAKE_PASSWORD
```

---

## Security Checklist

**Before Committing Code:**

- [ ] Run `git status` and verify `.env` and `secrets.json` are NOT staged
- [ ] Check `.gitignore` includes `.env`, `secrets.json`, and `credentials/`
- [ ] Never commit files with passwords, API keys, or tokens
- [ ] Example files (`.*.example`) are safe to commit

**Before Deploying to Production:**

- [ ] Rotate all credentials (passwords, tokens, API keys)
- [ ] Use managed secrets (Cloud Secrets Manager, AWS Secrets Manager, etc.)
- [ ] Enable container image scanning for vulnerabilities
- [ ] Set up monitoring and logging for failed authentication
- [ ] Document rotation schedule (e.g., rotate passwords every 90 days)

**Incident Response (Credentials Accidentally Committed):**

```bash
# 1. IMMEDIATELY rotate all credentials
# 2. Remove from git history
git filter-branch --tree-filter 'rm -f secrets.json' -- --all
git push origin --force

# 3. Notify team members
# 4. Monitor for unauthorized access
```

---

## Troubleshooting

**Q: Application says "secret not found" or configuration missing**

A: Check precedence:
```bash
# 1. Check environment variables
env | grep SNOWFLAKE

# 2. Check secrets.json
cat secrets.json | grep snowflake

# 3. Check .env
cat .env | grep SNOWFLAKE

# 4. Check defaults in config.py
grep "getenv" config.py
```

**Q: .env file got committed to git!**

A: Remove it immediately:
```bash
git rm --cached .env
git commit -m "Remove .env (oops!)"
git push
# Then rotate all credentials
```

**Q: How do I know if a file is gitignored?**

A: Test it:
```bash
git check-ignore -v .env
# Output: .env:9:	.env
# (means it's ignored on line 9 of .gitignore)
```

**Q: Can I store secrets in environment variables instead?**

A: Yes! It's actually preferred for production:
```bash
export SNOWFLAKE_PASSWORD="my_password"
export SNOWFLAKE_ACCOUNT="xy12345.us-east-1"
python -m streamlit run flat_file_scrubber.py
```

---

## Best Practices

✅ **Do This:**
- Use `.env.example` as a template
- Use `setup_secrets.py` for interactive setup
- Rotate credentials regularly
- Use managed secrets in production
- Keep secrets.json and .env backed up securely
- Document which secrets your app needs

❌ **Don't Do This:**
- Hardcode passwords in Python code
- Commit `.env` or `secrets.json` to git
- Share credentials in Slack, email, or chat
- Use generic/default passwords
- Store secrets in comments or documentation
- Mix secrets with application code

---

## Resources

- [GitHub: Secret scanning](https://docs.github.com/en/code-security/secret-scanning)
- [OWASP: Secrets Management](https://owasp.org/www-community/Secrets_Management)
- [12-Factor App: Config](https://12factor.net/config)
- [Snowflake: Secure Configuration](https://docs.snowflake.com/en/user-guide/security)
- [Docker: Use secrets](https://docs.docker.com/engine/swarm/secrets/)
