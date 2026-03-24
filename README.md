# 🧹 Flat File Scrubber
**SFCOE Data Steward Toolkit**
*Authored by Matthew Kelly — Jan 26, 2026*

---

## Table of Contents

1. [Overview](#overview)
2. [Quick Start](#quick-start)
3. [System Requirements & Installation](#system-requirements--installation)
4. [Configuration System](#configuration-system)
   - [Understanding the Configuration Cascade](#understanding-the-configuration-cascade)
   - [Option 1: Environment Variables File (.env)](#option-1-environment-variables-file-env)
   - [Option 2: System Environment Variables](#option-2-system-environment-variables)
   - [Option 3: Secrets File (secrets.json)](#option-3-secrets-file-secretsjson)
   - [Available Configuration Options](#available-configuration-options)
5. [Launching the Application](#launching-the-application)
6. [Application Layout](#application-layout)
7. [Sidebar Controls](#sidebar-controls)
8. [Step-by-Step Workflow Guide](#step-by-step-workflow-guide)
   - [Step 1 — Ingest](#step-1--ingest)
   - [Step 2 — Inspect](#step-2--inspect)
   - [Step 3 — Drop & Rename Columns](#step-3--drop--rename-columns)
   - [Step 4 — Null Handling](#step-4--null-handling)
   - [Step 5 — Types & Splits](#step-5--types--splits)
   - [Step 6 — Special Characters](#step-6--special-characters)
   - [Step 7 — Duplicates](#step-7--duplicates)
   - [Step 8 — Incomplete Records](#step-8--incomplete-records)
   - [Step 9 — Salesforce Duplicate Check](#step-9--salesforce-duplicate-check)
   - [Step 10 — Export](#step-10--export)
   - [Step 11 — Data Frames Viewer](#step-11--data-frames-viewer)
9. [Intake Methods](#intake-methods)
10. [Supported Salesforce Objects](#supported-salesforce-objects)
11. [The Four DataFrames](#the-four-dataframes)
12. [Undo History](#undo-history)
13. [Email Configuration Reference](#email-configuration-reference)
14. [API Load Reference](#api-load-reference)
15. [Troubleshooting](#troubleshooting)

---

## Overview

The Flat File Scrubber is an interactive Streamlit web application that guides a data steward through the complete lifecycle of cleaning a CSV file before loading it into Salesforce or Snowflake. It mirrors the SFCOE Flat File Scrubbing Workflow diagram and implements every processing stage as a dedicated, interactive tab.

**Key capabilities at a glance:**

| Capability | Description |
|---|---|
| Three intake modes | Browser upload, server file path, or email (IMAP) |
| 11 workflow tabs | Full cleaning pipeline from ingest to export |
| 4 named DataFrames | Clean, Dupes, Bad/Incomplete, SF Dupes — all exportable |
| 20-step undo history | Roll back any destructive operation instantly |
| Live sidebar metrics | Row counts across all DataFrames update in real time |
| Three export modes | CSV download, REST API POST with batching, SMTP email |
| Fully configurable | Environment variables, config files, and defaults work together |

---

## Quick Start

**For running locally with defaults:**

```bash
# 1. Clone the repository
git clone https://github.com/mdk32366/FFIS.git
cd FFIS

# 2. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate       # macOS / Linux
.venv\Scripts\activate          # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the app (any of these work)
run.bat                                    # Windows (simplest!)
./run.sh                                   # macOS / Linux
python -m streamlit run flat_file_scrubber.py  # Universal
```

The application will open at `http://localhost:8501`.

---

## 🔐 Security: Protecting Your Secrets

This project includes built-in protection to prevent API keys, passwords, and database credentials from being accidentally committed to git.

**Quick setup:**
```bash
# Interactive wizard for creating .env and secrets.json
python setup_secrets.py

# Then start the app
streamlit run flat_file_scrubber.py
```

**What gets protected:**
- ✅ `.env` file (application settings) — git-ignored
- ✅ `secrets.json` file (credentials) — git-ignored
- ✅ Environment variables (Snowflake, Salesforce, email, API keys) — never hardcoded
- ✅ `.env.example` template — safe to commit (no secrets)

**For detailed security documentation**, see [SECURITY.md](SECURITY.md).

---

## System Requirements & Installation

### System Requirements

- **Python:** 3.9 or higher
- **Operating System:** macOS, Linux, or Windows
- **Network access:** Required for IMAP intake and API export features

### Install Dependencies

```bash
pip install -r requirements.txt
```

Or install packages individually:

```bash
pip install streamlit>=1.32.0 pandas>=2.0.0 numpy>=1.26.0 requests>=2.31.0
```

All other libraries used (`imaplib`, `smtplib`, `email`, `glob`, `pathlib`, `io`, `os`, `re`, `json`, `datetime`) are part of the Python standard library.

### Virtual Environment Setup

Recommended for development:

```bash
python -m venv .venv
source .venv/bin/activate       # macOS / Linux
.venv\Scripts\activate          # Windows

pip install -r requirements.txt
```

---

## Configuration System

The Flat File Scrubber uses a **layered configuration system** to support local development, container deployments, and CI/CD pipelines. No hardcoded values exist in the source code—everything is configurable.

### Understanding the Configuration Cascade

Configuration is loaded in this priority order (highest priority first):

```
┌─────────────────────────────┐
│ System Environment Variables │  ← Highest priority (CI/CD, containers)
├─────────────────────────────┤
│ .env File (local)           │  ← Development & testing
├─────────────────────────────┤
│ secrets.json (credentials)  │  ← Credentials only
├─────────────────────────────┤
│ Hardcoded Defaults          │  ← Fallback values
└─────────────────────────────┘
```

**Example:** If you set `UNDO_HISTORY_LIMIT=50` in `.env` and also export `UNDO_HISTORY_LIMIT=100` in your shell, the application uses `100` (system environment wins).

---

### Option 1: Environment Variables File (.env)

The `.env` file is ideal for **local development and team collaboration**. It's included in `.gitignore` to prevent accidental credential leaks.

#### Setup

```bash
# Copy the example file
cp .env.example .env

# Edit with your settings
nano .env              # Linux/macOS
notepad .env           # Windows
```

#### Example .env File

```bash
# ──────────────────────────────────────────────
# STREAMLIT (User Interface)
# ──────────────────────────────────────────────
STREAMLIT_PAGE_TITLE=Flat File Scrubber
STREAMLIT_PAGE_ICON=🧹
STREAMLIT_LAYOUT=wide
STREAMLIT_SIDEBAR_STATE=expanded

# ──────────────────────────────────────────────
# SALESFORCE CONFIGURATION
# ──────────────────────────────────────────────
SALESFORCE_OBJECTS=Account,Contact,Lead,Opportunity,User,Snowflake Table - DEFAULT
SPECIAL_CHARS_PATTERN=[\*\n\^\$\#\@\!\%\&\(\)\[\]\{\}\<\>\?\/\\|`~"\';:]

# ──────────────────────────────────────────────
# APPLICATION BEHAVIOR
# ──────────────────────────────────────────────
UNDO_HISTORY_LIMIT=20
API_REQUEST_TIMEOUT=30

# ──────────────────────────────────────────────
# IMAP (Email Intake)
# ──────────────────────────────────────────────
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USE_SSL=true
IMAP_FOLDER=INBOX

# ──────────────────────────────────────────────
# REST API (Data Export)
# ──────────────────────────────────────────────
API_ENDPOINT_URL=https://your-instance.salesforce.com/services/data/v58.0/...
API_METHOD=POST
API_BATCH_SIZE=200

# ──────────────────────────────────────────────
# SMTP (Email Export)
# ──────────────────────────────────────────────
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
```

The `.env` file is loaded automatically when the app starts (`config.py` handles this).

---

### Option 2: System Environment Variables

Override configuration at runtime using system environment variables. This is the preferred approach for **containerized deployments, Kubernetes, and CI/CD pipelines**.

#### Setting Environment Variables

**Linux / macOS (Bash):**
```bash
export UNDO_HISTORY_LIMIT=50
export API_REQUEST_TIMEOUT=60
streamlit run flat_file_scrubber.py
```

**Windows (PowerShell):**
```powershell
$env:UNDO_HISTORY_LIMIT=50
$env:API_REQUEST_TIMEOUT=60
streamlit run flat_file_scrubber.py
```

**Docker Compose:**
```yaml
services:
  ffis:
    image: ffis-app:latest
    environment:
      UNDO_HISTORY_LIMIT: 50
      API_REQUEST_TIMEOUT: 60
      STREAMLIT_PAGE_TITLE: "Acme Corp Data Loader"
```

---

### Option 3: Secrets File (secrets.json)

The `secrets.json` file is for **sensitive credential data only**—it should never contain non-sensitive config. Use this for credentials you don't want in version control or `.env`.

#### Setup

```bash
# Copy the example file
cp secrets.json.example secrets.json

# Edit with your credentials (nano, vim, or an IDE)
nano secrets.json
```

#### Example secrets.json

```json
{
  "imap": {
    "host": "imap.gmail.com",
    "port": 993,
    "use_ssl": true,
    "folder": "INBOX"
  },
  "api": {
    "endpoint_url": "https://acme.my.salesforce.com/services/data/v58.0/composite/sobjects",
    "method": "POST",
    "headers": {
      "Authorization": "Bearer 00D5g00000IZ3ZFEA5!AQcAQIHf_5lL6Q...",
      "Content-Type": "application/json"
    },
    "batch_size": 200
  },
  "smtp": {
    "host": "smtp.gmail.com",
    "port": 587,
    "from_email": "dataloader@acme.com",
    "app_password": "abcd efgh ijkl mnop"
  }
}
```

#### Important Security Notes

- `secrets.json` is protected by `.gitignore`
- Store Salesforce Bearer tokens, SMTP passwords, and email credentials here
- For corporate deployments, use AWS Secrets Manager, HashiCorp Vault, or your organization's credential store instead
- Never commit `secrets.json` to version control

---

### Available Configuration Options

This table documents every environment variable and its purpose:

#### Streamlit UI

| Variable | Type | Default | Description |
|---|---|---|---|
| `STREAMLIT_PAGE_TITLE` | string | `Flat File Scrubber` | Browser tab title |
| `STREAMLIT_PAGE_ICON` | string | `🧹` | Browser favicon emoji |
| `STREAMLIT_LAYOUT` | string | `wide` | Page layout: `wide` or `centered` |
| `STREAMLIT_SIDEBAR_STATE` | string | `expanded` | Sidebar state on load: `expanded` or `collapsed` |

#### Salesforce

| Variable | Type | Default | Description |
|---|---|---|---|
| `SALESFORCE_OBJECTS` | CSV list | Account,Contact,Lead,... | Comma-separated list of supported objects |
| `SPECIAL_CHARS_PATTERN` | regex | `[\*\n\^\$...` | Characters to detect & remove in Step 6 |

#### Application Behavior

| Variable | Type | Default | Description |
|---|---|---|---|
| `UNDO_HISTORY_LIMIT` | integer | `20` | Max undo stack depth |
| `API_REQUEST_TIMEOUT` | integer | `30` | Seconds to wait for API responses |

#### IMAP (Email Intake)

| Variable | Type | Default | Description |
|---|---|---|---|
| `IMAP_HOST` | string | `imap.gmail.com` | Mail server IMAP hostname |
| `IMAP_PORT` | integer | `993` | IMAP port (993 for SSL, 143 for STARTTLS) |
| `IMAP_USE_SSL` | boolean | `true` | Use SSL/TLS encryption |
| `IMAP_FOLDER` | string | `INBOX` | Mailbox folder to scan |

#### REST API (Data Export)

| Variable | Type | Default | Description |
|---|---|---|---|
| `API_ENDPOINT_URL` | URL | *(empty)* | Target API endpoint URL |
| `API_METHOD` | string | `POST` | HTTP method: POST, PUT, or PATCH |
| `API_BATCH_SIZE` | integer | `200` | Records per API request |

#### SMTP (Email Export)

| Variable | Type | Default | Description |
|---|---|---|---|
| `SMTP_HOST` | string | `smtp.gmail.com` | SMTP mail server hostname |
| `SMTP_PORT` | integer | `587` | SMTP port (587 for STARTTLS, 465 for SSL) |

#### Credentials (secrets.json only)

These are **NOT** environment variables—use `secrets.json` only:

| Key | Location | Type | Example |
|---|---|---|---|
| `imap.host` | secrets.json | string | `imap.gmail.com` |
| `api.headers.Authorization` | secrets.json | string | `Bearer <token>` |
| `smtp.from_email` | secrets.json | string | `user@gmail.com` |
| `smtp.app_password` | secrets.json | string | `abcd efgh ijkl mnop` |

---

## Launching the Application

### Local Execution

From the command line:

```bash
streamlit run flat_file_scrubber.py
```

The app will automatically open in your default browser at `http://localhost:8501`.

### Running on a Custom Port

```bash
streamlit run flat_file_scrubber.py --server.port 8502
```

### Exposing to Network

To make the app accessible from other machines on your network:

```bash
streamlit run flat_file_scrubber.py --server.address 0.0.0.0
```

Then access it via `http://<your-machine-ip>:8501` from any networked computer.

### Container Deployment

```bash
docker run -p 8501:8501 \
  -e STREAMLIT_PAGE_TITLE="Acme Data Loader" \
  -e UNDO_HISTORY_LIMIT=50 \
  -v /path/to/secrets.json:/app/secrets.json:ro \
  ffis-app:latest
```

---

## Application Layout

The application is divided into two regions:

**Sidebar (left panel)**
Always visible. Contains job setup controls, the workflow step indicator, live DataFrame row count metrics, the Undo button, and the Reset button.

**Main area (tabbed)**
Eleven tabs, each representing a stage in the cleaning workflow. Tabs are non-linear — you can jump to any tab at any time. The sidebar step indicator tracks your progress.

```
┌─────────────────────┬───────────────────────────────────────────────────────┐
│  SIDEBAR            │  TABS                                                 │
│                     │                                                       │
│  Job Short Desc     │  📥 Ingest | 🔍 Inspect | ✂️ Drop/Rename |           │
│  Target Object      │  🕳 Nulls  | 🔢 Types   | 🔡 Spec Chars |            │
│                     │  🔁 Dupes  | ❌ Incomplete | ☁️ SF Dupes |            │
│  Workflow Steps ①…⑪ │  📤 Export | 📋 Data Frames                          │
│                     │                                                       │
│  ✅ Clean  🔁 Dupes  │                                                       │
│  ⚠️ Bad   ☁️ SF Dupes│                                                       │
│                     │                                                       │
│  ↩ Undo             │                                                       │
│  🔄 Reset           │                                                       │
└─────────────────────┴───────────────────────────────────────────────────────┘
```

---

## Sidebar Controls

| Control | Purpose |
|---|---|
| **Job Short Description** | Enter a brief label (e.g. `Apollo Load`). The app auto-generates a job name in the format `Apollo_Load_DDMMYYYY` and displays it. This name is used to prefix all exported file names. |
| **Salesforce / Target Object** | Select the object type you are loading. This drives the required-field checklist in the Inspect tab. |
| **Workflow Steps** | Visual indicator showing which step you are currently on. Updates automatically as you progress. |
| **DataFrame Counts** | Live row counts for all four DataFrames (Clean, Dupes, Bad, SF Dupes). Updates after every operation. |
| **↩ Undo Last Operation** | Reverts the Clean DataFrame to its state before the most recent destructive action. Up to N operations are stored (configurable via `UNDO_HISTORY_LIMIT`). |
| **🔄 Reset Everything** | Clears all session state and DataFrames, returning the app to its initial state. **This is irreversible.** |

---

## Step-by-Step Workflow Guide

### Step 1 — Ingest

**Tab:** 📥 Ingest

This is the entry point. Select an intake method using the radio buttons at the top of the tab, then load your file. See [Intake Methods](#intake-methods) for full details on each mode.

After a successful load you will see:
- A confirmation box showing the original filename, the job-name alias, and the row × column count.
- A 10-row preview of the data.

The sidebar step indicator advances to ② automatically.

> **Tip:** You can re-ingest a new file at any time. It will overwrite all four DataFrames and reset the undo history.

---

### Step 2 — Inspect

**Tab:** 🔍 Inspect

Provides a comprehensive view of the dataset before any modifications are made.

**What you will see:**

- **Row / Column / Memory metrics** — top-level size summary.
- **Column Info table** — for every column: data type, non-null count, null percentage, unique value count, and a sample value.
- **Descriptive Statistics** — `.describe(include="all")` covering count, mean, min, max, frequency for numeric and categorical columns.
- **Required Fields check** — lists which standard Salesforce fields for your selected object are present vs. missing in the incoming file.

**Decisions to make here:**

1. **SF ID Column** — If your CSV contains Salesforce IDs (e.g. an `Id` or `AccountId` column), select it from the dropdown. This is used later during the SF Dupe Check.
2. **Mark Required Fields** — Use the multiselect to tag which incoming columns must not be blank. These become the basis for the Incomplete Records check in Step 8.

Click **✅ Confirm Inspection** when done.

---

### Step 3 — Drop & Rename Columns

**Tab:** ✂️ Drop / Rename

**Drop Columns**

A null-percentage table is shown at the top to help you decide which columns are too sparse to be useful. Select one or more columns in the multiselect and click **🗑 Drop Selected Columns**.

**Rename Columns**

Every column is shown with a text field. Change any name and click **✏️ Apply Renames** to apply all renames at once. This is the correct place to map incoming column names to Salesforce API names (e.g. rename `Full Name` → `Name`, or `Account Number` → `AccountId`).

> Each operation pushes the pre-change state onto the undo stack.

---

### Step 4 — Null Handling

**Tab:** 🕳 Null Handling

Only columns that contain at least one null value are shown. Each column is presented in an expandable panel showing:
- Null count and percentage
- Up to 30 sample unique values (to help you decide on a fill strategy)

**Available actions per column:**

| Action | Effect |
|---|---|
| Leave as-is | No change made |
| Fill with signal value | Replaces nulls with a value you specify (e.g. `-1`, `UNKNOWN`, `0`). Numeric strings are auto-cast to the correct type. |
| Fill with mean / median / mode | Calculates the statistic from non-null values and fills nulls with it. Useful for numeric columns. |
| Drop rows where null | Removes every row where this column is null from the Clean DataFrame. Rows are not moved to the Bad DataFrame — they are deleted. |
| Drop this column | Removes the entire column from the Clean DataFrame. |

> **Best practice:** For columns that are required for your Salesforce load, use a recognizable signal value (e.g. `-1` for floats, `MISSING` for strings) rather than dropping rows. This allows downstream review.

---

### Step 5 — Types & Splits

**Tab:** 🔢 Types & Splits

**Type Casting**

All columns are displayed in a grid showing their current detected type. Change any column to a new type and click **🔄 Apply Type Conversions**. Supported target types:

`object` · `int64` · `float64` · `bool` · `datetime64[ns]` · `category` · `string`

Date columns should be cast to `datetime64[ns]`. If the conversion fails for any column, a warning is shown and the remaining conversions still apply.

**Column Splitting**

Select a column, enter a delimiter character, name the two output columns, and click **✂️ Split Column**. The original column is dropped by default (uncheck "Keep original column" to retain it).

Common use cases:
- Full name → First Name / Last Name (delimiter: space)
- Date + time combined → date / time (delimiter: `T` or space)
- City, State combined → City / State (delimiter: `,`)
- Street address with apartment → Street / Apt (delimiter: `#`)

---

### Step 6 — Special Characters

**Tab:** 🔡 Special Chars

Scans text columns for characters that commonly cause issues in Salesforce imports or database loads.

The default pattern is configurable via the `SPECIAL_CHARS_PATTERN` environment variable.

**Workflow:**
1. Select which columns to scan (all text columns are pre-selected).
2. Click **🔍 Preview Special Characters Found** to see a count of affected rows per column before making any changes.
3. Set the replacement string (blank = remove the character entirely).
4. Click **🧹 Scrub Special Characters** to apply.

The **Inspect Unique Values** expander at the bottom of this tab lets you view all distinct values in any text column — useful for spotting encoding artifacts or stray characters before scrubbing.

---

### Step 7 — Duplicates

**Tab:** 🔁 Duplicates

Detects records that are identical to other records within the same file (submitted duplicates).

**Configuration:**
- **Subset columns** — By default, duplicate detection compares all columns. Use the multiselect to compare only specific columns (e.g. just `Email` or `Name` + `Phone`).
- **Keep option** — Choose whether to keep the first occurrence, the last, or neither (move all copies).

After clicking **📦 Move Duplicates to Dupes DataFrame**, duplicate rows are removed from Clean and added to the Dupes DataFrame. They remain available for review and export.

---

### Step 8 — Incomplete Records

**Tab:** ❌ Incomplete

Uses the required columns you defined in Step 2 (Inspect) to find records that are missing critical data.

If no required columns have been set, you will be prompted to return to the Inspect tab and define them.

Click **Preview incomplete records** to review affected rows, then **📦 Move Incomplete Records to Bad DataFrame** to quarantine them. These rows are available in the Bad DataFrame for review, correction, and re-export.

---

### Step 9 — Salesforce Duplicate Check

**Tab:** ☁️ SF Dupe Check

Compares the Clean DataFrame against a reference export from Salesforce or Snowflake to identify records that already exist in the system.

**How to use:**
1. Export a report from Salesforce (or query your Snowflake Salesforce replica table) and save it as CSV.
2. Upload that reference CSV in this tab.
3. Select the matching column in your incoming file and the matching column in the reference file (typically both will be `Id`, `Email`, `Name`, or an external key).
4. Click **🔍 Find SF Duplicates**.
5. If matches are found, click **📦 Move SF Dupes to SF Dupes DataFrame**.

> **Note:** Matching is case-insensitive and strips leading/trailing whitespace from both sides before comparison.

---

### Step 10 — Export

**Tab:** 📤 Export

The final step. A summary showing record counts across all four DataFrames is shown at the top.

#### Option A — Download as CSV

One download button per DataFrame, only shown if that DataFrame contains rows:

| Button | File name format |
|---|---|
| ⬇ Clean CSV | `<job_name>_clean.csv` |
| ⬇ Dupes CSV | `<job_name>_dupes.csv` |
| ⬇ Bad CSV | `<job_name>_bad.csv` |
| ⬇ SF Dupes CSV | `<job_name>_sf_dupes.csv` |

#### Option B — Load via API

Posts records to a REST endpoint as JSON. Supports Salesforce Bulk API v2, Composite API, Mulesoft, custom REST APIs, and similar targets.

**Required fields:**
- **API Endpoint URL** — the full URL of the target endpoint (can be pre-configured with `API_ENDPOINT_URL` environment variable)
- **HTTP Method** — POST, PUT, or PATCH (default configurable with `API_METHOD`)
- **Headers** — a valid JSON object (paste in your Authorization Bearer token here)
- **Batch size** — number of records per request (default configurable with `API_BATCH_SIZE`; 200 is a safe default; Salesforce Bulk API supports up to 10,000 per batch)

Records are sent as `{"records": [...]}` in the request body. A progress bar tracks batch completion. See [API Load Reference](#api-load-reference) for Salesforce-specific configuration.

#### Option C — Email to Stakeholder

Sends selected DataFrames as CSV attachments via SMTP.

**Required fields:**
- SMTP Host and Port (defaults configurable with `SMTP_HOST` and `SMTP_PORT`)
- From email address and App Password
- To email address(es), comma-separated
- Subject and body (pre-populated with job name and record counts)
- Attachment selection (choose which DataFrames to attach)

See [Email Configuration Reference](#email-configuration-reference) for provider-specific settings.

---

### Step 11 — Data Frames Viewer

**Tab:** 📋 Data Frames

Provides a full view of all four DataFrames in sub-tabs. From here you can:

- **Browse** any DataFrame with full pagination
- **Restore rows** — click "↩ Move ALL [DataFrame] back to Clean" to return all rows in a side DataFrame back to the Clean DataFrame
- **Manually move rows** — in the Clean sub-tab, select specific row indices and move them to Dupes, Bad, or SF Dupes
- **Review undo history** — the History sub-tab lists all operations in the undo stack and lets you undo the most recent one

---

## Intake Methods

### 📁 Browser Upload

Standard drag-and-drop file upload through the browser. Accepts `.csv` and `.txt` files. Maximum file size is controlled by Streamlit's server configuration (default 200 MB).

### 🗂 File Path / Directory

Enter an absolute path on the machine running the Streamlit server.

**Single file:**
```
/data/imports/accounts_march.csv
```

**Directory:**
```
/data/imports/
```
When a directory is entered, all `.csv` and `.txt` files inside it are listed in a dropdown with file sizes and modification timestamps. Select one and click **📥 Load Selected File**.

> This mode is most useful when the application is deployed on a server that already receives files via SFTP, a scheduled export, or a network share.

### 📧 Email (IMAP)

Connects to an IMAP mailbox and retrieves CSV attachments from emails.

**Configuration fields:**

| Field | Description |
|---|---|
| IMAP Host | Your mail server's IMAP address |
| Port | 993 for IMAPS (SSL), 143 for STARTTLS |
| Use SSL | Check for port 993; uncheck for 143 |
| Mailbox Folder | Usually `INBOX`; can be a subfolder (e.g. `INBOX/DataLoads`) |
| Email Address | Your full email address |
| Password / App Password | See provider table below |
| Subject Filter | Optional text to filter by email subject. Leave blank to scan all unread messages. |
| Search ALL mail | When checked, scans all messages (not just unread) and marks retrieved ones as read |

**Provider Quick Reference:**

| Provider | IMAP Host | Port | Notes |
|---|---|---|---|
| Gmail | `imap.gmail.com` | 993 | Requires an App Password. Enable 2FA first at myaccount.google.com, then generate a password at myaccount.google.com/apppasswords |
| Outlook / Office 365 | `outlook.office365.com` | 993 | Use your regular Microsoft password, or an app password if MFA is enabled |
| Yahoo Mail | `imap.mail.yahoo.com` | 993 | Requires an App Password from Yahoo Account Security settings |
| Generic IMAP | Your server's address | 993 or 143 | Contact your IT/email admin for credentials |

Once attachments are retrieved, they appear in a dropdown. A 5-line raw preview is available before loading. After a successful load the attachment cache is cleared.

---

## Supported Salesforce Objects

The application is pre-configured with required-field awareness for the following objects:

| Object | Pre-defined Required Fields |
|---|---|
| Account | Name |
| Contact | LastName, AccountId |
| Lead | LastName, Company |
| Opportunity | Name, StageName, CloseDate, AccountId |
| Account to Account Relationship | ParentId, ChildId |
| Account to Contact Relationship | AccountId, ContactId |
| User | LastName, Username, Email, ProfileId, TimeZoneSidKey, LocaleSidKey, EmailEncodingKey, LanguageLocaleKey |
| Snowflake Table — DEFAULT | *(none — free-form)* |

You can supplement or override the pre-defined required fields in the **Inspect** tab using the "Mark Required Fields from Incoming CSV" multiselect.

To add custom objects or change required fields, modify the `get_required_fields()` function in `config.py` or add a custom configuration file and load it via environment variables.

---

## The Four DataFrames

All data in the application is partitioned across four named DataFrames that persist for the duration of your session.

| DataFrame | Contents | Key |
|---|---|---|
| **Clean** | Records that have passed all checks performed so far | `clean_df` |
| **Dupes** | Records moved from Clean because they were exact duplicates of other rows in the file | `dupes_df` |
| **Bad / Incomplete** | Records moved from Clean because one or more required fields were blank | `bad_df` |
| **SF Dupes** | Records moved from Clean because they already exist in Salesforce/Snowflake | `sf_dupes_df` |

All four DataFrames can be:
- Viewed in the **Data Frames** tab
- Downloaded as CSV from the **Export** tab
- Restored back to Clean at any time from the **Data Frames** tab

---

## Undo History

Every destructive operation (drop, fill, split, move, cast, scrub) pushes a snapshot of the Clean DataFrame onto the undo stack before making the change.

The maximum number of stored operations is configurable via the `UNDO_HISTORY_LIMIT` environment variable (default: **20**).

To undo, click **↩ Undo Last Operation** in the sidebar or in the Data Frames → History sub-tab.

> **Important:** Undo only affects the **Clean DataFrame**. Rows that have been moved to Dupes, Bad, or SF Dupes are not reversed by undo — use the "Move ALL back to Clean" buttons in the Data Frames tab for that.

---

## Email Configuration Reference

### Gmail

```
SMTP Host:  smtp.gmail.com
Port:       587
```

You **must** use an App Password, not your regular Gmail password.

1. Go to [myaccount.google.com](https://myaccount.google.com)
2. Enable 2-Step Verification if not already enabled
3. Go to Security → App Passwords
4. Create a new App Password for "Mail" / "Other"
5. Copy the 16-character password into the app

### Outlook / Office 365

```
SMTP Host:  smtp.office365.com
Port:       587
```

Use your regular Microsoft password (or an app password if your org enforces MFA).

### Yahoo Mail

```
SMTP Host:  smtp.mail.yahoo.com
Port:       587
```

Requires an App Password from Yahoo Account Security settings.

---

## API Load Reference

### Salesforce REST API

```
URL:     https://<your-instance>.my.salesforce.com/services/data/v58.0/sobjects/<ObjectName>/
Method:  POST
Headers: {"Authorization": "Bearer <access_token>", "Content-Type": "application/json"}
Batch:   1 (REST API processes one record per call)
```

### Salesforce Composite API (bulk inserts)

```
URL:     https://<your-instance>.my.salesforce.com/services/data/v58.0/composite/sobjects
Method:  POST
Headers: {"Authorization": "Bearer <access_token>", "Content-Type": "application/json"}
Batch:   200 (maximum per composite request)
```

### Salesforce Bulk API v2

```
URL:     https://<your-instance>.my.salesforce.com/services/data/v58.0/jobs/ingest
Method:  POST
Headers: {"Authorization": "Bearer <access_token>", "Content-Type": "application/json"}
Batch:   10000
```

> **Note:** Obtaining a Salesforce Bearer token requires a separate OAuth flow (username-password flow, JWT Bearer flow, or Connected App). The Flat File Scrubber does not perform OAuth — paste your token directly into the Headers field.

---

## Troubleshooting

### "ModuleNotFoundError: No module named 'streamlit'"
Run `pip install -r requirements.txt` and ensure your virtual environment is activated.

### The browser does not open automatically
Navigate manually to `http://localhost:8501` in your browser.

### Configuration changes don't take effect
1. **For .env changes:** Streamlit automatically detects `.env` file changes. If changes don't reflect, restart with `Ctrl+C` and `streamlit run flat_file_scrubber.py`.
2. **For environment variables:** They're read once on app start. Restart streamlit for changes to apply.
3. **For secrets.json changes:** Restart the app.

### File upload size limit exceeded
Increase Streamlit's upload limit by creating a `.streamlit/config.toml` file in the project directory:
```toml
[server]
maxUploadSize = 500
```
This sets the limit to 500 MB.

### IMAP connection fails
- Confirm the host and port are correct for your provider (use values from Environment Variables table or Email Configuration Reference).
- For Gmail: ensure 2FA is enabled and you are using an App Password (not your regular password).
- For corporate mail: confirm that IMAP access is enabled by your IT administrator.
- If behind a firewall, confirm outbound port 993 (or 143) is open.

### API POST returns 401 Unauthorized
Your Bearer token has expired. Salesforce tokens expire after a period defined by your org's session settings (typically 2–12 hours). Re-authenticate and paste the new token into the Headers field. You can also store the token in `secrets.json` under `api.headers.Authorization` to avoid re-entering it.

### API POST returns 400 Bad Request
The payload structure does not match what the endpoint expects. Check that your column names in the Clean DataFrame match the target API's expected field names exactly (Salesforce API names are case-sensitive, e.g. `LastName` not `last_name`). Use the Rename Columns feature in the Drop & Rename tab to align names before exporting.

### Email send fails with "SMTPAuthenticationError"
Use an App Password rather than your account password. See [Email Configuration Reference](#email-configuration-reference). You can also store SMTP credentials in `secrets.json` under `smtp` to avoid re-entering them.

### Undo is grayed out / has no effect
The undo stack is empty — either no operations have been performed yet, or `UNDO_HISTORY_LIMIT` operations have been performed and the oldest entries have been dropped. The stack is also cleared on full reset.

### "Reset Everything" lost my work
The Reset button clears all session state immediately with no confirmation. This is intentional for speed during repeated testing cycles. If this is a concern, download your Clean CSV before resetting.

### Custom configuration not loading
1. Ensure `.env` is in the same directory as `flat_file_scrubber.py`.
2. Check for typos in variable names (case-sensitive).
3. Restart streamlit: `streamlit run flat_file_scrubber.py`.

---

## Architecture & Extensibility

### File Structure

```
FFIS/
├── flat_file_scrubber.py      # Main Streamlit application
├── config.py                   # Configuration system (env, defaults, secrets)
├── requirements.txt            # Python dependencies
├── README.md                   # This file
├── .env.example               # Environment variables template
├── .gitignore                 # Prevents .env, secrets.json from being committed
├── secrets.json.example       # Credentials template
└── secrets.json               # (local, not committed) Actual credentials
```

### Configuration Module (config.py)

The `config.py` module provides:
- Automatic `.env` file loading
- Helper functions for type conversion (`getenv_str`, `getenv_int`, `getenv_bool`, `getenv_list`)
- High-level getter functions for each feature area (`get_salesforce_objects()`, `get_imap_config()`, etc.)
- Fallback defaults for all settings

Adding a new configuration option:

1. Add it to `.env.example` with a clear comment
2. Add a getter function to `config.py` (e.g., `def get_my_setting(): return getenv_str("MY_SETTING", "default")`)
3. Import and call it in `flat_file_scrubber.py`

### Adding Custom Salesforce Objects

Edit `config.py`, find the `get_required_fields()` function, and add your object:

```python
def get_required_fields():
    """Get required fields for each Salesforce object."""
    return {
        "Account": ["Name"],
        "Contact": ["LastName", "AccountId"],
        "MyCustomObject__c": ["SomeField__c", "AnotherField__c"],  # ← Add here
        # ...
    }
```

---

*Flat File Scrubber · SFCOE · Matthew Kelly · v1.0 · January 2026*
